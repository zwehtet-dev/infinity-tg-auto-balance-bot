"""Entrypoint: wiring, startup/shutdown, background maintenance.

Startup sequence:
1. Load + validate config (fail fast with a clear message).
2. Initialize DB schema (idempotent) and seed default accounts if empty.
3. Restore the last persisted balance sheet — the bot resumes exactly where
   it stopped, without waiting for someone to re-post a balance message.
4. Register handlers, start polling with generous network timeouts.

A background task periodically prunes stored photos, OCR cache entries,
idempotency claims, and old audit rows.
"""

from __future__ import annotations

import asyncio
import logging
import traceback

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import ConfigError, Settings
from handlers import commands
from handlers.router import handle_message
from services import SERVICES_KEY, Services

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every API call
logger = logging.getLogger(__name__)

# Seed data used only when the account tables are completely empty.
DEFAULT_MMK_BANKS = [
    ("San(CB)", "0225100900026042", "Chaw Su Thu Zar"),
    ("San(KBZ)", "27251127201844001", "CHAW SU THU ZAR"),
    ("San(Yoma)", "007011118014339", "Daw Chaw Su Thu Zar"),
    ("San(Kpay P)", "300948464", "Chaw Su"),
    ("San(AYA)", "40038204256", "CHAW SU THU ZAR"),
]
DEFAULT_USDT_WALLETS = [
    ("ACT(BNB Wallet)", "0x640e9AEde10B610834876cCc0ef2576C9469CB0e", "BNB"),
    ("ACT(Tron Wallet)", "TCFKANz7vhaMLtxjTSYSZRRGdVivNNPDEy", "Tron"),
    ("ACT(SOL Wallet)", "EECRtME4j6uqd3GsjbkoWhKuYxX2V7LCcHjwP3y5JPnD", "SOL"),
    ("ACT(TON Wallet)", "UQBkM-eV3JW6pzFaf_JGvTewOEw6nl38lXIdnDMF3H8UpRCQ", "TON"),
]


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(
        "Unhandled exception while processing update:\n%s",
        "".join(traceback.format_exception(context.error)) if context.error else "unknown",
    )
    services: Services = context.application.bot_data.get(SERVICES_KEY)
    if services:
        try:
            await services.notifier.status(
                "❌ An internal error occurred while processing a message. "
                "The balance sheet was NOT modified unless a success message was posted. "
                "Check the logs for details.",
                parse_mode=None,
            )
        except Exception:
            logger.error("Failed to report error to the group")


async def _maintenance_loop(services: Services) -> None:
    settings = services.settings
    while True:
        await asyncio.sleep(settings.cleanup_interval_seconds)
        try:
            photos = await services.photos.cleanup_older_than(settings.photo_retention_hours)
            ocr = await services.ocr_cache.cleanup_older_than(settings.ocr_cache_retention_hours)
            claims = await services.idempotency.cleanup_older_than(72)
            audits = await services.audit.cleanup_older_than_days(settings.audit_retention_days)
            logger.info(
                "Maintenance: removed %s photos, %s OCR rows, %s claims, %s audit rows",
                photos, ocr, claims, audits,
            )
        except Exception as e:
            logger.error("Maintenance run failed: %s", e)


def _post_init_factory(settings: Settings):
    async def post_init(application: Application) -> None:
        services = Services.build(settings, application.bot)
        application.bot_data[SERVICES_KEY] = services

        await services.db.init_schema()
        await services.bank_accounts.seed_defaults(DEFAULT_MMK_BANKS, DEFAULT_USDT_WALLETS)

        restored = await services.ledger.load_from_db()
        if restored:
            logger.info("✅ Balance sheet restored from database")
        else:
            logger.info("ℹ️ No persisted balances — waiting for a balance message or /load")

        application.create_task(_maintenance_loop(services))
        logger.info("🤖 Infinity Balance Bot v2 started — %s", settings.summary())

    return post_init


def main() -> None:
    # python-telegram-bot 21.x calls asyncio.get_event_loop() during
    # run_polling and relies on it auto-creating a loop when none exists.
    # That behaviour was removed in Python 3.12+ (a hard RuntimeError on
    # 3.14). Ensure a loop exists in the main thread before PTB looks for it.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    try:
        settings = Settings.load()
    except ConfigError as e:
        raise SystemExit(f"Configuration error: {e}")

    timeout = settings.telegram_timeout_seconds
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .connect_timeout(timeout)
        .read_timeout(timeout)
        .write_timeout(timeout)
        .pool_timeout(timeout)
        .get_updates_connect_timeout(timeout)
        .get_updates_read_timeout(timeout)
        .get_updates_write_timeout(timeout)
        .get_updates_pool_timeout(timeout)
        .post_init(_post_init_factory(settings))
        .build()
    )

    app.add_error_handler(error_handler)

    for name, callback in [
        ("start", commands.start_command),
        ("help", commands.start_command),
        ("health", commands.health_command),
        ("audit", commands.audit_command),
        ("test", commands.test_command),
        ("balance", commands.balance_command),
        ("load", commands.load_command),
        ("set_user", commands.set_user_command),
        ("list_users", commands.list_users_command),
        ("remove_user", commands.remove_user_command),
        ("set_receiving_usdt_acc", commands.set_receiving_usdt_acc_command),
        ("show_receiving_usdt_acc", commands.show_receiving_usdt_acc_command),
        ("set_mmk_bank", commands.set_mmk_bank_command),
        ("edit_mmk_bank", commands.edit_mmk_bank_command),
        ("remove_mmk_bank", commands.remove_mmk_bank_command),
        ("list_mmk_bank", commands.list_mmk_bank_command),
        ("set_usdt_bank", commands.set_usdt_bank_command),
        ("edit_usdt_bank", commands.edit_usdt_bank_command),
        ("remove_usdt_bank", commands.remove_usdt_bank_command),
        ("list_usdt_banks", commands.list_usdt_banks_command),
    ]:
        app.add_handler(CommandHandler(name, callback))

    app.add_handler(MessageHandler(filters.ALL, handle_message))

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=settings.drop_pending_updates,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
