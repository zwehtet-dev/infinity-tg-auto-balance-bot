"""Balance sheet: parsing, formatting, and the transactional ledger.

The ledger is the single write path for balances. It guarantees:

- **Serialized mutations** — an ``asyncio.Lock`` prevents two concurrently
  processed receipts from interleaving read-modify-write on the same bank
  (a real race in v1, where handlers mutated shared dicts freely).
- **Validate-then-apply** — every change in a transaction is checked (bank
  exists, debits covered) before anything is mutated; failures leave the
  sheet untouched.
- **Durability** — every applied state is persisted to the DB in the same
  atomic transaction as its audit record, so a restart resumes from the
  exact last state instead of waiting for someone to re-post a balance
  message (v1 lost all balances on restart).
- **Auditability** — every mutation writes an audit row: who, what, why,
  and the per-bank deltas.
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from dataclasses import dataclass
from typing import Optional

from db import Database
from models import BalanceChange, Balances, BankBalance, Currency
from repositories import AuditRepo

logger = logging.getLogger(__name__)

# Matches: San(KBZ)-11044185 | TZT (Binance)-(222.6) | NDT (Wave) -2864900
# and tolerates trailing annotations like NDT(Binance)-6.96(52.96)
_BANK_LINE = re.compile(
    r"([A-Za-z\s]+?)\s*\(([^)]+)\)\s*-\s*\(?([\d,]+(?:\.\d+)?)\)?(?:\([^)]+\))?"
)


class LedgerError(Exception):
    """A balance operation failed validation. ``str(e)`` is user-presentable."""


def parse_balance_message(text: str) -> Optional[Balances]:
    """Parse the operational balance format.

    ::

        San(Kpay P) -2639565
        San(KBZ)-11044185
        USDT
        San(Swift) -81.99
        THB
        ACT(Bkk B) -13223

    The hyphen is a separator, not a minus sign. Also tolerates the whole
    message on a single line.
    """
    try:
        text = text.strip()
        if text.startswith("MMK"):
            text = text[3:]

        usdt_start = text.find("USDT")
        if usdt_start == -1:
            logger.warning("Balance parse: missing USDT marker")
            return None
        thb_start = text.find("THB")

        mmk_section = text[:usdt_start]
        if thb_start != -1 and thb_start > usdt_start:
            usdt_section = text[usdt_start + 4 : thb_start]
            thb_section = text[thb_start + 3 :]
        else:
            usdt_section = text[usdt_start + 4 :]
            thb_section = ""

        def parse_section(section: str) -> list[BankBalance]:
            banks = []
            for match in _BANK_LINE.finditer(section):
                prefix = match.group(1).strip()
                bank = match.group(2).strip()
                try:
                    amount = float(match.group(3).replace(",", ""))
                except ValueError:
                    logger.warning("Balance parse: bad amount for %s(%s)", prefix, bank)
                    continue
                banks.append(
                    BankBalance(
                        bank_name=f"{prefix}({bank})", prefix=prefix, bank=bank, amount=amount
                    )
                )
            return banks

        balances = Balances(
            mmk=parse_section(mmk_section),
            usdt=parse_section(usdt_section),
            thb=parse_section(thb_section),
        )
        if balances.is_empty:
            return None
        logger.info(
            "Parsed balance: %d MMK, %d USDT, %d THB banks",
            len(balances.mmk), len(balances.usdt), len(balances.thb),
        )
        return balances
    except Exception:
        logger.error("Balance parse error:\n%s", traceback.format_exc())
        return None


def format_balance_message(balances: Balances) -> str:
    """Render the sheet back in the exact v1 format (MMK/THB as integers,
    USDT with 4 decimals, hyphen separators)."""
    lines: list[str] = []
    for bank in balances.mmk:
        lines.append(f"{bank.bank_name} -{abs(int(bank.amount)):,}")
    lines.append("")
    lines.append("USDT")
    for bank in balances.usdt:
        lines.append(f"{bank.bank_name} -{abs(bank.amount):.4f}")
    if balances.thb:
        lines.append("")
        lines.append("THB")
        for bank in balances.thb:
            lines.append(f"{bank.bank_name} -{abs(int(bank.amount)):,}")
    return "\n".join(lines).strip()


@dataclass
class ApplyResult:
    balances: Balances          # post-apply snapshot (deep copy, safe to read)
    balance_text: str           # formatted sheet, ready to post


class BalanceLedger:
    def __init__(self, db: Database, audit: AuditRepo):
        self._db = db
        self._audit = audit
        self._lock = asyncio.Lock()
        self._balances: Optional[Balances] = None

    # ------------------------------------------------------------------ reads

    @property
    def is_loaded(self) -> bool:
        return self._balances is not None and not self._balances.is_empty

    def snapshot(self) -> Optional[Balances]:
        """Deep copy for read-only use (OCR candidate lists, previews)."""
        return self._balances.copy() if self._balances else None

    # ------------------------------------------------------------------ loads

    async def load_from_db(self) -> bool:
        """Restore the last persisted sheet on startup."""
        rows = await self._db.fetchall(
            "SELECT currency, bank_name, prefix, bank, amount FROM balances ORDER BY currency, position"
        )
        if not rows:
            return False
        balances = Balances()
        for currency, bank_name, prefix, bank, amount in rows:
            balances.section(Currency(currency)).append(
                BankBalance(bank_name=bank_name, prefix=prefix, bank=bank, amount=amount)
            )
        self._balances = balances
        logger.info(
            "Restored balances from DB: %d MMK, %d USDT, %d THB banks",
            len(balances.mmk), len(balances.usdt), len(balances.thb),
        )
        return True

    async def load(
        self,
        balances: Balances,
        actor_id: Optional[int] = None,
        actor_name: Optional[str] = None,
        ref_message_id: Optional[int] = None,
    ) -> None:
        """Replace the whole sheet (balance message posted / /load)."""
        async with self._lock:
            statements: list[tuple[str, tuple]] = [("DELETE FROM balances", ())]
            for currency in Currency:
                for position, bank in enumerate(balances.section(currency)):
                    statements.append(
                        (
                            "INSERT INTO balances (currency, bank_name, prefix, bank, amount, position) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (currency.value, bank.bank_name, bank.prefix, bank.bank,
                             bank.amount, position),
                        )
                    )
            await self._db.transaction(statements)
            await self._audit.record(
                tx_type="balance_load",
                description=(
                    f"Balance sheet loaded: {len(balances.mmk)} MMK, "
                    f"{len(balances.usdt)} USDT, {len(balances.thb)} THB banks"
                ),
                changes=[],
                actor_id=actor_id,
                actor_name=actor_name,
                ref_message_id=ref_message_id,
            )
            self._balances = balances.copy()

    # ------------------------------------------------------------------ apply

    async def apply(
        self,
        changes: list[BalanceChange],
        *,
        tx_type: str,
        description: str,
        actor_id: Optional[int] = None,
        actor_name: Optional[str] = None,
        ref_message_id: Optional[int] = None,
    ) -> ApplyResult:
        """Atomically apply a set of signed deltas.

        Raises :class:`LedgerError` (user-presentable) when the sheet isn't
        loaded, a bank is unknown, or a debit exceeds the available amount.
        On success the new state and its audit record are already persisted.
        """
        if not changes:
            raise LedgerError("Nothing to apply")

        async with self._lock:
            if not self.is_loaded:
                raise LedgerError(
                    "Balance not loaded. Post the balance message in the auto balance topic first."
                )
            balances = self._balances  # type: ignore[assignment]

            # Phase 1 — validate everything before touching anything.
            resolved: list[tuple[BankBalance, BalanceChange]] = []
            net: dict[int, float] = {}  # id(bank) -> summed delta (a bank may appear twice)
            for change in changes:
                bank = balances.find(change.currency, change.bank_name)
                if bank is None:
                    raise LedgerError(
                        f"Bank not found in balance sheet: {change.bank_name} "
                        f"({change.currency.value.upper()})"
                    )
                net[id(bank)] = net.get(id(bank), 0.0) + change.delta
                resolved.append((bank, change))

            for bank, _ in resolved:
                new_amount = bank.amount + net[id(bank)]
                if net[id(bank)] < 0 and new_amount < 0:
                    unit = "MMK" if bank in balances.mmk else ("USDT" if bank in balances.usdt else "THB")
                    raise LedgerError(
                        f"Insufficient balance!\n\n"
                        f"{bank.bank_name}: {bank.amount:,.4f} {unit}\n"
                        f"Required: {abs(net[id(bank)]):,.4f} {unit}\n"
                        f"Shortage: {abs(new_amount):,.4f} {unit}"
                    )

            # Phase 2 — persist new amounts + audit row in one DB transaction.
            changes_json = []
            statements: list[tuple[str, tuple]] = []
            seen: set[int] = set()
            for bank, change in resolved:
                changes_json.append(
                    {
                        "currency": change.currency.value,
                        "bank": bank.bank_name,
                        "delta": round(change.delta, 6),
                        "before": round(bank.amount, 6),
                        "after": round(bank.amount + net[id(bank)], 6),
                    }
                )
                if id(bank) not in seen:
                    seen.add(id(bank))
                    statements.append(
                        (
                            "UPDATE balances SET amount = ?, updated_at = CURRENT_TIMESTAMP "
                            "WHERE currency = ? AND bank_name = ?",
                            (bank.amount + net[id(bank)], change.currency.value, bank.bank_name),
                        )
                    )
            await self._db.transaction(statements)
            await self._audit.record(
                tx_type=tx_type,
                description=description,
                changes=changes_json,
                actor_id=actor_id,
                actor_name=actor_name,
                ref_message_id=ref_message_id,
            )

            # Phase 3 — mutate memory only after the DB write succeeded.
            applied: set[int] = set()
            for bank, _ in resolved:
                if id(bank) not in applied:
                    applied.add(id(bank))
                    bank.amount += net[id(bank)]

            snapshot = balances.copy()
            return ApplyResult(balances=snapshot, balance_text=format_balance_message(snapshot))
