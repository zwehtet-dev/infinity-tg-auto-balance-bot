"""Database access layer.

Design goals:
- **Non-blocking**: sync drivers (sqlite3 / psycopg) run in a worker thread via
  ``asyncio.to_thread`` so OCR timers, polling and handlers never stall on I/O
  (v1 ran DB calls directly on the event loop).
- **One dialect seam**: queries are written once with ``?`` placeholders and
  translated for Postgres, instead of duplicating every statement.
- **Reliability**: each operation is one connection + one transaction with a
  single retry on transient failures; schema creation is idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any, Iterable, Optional, Sequence

from config import Settings

logger = logging.getLogger(__name__)

try:  # psycopg is only required when DATABASE_URL points at Postgres
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


class Database:
    def __init__(self, settings: Settings):
        self._settings = settings
        self.is_postgres = settings.uses_postgres
        if self.is_postgres and psycopg is None:
            raise RuntimeError("DATABASE_URL is Postgres but psycopg is not installed")

    # ------------------------------------------------------------------ core

    def _connect(self):
        if self.is_postgres:
            return psycopg.connect(self._settings.database_url)
        conn = sqlite3.connect(self._settings.sqlite_db_file, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")     # readers don't block writers
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _adapt(self, query: str) -> str:
        return query.replace("?", "%s") if self.is_postgres else query

    def _run(self, work, attempts: int = 2):
        """Run ``work(conn)`` with commit/rollback and one retry on transient errors."""
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                conn = self._connect()
                try:
                    result = work(conn)
                    conn.commit()
                    return result
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    raise
                finally:
                    conn.close()
            except (sqlite3.OperationalError, *((psycopg.OperationalError,) if psycopg else ())) as e:
                last_error = e
                logger.warning("DB transient error (attempt %d/%d): %s", attempt, attempts, e)
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
        raise last_error  # type: ignore[misc]

    # --------------------------------------------------------------- async API

    async def execute(self, query: str, params: Sequence[Any] = ()) -> int:
        """Run one statement; returns affected rowcount."""
        def work(conn):
            cur = conn.cursor()
            cur.execute(self._adapt(query), tuple(params))
            return cur.rowcount

        return await asyncio.to_thread(self._run, work)

    async def executemany(self, query: str, rows: Iterable[Sequence[Any]]) -> None:
        rows = [tuple(r) for r in rows]
        if not rows:
            return

        def work(conn):
            cur = conn.cursor()
            cur.executemany(self._adapt(query), rows)

        await asyncio.to_thread(self._run, work)

    async def fetchall(self, query: str, params: Sequence[Any] = ()) -> list[tuple]:
        def work(conn):
            cur = conn.cursor()
            cur.execute(self._adapt(query), tuple(params))
            return cur.fetchall()

        return await asyncio.to_thread(self._run, work)

    async def fetchone(self, query: str, params: Sequence[Any] = ()) -> Optional[tuple]:
        def work(conn):
            cur = conn.cursor()
            cur.execute(self._adapt(query), tuple(params))
            return cur.fetchone()

        return await asyncio.to_thread(self._run, work)

    async def transaction(self, statements: list[tuple[str, Sequence[Any]]]) -> None:
        """Run several statements atomically — all succeed or none apply.

        This backs the balance ledger: a transfer's debit and credit can never
        be persisted half-done.
        """
        def work(conn):
            cur = conn.cursor()
            for query, params in statements:
                cur.execute(self._adapt(query), tuple(params))

        await asyncio.to_thread(self._run, work)

    # ----------------------------------------------------------------- schema

    async def init_schema(self) -> None:
        pk_auto = "SERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        big_int = "BIGINT" if self.is_postgres else "INTEGER"

        statements = [
            f"""CREATE TABLE IF NOT EXISTS user_prefixes (
                    user_id {big_int} PRIMARY KEY,
                    prefix_name TEXT NOT NULL,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
            """CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
            f"""CREATE TABLE IF NOT EXISTS mmk_bank_accounts (
                    id {pk_auto},
                    bank_name TEXT NOT NULL UNIQUE,
                    account_number TEXT NOT NULL,
                    account_holder TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
            f"""CREATE TABLE IF NOT EXISTS usdt_bank_accounts (
                    id {pk_auto},
                    bank_name TEXT NOT NULL UNIQUE,
                    wallet_address TEXT NOT NULL,
                    network TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
            f"""CREATE TABLE IF NOT EXISTS media_group_photos (
                    id {pk_auto},
                    media_group_id TEXT NOT NULL,
                    message_id {big_int} NOT NULL,
                    file_path TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(media_group_id, message_id)
                )""",
            "CREATE INDEX IF NOT EXISTS idx_media_group_id ON media_group_photos(media_group_id)",
            "CREATE INDEX IF NOT EXISTS idx_message_id ON media_group_photos(message_id)",
            f"""CREATE TABLE IF NOT EXISTS sale_receipt_ocr (
                    id {pk_auto},
                    message_id {big_int} NOT NULL,
                    media_group_id TEXT,
                    receipt_index INTEGER DEFAULT 0,
                    transaction_type TEXT,
                    detected_amount REAL,
                    detected_bank TEXT,
                    detected_usdt REAL,
                    ocr_raw_data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(message_id, receipt_index)
                )""",
            "CREATE INDEX IF NOT EXISTS idx_sale_receipt_message_id ON sale_receipt_ocr(message_id)",
            "CREATE INDEX IF NOT EXISTS idx_sale_receipt_media_group ON sale_receipt_ocr(media_group_id)",
            # --- New in v2 ---------------------------------------------------
            # Balances persist across restarts (v1 kept them only in memory).
            """CREATE TABLE IF NOT EXISTS balances (
                    currency TEXT NOT NULL,
                    bank_name TEXT NOT NULL,
                    prefix TEXT NOT NULL,
                    bank TEXT NOT NULL,
                    amount REAL NOT NULL,
                    position INTEGER NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (currency, bank_name)
                )""",
            # Immutable audit trail of every balance mutation.
            f"""CREATE TABLE IF NOT EXISTS audit_log (
                    id {pk_auto},
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tx_type TEXT NOT NULL,
                    actor_id {big_int},
                    actor_name TEXT,
                    ref_message_id {big_int},
                    description TEXT NOT NULL,
                    changes_json TEXT NOT NULL
                )""",
            # Idempotency guard: a (message, action) pair is processed once,
            # even if Telegram redelivers the update or two tasks race.
            f"""CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id {big_int} NOT NULL,
                    kind TEXT NOT NULL,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (message_id, kind)
                )""",
        ]
        for statement in statements:
            def work(conn, s=statement):
                conn.cursor().execute(s)

            await asyncio.to_thread(self._run, work)

        logger.info("Database schema ready (%s)", "postgres" if self.is_postgres else "sqlite")
