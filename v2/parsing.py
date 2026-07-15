"""Message-text parsing: transaction intent, fees, bank references.

Pure functions, no I/O — fully unit-testable. Formats are identical to v1
(see README "Message formats"), so existing group workflows keep working.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from models import BreakdownEntry, TxInfo, TxType

logger = logging.getLogger(__name__)

_BASIC_P2P = re.compile(
    r"p2p\s+sell\s+([\d,]+(?:\.\d+)?)\s*[×xX\*]\s*([\d,]+(?:\.\d+)?)\s*=\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_P2P_NEW = re.compile(
    r"p2p\s+sell\s+([\d,]+(?:\.\d+)?)\s*[×xX\*]\s*([\d,]+(?:\.\d+)?)\s*=\s*([\d,]+(?:\.\d+)?)\s*fee\s*-?\s*([\d.]+)",
    re.IGNORECASE,
)
_P2P_LEGACY = re.compile(
    r"sell\s+([\d,]+(?:\.\d+)?)\s*/\s*([\d.]+)\s*=\s*([\d.]+)\s*fee\s*-?\s*([\d.]+)",
    re.IGNORECASE,
)
_TO_BANK = re.compile(r"to\s+([A-Za-z\s]+?)\s*\(([^)]+)\)", re.IGNORECASE)
_FROM_BANK = re.compile(r"from\s+([A-Za-z\s]+?)\s*\(([^)]+)\)", re.IGNORECASE)
_BREAKDOWN = re.compile(r"([\d,]+(?:\.\d+)?)\s*to\s+([A-Za-z\s]+?)\s*\(([^)]+)\)", re.IGNORECASE)
_BUY_WORD = re.compile(r"\bbuy\b", re.IGNORECASE)
_SELL_WORD = re.compile(r"\bsell\b", re.IGNORECASE)
_AMOUNT_AFTER_TYPE = re.compile(r"\b(buy|sell)\s+([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_MMK_AFTER_EQUALS = re.compile(r"=\s*([\d,]+\.?\d*)")
_FEE_IN_REPLY = re.compile(r"fee\s*-\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_FROM_IN_REPLY = re.compile(r"From\s+([^(]+)\(([^)]+)\)", re.IGNORECASE)
_TRANSFER = re.compile(r"([A-Za-z\s]+)\(([^)]+)\)\s+to\s+([A-Za-z\s]+)\(([^)]+)\)", re.IGNORECASE)
_COIN_TRANSFER = re.compile(
    r"([A-Za-z\s]+)\s*\(([^)]+)\)\s+to\s+([A-Za-z\s]+)\s*\(([^)]+)\)\s+"
    r"([\d.]+)\s*USDT\s*-\s*([\d.]+)\s*USDT\s*\(fee\)\s*=\s*([\d.]+)\s*USDT",
    re.IGNORECASE,
)


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _full_name(prefix: str, bank: str) -> str:
    return f"{prefix.strip()}({bank.strip()})"


def parse_breakdown(text: str) -> list[BreakdownEntry]:
    """'2,042,960 to San (Wave)' entries, possibly concatenated without spaces."""
    entries = []
    for amount_str, prefix, bank in _BREAKDOWN.findall(text):
        entries.append(
            BreakdownEntry(
                amount=_num(amount_str),
                prefix=prefix.strip(),
                bank=bank.strip(),
                bank_name=_full_name(prefix, bank),
            )
        )
    return entries


def extract_transaction_info(text: str) -> TxInfo:
    """Classify a message's transaction intent. See README for the formats.

    Precedence (same as v1):
    1. Staff P2P Sell  — ``P2P Sell U×R =M ... to D(B) From S(B)`` (no fee, no OCR)
    2. P2P Sell (new)  — ``P2P Sell U×R=Mfee-F [breakdown]``
    3. P2P Sell legacy — ``sell M/U=R fee-F [breakdown]``
    4. Regular Buy/Sell — any text containing "buy"/"sell"
    """
    stripped = text.strip().lower()

    if stripped.startswith("p2p sell"):
        basic = _BASIC_P2P.search(text)
        if basic:
            dest = _TO_BANK.search(text)
            src = _FROM_BANK.search(text)
            if dest and src:
                return TxInfo(
                    type=TxType.STAFF_P2P_SELL,
                    usdt=_num(basic.group(1)),
                    rate=_num(basic.group(2)),
                    mmk=_num(basic.group(3)),
                    fee=0.0,
                    total_usdt=_num(basic.group(1)),
                    dest_bank=_full_name(dest.group(1), dest.group(2)),
                    src_bank=_full_name(src.group(1), src.group(2)),
                )

        new = _P2P_NEW.search(text)
        if new:
            usdt, rate, mmk, fee = (_num(new.group(1)), _num(new.group(2)),
                                    _num(new.group(3)), float(new.group(4)))
            return TxInfo(
                type=TxType.P2P_SELL, usdt=usdt, rate=rate, mmk=mmk, fee=fee,
                total_usdt=usdt + fee, bank_breakdown=parse_breakdown(text),
            )

    if "fee-" in stripped or "fee -" in stripped:
        legacy = _P2P_LEGACY.search(text)
        if legacy:
            mmk, usdt, rate, fee = (_num(legacy.group(1)), float(legacy.group(2)),
                                    float(legacy.group(3)), float(legacy.group(4)))
            return TxInfo(
                type=TxType.P2P_SELL, usdt=usdt, rate=rate, mmk=mmk, fee=fee,
                total_usdt=usdt + fee, bank_breakdown=parse_breakdown(text),
            )

    tx_type: Optional[TxType] = None
    if _BUY_WORD.search(text):
        tx_type = TxType.BUY
    elif _SELL_WORD.search(text):
        tx_type = TxType.SELL

    amount_match = _AMOUNT_AFTER_TYPE.search(text)
    mmk_match = _MMK_AFTER_EQUALS.search(text)
    return TxInfo(
        type=tx_type,
        usdt=_num(amount_match.group(2)) if amount_match else 0.0,
        mmk=_num(mmk_match.group(1)) if mmk_match else 0.0,
    )


def parse_reply_fee(text: str) -> float:
    """MMK fee a staff reply may carry: ``fee-3039``."""
    match = _FEE_IN_REPLY.search(text or "")
    return _num(match.group(1)) if match else 0.0


def parse_reply_source_bank(text: str) -> Optional[str]:
    """Explicit bank override in a staff reply: ``From San(Kpay P)``."""
    match = _FROM_IN_REPLY.search(text or "")
    return _full_name(match.group(1), match.group(2)) if match else None


def parse_internal_transfer(text: str) -> Optional[tuple[str, str]]:
    """'San(Wave Channel) to NDT (Wave)' -> (from_name, to_name)."""
    match = _TRANSFER.search(text or "")
    if not match:
        return None
    return _full_name(match.group(1), match.group(2)), _full_name(match.group(3), match.group(4))


def parse_coin_transfer(text: str) -> Optional[dict]:
    """'San (binance) to OKM(Wallet) 10 USDT-0.47 USDT(fee) = 9.53 USDT'."""
    match = _COIN_TRANSFER.search(text or "")
    if not match:
        return None
    return {
        "from_bank": _full_name(match.group(1), match.group(2)),
        "to_bank": _full_name(match.group(3), match.group(4)),
        "sent": float(match.group(5)),
        "fee": float(match.group(6)),
        "received": float(match.group(7)),
    }
