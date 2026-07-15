"""Typed, validated configuration loaded once at startup.

All settings come from environment variables (optionally via a .env file).
Secrets are never logged; ``Settings.summary()`` is safe to print.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env_int(name: str, default: int = 0) -> int:
    value = os.getenv(name, "")
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, "")
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _env_int_set(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                raise ConfigError(f"{name} contains a non-integer entry: {part!r}")
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    # --- Required ---
    telegram_bot_token: str
    openai_api_key: str
    target_group_id: int

    # --- Topics (0 = disabled / main chat) ---
    usdt_transfers_topic_id: int = 0
    auto_balance_topic_id: int = 0
    accounts_matter_topic_id: int = 0
    alert_topic_id: int = 0

    # --- Storage ---
    database_url: str = ""              # postgres URL, empty = SQLite
    sqlite_db_file: str = "bot_data.db"
    media_dir: str = "media_group_photos"

    # --- Security ---
    # Empty set = every group member may run admin commands (v1-compatible,
    # a warning is logged). Non-empty = only these user ids may run them.
    admin_user_ids: frozenset[int] = field(default_factory=frozenset)

    # --- Tuning ---
    ocr_model: str = "gpt-4o"
    ocr_timeout_seconds: float = 60.0
    ocr_max_attempts: int = 3
    ocr_max_concurrency: int = 4
    media_group_quiet_seconds: float = 2.5   # debounce: flush after this much quiet
    media_group_max_wait_seconds: float = 20.0
    telegram_timeout_seconds: float = 60.0
    drop_pending_updates: bool = True
    cleanup_interval_seconds: int = 3600
    photo_retention_hours: int = 24
    ocr_cache_retention_hours: int = 48
    audit_retention_days: int = 365

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        group_id = _env_int("TARGET_GROUP_ID")

        missing = [
            name
            for name, ok in (
                ("TELEGRAM_BOT_TOKEN", bool(token)),
                ("OPENAI_API_KEY", bool(openai_key)),
                ("TARGET_GROUP_ID", group_id != 0),
            )
            if not ok
        ]
        if missing:
            raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            telegram_bot_token=token,
            openai_api_key=openai_key,
            target_group_id=group_id,
            usdt_transfers_topic_id=_env_int("USDT_TRANSFERS_TOPIC_ID"),
            auto_balance_topic_id=_env_int("AUTO_BALANCE_TOPIC_ID"),
            accounts_matter_topic_id=_env_int("ACCOUNTS_MATTER_TOPIC_ID"),
            alert_topic_id=_env_int("ALERT_TOPIC_ID"),
            database_url=os.getenv("DATABASE_URL", "").strip(),
            sqlite_db_file=os.getenv("SQLITE_DB_FILE", "bot_data.db"),
            media_dir=os.getenv("MEDIA_GROUP_DIR", "media_group_photos"),
            admin_user_ids=_env_int_set("ADMIN_USER_IDS"),
            ocr_model=os.getenv("OCR_MODEL", "gpt-4o"),
            ocr_timeout_seconds=_env_float("OCR_TIMEOUT_SECONDS", 60.0),
            ocr_max_attempts=max(1, _env_int("OCR_MAX_ATTEMPTS", 3)),
            ocr_max_concurrency=max(1, _env_int("OCR_MAX_CONCURRENCY", 4)),
            media_group_quiet_seconds=_env_float("MEDIA_GROUP_QUIET_SECONDS", 2.5),
            media_group_max_wait_seconds=_env_float("MEDIA_GROUP_MAX_WAIT_SECONDS", 20.0),
            telegram_timeout_seconds=_env_float("TELEGRAM_TIMEOUT_SECONDS", 60.0),
            drop_pending_updates=_env_bool("DROP_PENDING_UPDATES", True),
            cleanup_interval_seconds=max(60, _env_int("CLEANUP_INTERVAL_SECONDS", 3600)),
            photo_retention_hours=max(1, _env_int("PHOTO_RETENTION_HOURS", 24)),
            ocr_cache_retention_hours=max(1, _env_int("OCR_CACHE_RETENTION_HOURS", 48)),
            audit_retention_days=max(1, _env_int("AUDIT_RETENTION_DAYS", 365)),
        )

    @property
    def uses_postgres(self) -> bool:
        return self.database_url.startswith("postgres")

    def is_admin(self, user_id: int) -> bool:
        """Empty allowlist keeps v1 behavior (anyone may administer)."""
        return not self.admin_user_ids or user_id in self.admin_user_ids

    def summary(self) -> str:
        """Loggable, secret-free configuration overview."""
        return (
            f"group={self.target_group_id} "
            f"topics(usdt={self.usdt_transfers_topic_id or 'main'}, "
            f"balance={self.auto_balance_topic_id or 'main'}, "
            f"accounts={self.accounts_matter_topic_id or '-'}, "
            f"alert={self.alert_topic_id or 'reply'}) "
            f"db={'postgres' if self.uses_postgres else 'sqlite'} "
            f"admins={'open' if not self.admin_user_ids else len(self.admin_user_ids)}"
        )
