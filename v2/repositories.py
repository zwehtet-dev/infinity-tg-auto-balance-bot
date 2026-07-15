"""Repositories: all SQL lives here, one class per aggregate.

Both SQLite (>= 3.24) and Postgres support ``INSERT ... ON CONFLICT``, so a
single statement covers both dialects — no per-driver forks in business code.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from db import Database
from models import ReceiptOcrRecord

logger = logging.getLogger(__name__)


class UserRepo:
    """Telegram user -> staff prefix (e.g. 123456 -> 'San')."""

    def __init__(self, db: Database):
        self._db = db

    async def get_prefix(self, user_id: int) -> Optional[str]:
        row = await self._db.fetchone(
            "SELECT prefix_name FROM user_prefixes WHERE user_id = ?", (user_id,)
        )
        return row[0] if row else None

    async def set_prefix(self, user_id: int, prefix: str, username: Optional[str]) -> None:
        await self._db.execute(
            """INSERT INTO user_prefixes (user_id, prefix_name, username)
               VALUES (?, ?, ?)
               ON CONFLICT (user_id)
               DO UPDATE SET prefix_name = excluded.prefix_name, username = excluded.username""",
            (user_id, prefix, username),
        )
        logger.info("Set prefix %r for user %s (@%s)", prefix, user_id, username)

    async def remove(self, user_id: int) -> bool:
        return await self._db.execute(
            "DELETE FROM user_prefixes WHERE user_id = ?", (user_id,)
        ) > 0

    async def list_all(self) -> list[dict[str, Any]]:
        rows = await self._db.fetchall(
            "SELECT user_id, prefix_name, username FROM user_prefixes ORDER BY prefix_name"
        )
        return [{"user_id": r[0], "prefix_name": r[1], "username": r[2]} for r in rows]


class SettingsRepo:
    """Key/value settings (currently the default receiving USDT account)."""

    RECEIVING_USDT = "receiving_usdt_account"
    DEFAULT_RECEIVING_USDT = "ACT(Wallet)"

    def __init__(self, db: Database):
        self._db = db

    async def get(self, key: str, default: str = "") -> str:
        row = await self._db.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return row[0] if row else default

    async def set(self, key: str, value: str) -> None:
        await self._db.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT (key)
               DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value),
        )

    async def get_receiving_usdt_account(self) -> str:
        return await self.get(self.RECEIVING_USDT, self.DEFAULT_RECEIVING_USDT)

    async def set_receiving_usdt_account(self, account: str) -> None:
        await self.set(self.RECEIVING_USDT, account)


class BankAccountRepo:
    """Registered MMK bank accounts and USDT wallets (OCR verification data)."""

    def __init__(self, db: Database):
        self._db = db

    # --- MMK ---------------------------------------------------------------

    async def set_mmk(self, bank_name: str, account_number: str, account_holder: str) -> None:
        await self._db.execute(
            """INSERT INTO mmk_bank_accounts (bank_name, account_number, account_holder, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT (bank_name)
               DO UPDATE SET account_number = excluded.account_number,
                             account_holder = excluded.account_holder,
                             updated_at = excluded.updated_at""",
            (bank_name, account_number, account_holder),
        )

    async def get_mmk(self, bank_name: str) -> Optional[dict[str, str]]:
        row = await self._db.fetchone(
            "SELECT account_number, account_holder FROM mmk_bank_accounts WHERE bank_name = ?",
            (bank_name,),
        )
        return {"account_number": row[0], "account_holder": row[1]} if row else None

    async def list_mmk(self) -> list[dict[str, str]]:
        rows = await self._db.fetchall(
            "SELECT bank_name, account_number, account_holder FROM mmk_bank_accounts ORDER BY bank_name"
        )
        return [
            {"bank_name": r[0], "account_number": r[1], "account_holder": r[2]} for r in rows
        ]

    async def remove_mmk(self, bank_name: str) -> bool:
        return await self._db.execute(
            "DELETE FROM mmk_bank_accounts WHERE bank_name = ?", (bank_name,)
        ) > 0

    # --- USDT ---------------------------------------------------------------

    async def set_usdt(self, bank_name: str, wallet_address: str, network: str) -> None:
        await self._db.execute(
            """INSERT INTO usdt_bank_accounts (bank_name, wallet_address, network, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT (bank_name)
               DO UPDATE SET wallet_address = excluded.wallet_address,
                             network = excluded.network,
                             updated_at = excluded.updated_at""",
            (bank_name, wallet_address, network),
        )

    async def get_usdt(self, bank_name: str) -> Optional[dict[str, str]]:
        row = await self._db.fetchone(
            "SELECT wallet_address, network FROM usdt_bank_accounts WHERE bank_name = ?",
            (bank_name,),
        )
        return {"wallet_address": row[0], "network": row[1]} if row else None

    async def list_usdt(self) -> list[dict[str, str]]:
        rows = await self._db.fetchall(
            "SELECT bank_name, wallet_address, network FROM usdt_bank_accounts ORDER BY bank_name"
        )
        return [{"bank_name": r[0], "wallet_address": r[1], "network": r[2]} for r in rows]

    async def remove_usdt(self, bank_name: str) -> bool:
        return await self._db.execute(
            "DELETE FROM usdt_bank_accounts WHERE bank_name = ?", (bank_name,)
        ) > 0

    # --- Seeding -------------------------------------------------------------

    async def seed_defaults(self, mmk: list[tuple], usdt: list[tuple]) -> None:
        """Seed initial accounts only into an *empty* table.

        v1 re-inserted defaults on every startup, silently resurrecting
        accounts an admin had deliberately removed.
        """
        row = await self._db.fetchone("SELECT COUNT(*) FROM mmk_bank_accounts")
        if row and row[0] == 0 and mmk:
            await self._db.executemany(
                "INSERT INTO mmk_bank_accounts (bank_name, account_number, account_holder) VALUES (?, ?, ?)",
                mmk,
            )
            logger.info("Seeded %d default MMK bank accounts", len(mmk))
        row = await self._db.fetchone("SELECT COUNT(*) FROM usdt_bank_accounts")
        if row and row[0] == 0 and usdt:
            await self._db.executemany(
                "INSERT INTO usdt_bank_accounts (bank_name, wallet_address, network) VALUES (?, ?, ?)",
                usdt,
            )
            logger.info("Seeded %d default USDT wallets", len(usdt))


class MediaPhotoRepo:
    """Downloaded media-group photos, on disk + indexed in the DB."""

    def __init__(self, db: Database, media_dir: str):
        self._db = db
        self._dir = media_dir
        os.makedirs(media_dir, exist_ok=True)

    async def save(self, media_group_id: str, message_id: int, photo_bytes: bytes) -> str:
        file_path = os.path.join(self._dir, f"{media_group_id}_{message_id}.jpg")
        with open(file_path, "wb") as f:
            f.write(photo_bytes)
        await self._db.execute(
            """INSERT INTO media_group_photos (media_group_id, message_id, file_path)
               VALUES (?, ?, ?)
               ON CONFLICT (media_group_id, message_id)
               DO UPDATE SET file_path = excluded.file_path""",
            (media_group_id, message_id, file_path),
        )
        return file_path

    async def get_group(self, media_group_id: str) -> list[tuple[int, str]]:
        """[(message_id, file_path)] ordered by message id."""
        rows = await self._db.fetchall(
            "SELECT message_id, file_path FROM media_group_photos WHERE media_group_id = ? ORDER BY message_id",
            (media_group_id,),
        )
        return [(r[0], r[1]) for r in rows]

    async def get_group_by_message(self, message_id: int) -> tuple[Optional[str], list[tuple[int, str]]]:
        row = await self._db.fetchone(
            "SELECT media_group_id FROM media_group_photos WHERE message_id = ?", (message_id,)
        )
        if not row:
            return None, []
        return row[0], await self.get_group(row[0])

    async def has_photo(self, media_group_id: str, message_id: int) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM media_group_photos WHERE media_group_id = ? AND message_id = ?",
            (media_group_id, message_id),
        )
        return row is not None

    async def delete_group(self, media_group_id: str) -> None:
        photos = await self.get_group(media_group_id)
        for _, file_path in photos:
            try:
                os.remove(file_path)
            except OSError:
                pass
        await self._db.execute(
            "DELETE FROM media_group_photos WHERE media_group_id = ?", (media_group_id,)
        )

    async def cleanup_older_than(self, hours: int) -> int:
        rows = await self._db.fetchall(
            self._age_filter("SELECT file_path FROM media_group_photos", hours)
        )
        for (file_path,) in rows:
            try:
                os.remove(file_path)
            except OSError:
                pass
        return await self._db.execute(
            self._age_filter("DELETE FROM media_group_photos", hours)
        )

    def _age_filter(self, prefix: str, hours: int) -> str:
        if self._db.is_postgres:
            return f"{prefix} WHERE created_at < NOW() - INTERVAL '{int(hours)} hours'"
        return f"{prefix} WHERE created_at < datetime('now', '-{int(hours)} hours')"


class OcrCacheRepo:
    """Pre-scanned sale-receipt OCR results (avoid re-OCR on staff reply)."""

    def __init__(self, db: Database):
        self._db = db

    async def save(self, record: ReceiptOcrRecord, raw: Optional[dict] = None) -> None:
        await self._db.execute(
            """INSERT INTO sale_receipt_ocr
                   (message_id, media_group_id, receipt_index, transaction_type,
                    detected_amount, detected_bank, detected_usdt, ocr_raw_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (message_id, receipt_index)
               DO UPDATE SET transaction_type = excluded.transaction_type,
                             detected_amount = excluded.detected_amount,
                             detected_bank = excluded.detected_bank,
                             detected_usdt = excluded.detected_usdt,
                             ocr_raw_data = excluded.ocr_raw_data,
                             media_group_id = excluded.media_group_id""",
            (
                record.message_id,
                record.media_group_id,
                record.receipt_index,
                record.transaction_type,
                record.detected_amount,
                record.detected_bank,
                record.detected_usdt,
                json.dumps(raw or {}),
            ),
        )

    _COLS = "message_id, receipt_index, transaction_type, detected_amount, detected_bank, detected_usdt, media_group_id"

    def _to_record(self, row: tuple) -> ReceiptOcrRecord:
        return ReceiptOcrRecord(
            message_id=row[0],
            receipt_index=row[1],
            transaction_type=row[2],
            detected_amount=row[3],
            detected_bank=row[4],
            detected_usdt=row[5],
            media_group_id=row[6],
        )

    async def get_by_message(self, message_id: int) -> list[ReceiptOcrRecord]:
        rows = await self._db.fetchall(
            f"SELECT {self._COLS} FROM sale_receipt_ocr WHERE message_id = ? ORDER BY receipt_index",
            (message_id,),
        )
        return [self._to_record(r) for r in rows]

    async def get_by_media_group(self, media_group_id: str) -> list[ReceiptOcrRecord]:
        rows = await self._db.fetchall(
            f"SELECT {self._COLS} FROM sale_receipt_ocr WHERE media_group_id = ? "
            "ORDER BY message_id, receipt_index",
            (media_group_id,),
        )
        return [self._to_record(r) for r in rows]

    async def delete_by_message(self, message_id: int) -> None:
        await self._db.execute("DELETE FROM sale_receipt_ocr WHERE message_id = ?", (message_id,))

    async def delete_by_media_group(self, media_group_id: str) -> None:
        await self._db.execute(
            "DELETE FROM sale_receipt_ocr WHERE media_group_id = ?", (media_group_id,)
        )

    async def cleanup_older_than(self, hours: int) -> int:
        if self._db.is_postgres:
            query = f"DELETE FROM sale_receipt_ocr WHERE created_at < NOW() - INTERVAL '{int(hours)} hours'"
        else:
            query = f"DELETE FROM sale_receipt_ocr WHERE created_at < datetime('now', '-{int(hours)} hours')"
        return await self._db.execute(query)


class AuditRepo:
    """Append-only audit trail: who changed which balance, when, and why."""

    def __init__(self, db: Database):
        self._db = db

    async def record(
        self,
        tx_type: str,
        description: str,
        changes: list[dict[str, Any]],
        actor_id: Optional[int] = None,
        actor_name: Optional[str] = None,
        ref_message_id: Optional[int] = None,
    ) -> None:
        await self._db.execute(
            """INSERT INTO audit_log (tx_type, actor_id, actor_name, ref_message_id, description, changes_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (tx_type, actor_id, actor_name, ref_message_id, description, json.dumps(changes)),
        )

    async def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self._db.fetchall(
            """SELECT created_at, tx_type, actor_name, ref_message_id, description
               FROM audit_log ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        return [
            {
                "created_at": r[0],
                "tx_type": r[1],
                "actor_name": r[2],
                "ref_message_id": r[3],
                "description": r[4],
            }
            for r in rows
        ]

    async def cleanup_older_than_days(self, days: int) -> int:
        if self._db.is_postgres:
            query = f"DELETE FROM audit_log WHERE created_at < NOW() - INTERVAL '{int(days)} days'"
        else:
            query = f"DELETE FROM audit_log WHERE created_at < datetime('now', '-{int(days)} days')"
        return await self._db.execute(query)


class IdempotencyRepo:
    """At-most-once processing guard for (message, action) pairs."""

    def __init__(self, db: Database):
        self._db = db

    async def try_claim(self, message_id: int, kind: str) -> bool:
        """True if this call claimed the work; False if already processed."""
        affected = await self._db.execute(
            "INSERT INTO processed_messages (message_id, kind) VALUES (?, ?) "
            "ON CONFLICT (message_id, kind) DO NOTHING",
            (message_id, kind),
        )
        return affected > 0

    async def release(self, message_id: int, kind: str) -> None:
        """Undo a claim after a failure, so a retry can process the message."""
        await self._db.execute(
            "DELETE FROM processed_messages WHERE message_id = ? AND kind = ?",
            (message_id, kind),
        )

    async def cleanup_older_than(self, hours: int) -> int:
        if self._db.is_postgres:
            query = f"DELETE FROM processed_messages WHERE processed_at < NOW() - INTERVAL '{int(hours)} hours'"
        else:
            query = f"DELETE FROM processed_messages WHERE processed_at < datetime('now', '-{int(hours)} hours')"
        return await self._db.execute(query)
