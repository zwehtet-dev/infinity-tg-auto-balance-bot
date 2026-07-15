"""Domain models shared across the bot.

Plain dataclasses — no framework coupling — so every layer (parsing, OCR,
ledger, handlers) speaks the same language instead of ad-hoc dicts.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Currency(str, Enum):
    MMK = "mmk"
    USDT = "usdt"
    THB = "thb"


def normalize_bank_name(bank_name: str) -> str:
    """Case/space-insensitive canonical form: 'MMN ( Swift )' -> 'mmn(swift)'."""
    if not bank_name:
        return ""
    return bank_name.replace(" ", "").lower()


def banks_match(a: str, b: str) -> bool:
    return normalize_bank_name(a) == normalize_bank_name(b)


@dataclass
class BankBalance:
    """One line of the balance sheet, e.g. San(KBZ) -11044185."""

    bank_name: str  # "San(KBZ)"
    prefix: str     # "San"
    bank: str       # "KBZ"
    amount: float

    def matches(self, other_name: str) -> bool:
        return banks_match(self.bank_name, other_name)


@dataclass
class Balances:
    """The full balance sheet, grouped by currency."""

    mmk: list[BankBalance] = field(default_factory=list)
    usdt: list[BankBalance] = field(default_factory=list)
    thb: list[BankBalance] = field(default_factory=list)

    def section(self, currency: Currency) -> list[BankBalance]:
        return {Currency.MMK: self.mmk, Currency.USDT: self.usdt, Currency.THB: self.thb}[currency]

    def find(self, currency: Currency, bank_name: str) -> Optional[BankBalance]:
        for bank in self.section(currency):
            if bank.matches(bank_name):
                return bank
        return None

    def find_any(self, bank_name: str) -> Optional[tuple[Currency, BankBalance]]:
        """Search all currencies (used by internal transfers)."""
        for currency in Currency:
            bank = self.find(currency, bank_name)
            if bank:
                return currency, bank
        return None

    def copy(self) -> "Balances":
        return copy.deepcopy(self)

    @property
    def is_empty(self) -> bool:
        return not (self.mmk or self.usdt or self.thb)


@dataclass(frozen=True)
class BalanceChange:
    """A single signed delta to apply to one bank. Negative = debit."""

    currency: Currency
    bank_name: str
    delta: float

    @property
    def is_debit(self) -> bool:
        return self.delta < 0


class TxType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    P2P_SELL = "p2p_sell"
    STAFF_P2P_SELL = "staff_p2p_sell"
    INTERNAL_TRANSFER = "internal_transfer"
    COIN_TRANSFER = "coin_transfer"
    BALANCE_LOAD = "balance_load"


@dataclass
class BreakdownEntry:
    """'2,042,960 to San (Wave)' inside a P2P sell message."""

    amount: float
    prefix: str
    bank: str
    bank_name: str


@dataclass
class TxInfo:
    """Parsed transaction intent from a message's text."""

    type: Optional[TxType] = None
    usdt: float = 0.0
    mmk: float = 0.0
    rate: float = 0.0
    fee: float = 0.0
    total_usdt: float = 0.0                 # usdt + fee, for P2P sells
    bank_breakdown: list[BreakdownEntry] = field(default_factory=list)
    dest_bank: str = ""                     # staff P2P sell
    src_bank: str = ""                      # staff P2P sell

    @property
    def is_buy_or_sell(self) -> bool:
        return self.type in (TxType.BUY, TxType.SELL)


# --- OCR result models -----------------------------------------------------

@dataclass
class MmkOcrResult:
    amount: float
    bank: Optional[BankBalance]
    confidence: float = 0.0


@dataclass
class UsdtSentOcr:
    """Staff sent USDT to someone: total spent = amount + network fee."""

    amount: float
    network_fee: float
    total_amount: float
    bank_type: str  # 'swift' | 'wallet' | 'binance'


@dataclass
class UsdtReceivedOcr:
    """Customer sent USDT to us: what lands in our wallet, net of fee."""

    received_amount: float
    network_fee: float
    bank_type: str


@dataclass
class BankMatchOcr:
    """Confidence-scored match of a receipt against registered accounts."""

    amount: float
    confidences: dict[str, float]  # bank_id (str) -> 0..100

    def best(self) -> tuple[Optional[int], float]:
        best_id, best_conf = None, 0.0
        for bank_id, conf in self.confidences.items():
            if conf > best_conf:
                best_conf = conf
                best_id = int(bank_id)
        return best_id, best_conf


# --- Stored records ----------------------------------------------------------

@dataclass
class ReceiptOcrRecord:
    """Pre-scanned sale receipt stored in the DB (survives restarts)."""

    message_id: int
    receipt_index: int
    transaction_type: str
    detected_amount: Optional[float]
    detected_bank: Optional[str]
    detected_usdt: Optional[float]
    media_group_id: Optional[str] = None
