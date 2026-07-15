"""Service container + cross-cutting helpers used by all handlers.

One ``Services`` object is built at startup and stored in
``application.bot_data`` — handlers pull their dependencies from it instead
of reaching for module-level globals (v1 style), which keeps every piece
independently testable.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Optional

from telegram import Bot, PhotoSize, User
from telegram.ext import ContextTypes

from balances import BalanceLedger
from config import Settings
from db import Database
from media_groups import MediaGroupCollector
from models import Balances, BankBalance, MmkOcrResult
from notify import Notifier
from ocr import OcrService
from repositories import (
    AuditRepo,
    BankAccountRepo,
    IdempotencyRepo,
    MediaPhotoRepo,
    OcrCacheRepo,
    SettingsRepo,
    UserRepo,
)

logger = logging.getLogger(__name__)

SERVICES_KEY = "services"


@dataclass
class Services:
    settings: Settings
    db: Database
    users: UserRepo
    app_settings: SettingsRepo
    bank_accounts: BankAccountRepo
    photos: MediaPhotoRepo
    ocr_cache: OcrCacheRepo
    audit: AuditRepo
    idempotency: IdempotencyRepo
    ledger: BalanceLedger
    ocr: OcrService
    notifier: Notifier
    collector: MediaGroupCollector

    @classmethod
    def build(cls, settings: Settings, bot: Bot) -> "Services":
        db = Database(settings)
        audit = AuditRepo(db)
        return cls(
            settings=settings,
            db=db,
            users=UserRepo(db),
            app_settings=SettingsRepo(db),
            bank_accounts=BankAccountRepo(db),
            photos=MediaPhotoRepo(db, settings.media_dir),
            ocr_cache=OcrCacheRepo(db),
            audit=audit,
            idempotency=IdempotencyRepo(db),
            ledger=BalanceLedger(db, audit),
            ocr=OcrService(settings),
            notifier=Notifier(bot, settings),
            collector=MediaGroupCollector(
                quiet_seconds=settings.media_group_quiet_seconds,
                max_wait_seconds=settings.media_group_max_wait_seconds,
            ),
        )

    # ------------------------------------------------------------- helpers

    async def download_photo_b64(self, bot: Bot, photo: PhotoSize) -> str:
        """Download a Telegram photo and return it base64-encoded for OCR."""
        photo_file = await bot.get_file(photo.file_id)
        photo_bytes = await photo_file.download_as_bytearray()
        return base64.b64encode(bytes(photo_bytes)).decode("utf-8")

    async def download_photo_bytes(self, bot: Bot, photo: PhotoSize) -> bytes:
        photo_file = await bot.get_file(photo.file_id)
        return bytes(await photo_file.download_as_bytearray())

    @staticmethod
    def read_file_b64(file_path: str) -> str:
        with open(file_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    async def staff_identity(self, user: User) -> tuple[str, str]:
        """(prefix, display_name). Falls back to username when the user has
        no registered prefix — same behavior as v1."""
        display = user.username or user.first_name or str(user.id)
        prefix = await self.users.get_prefix(user.id)
        return (prefix or display), display

    async def mmk_banks_with_accounts(self, balances: Balances) -> list[dict]:
        """Balance MMK banks joined with their registered account details,
        shaped for confidence-scored OCR matching."""
        result = []
        for idx, bank in enumerate(balances.mmk, 1):
            account = await self.bank_accounts.get_mmk(bank.bank_name)
            result.append(
                {
                    "bank_id": idx,
                    "bank_name": bank.bank_name,
                    "account_number": account["account_number"] if account else "0000",
                    "account_holder": account["account_holder"] if account else "Unknown",
                    "bank_obj": bank,
                }
            )
        return result

    async def usdt_wallets_for_ocr(self) -> list[dict]:
        """Registered USDT wallets shaped for confidence-scored OCR matching."""
        wallets = await self.bank_accounts.list_usdt()
        return [
            {
                "bank_id": idx,
                "bank_name": w["bank_name"],
                "wallet_address": w["wallet_address"],
                "network": w["network"],
            }
            for idx, w in enumerate(wallets, 1)
        ]

    async def detect_mmk_any_bank(
        self, image_base64: str, balances: Balances
    ) -> Optional[MmkOcrResult]:
        """Detect MMK amount + bank against ALL banks (customer receipts).

        Uses confidence matching against registered account numbers/holders
        when available; falls back to visual-only detection otherwise.
        """
        candidates = await self.mmk_banks_with_accounts(balances)
        with_accounts = [c for c in candidates if c["account_number"] != "0000"]

        if with_accounts:
            match = await self.ocr.match_mmk_receipt(image_base64, with_accounts)
            if not match:
                return None
            best_id, best_conf = match.best()
            if best_id and 1 <= best_id <= len(with_accounts):
                return MmkOcrResult(
                    amount=match.amount,
                    bank=with_accounts[best_id - 1]["bank_obj"],
                    confidence=best_conf,
                )
            return None

        result = await self.ocr.detect_mmk_bank_and_amount(image_base64, balances.mmk)
        if result:
            result.confidence = 50.0
        return result

    def find_staff_usdt_bank(
        self, balances: Balances, user_prefix: str
    ) -> Optional[BankBalance]:
        """Staff's USDT account for P2P sells: Binance preferred, else any
        USDT bank carrying the staff's prefix."""
        fallback = None
        for bank in balances.usdt:
            if bank.prefix == user_prefix:
                if "binance" in bank.bank.lower():
                    return bank
                if fallback is None:
                    fallback = bank
        return fallback


def get_services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data[SERVICES_KEY]
