"""Message routing — the single entry point for every non-command message.

Routing order (identical to v1, documented in the repo's botflow.md):

1. Ignore anything outside the target group.
2. Auto Balance topic  → (re)load the balance sheet.
3. Accounts Matter topic → internal transfers.
4. USDT Transfers topic (or main chat) — reply-aware location check:
   a. pre-scan sale messages (photo, not a reply, Buy/Sell text),
   b. staff P2P sell (text only),
   c. P2P sell with fee (breakdown, single photo, or album),
   d. regular Buy/Sell settlement (photo reply, single or album).

Albums are collected through the debounced MediaGroupCollector instead of
v1's fixed sleeps.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Message, Update
from telegram.ext import ContextTypes

from balances import parse_balance_message
from handlers.internal import handle_accounts_matter_message
from handlers.p2p import (
    process_p2p_sell_with_breakdown,
    process_p2p_sell_with_photos,
    process_staff_p2p_sell,
)
from handlers.prescan import prescan_sale_media_group, prescan_sale_message
from handlers.transactions import process_buy_sell
from models import TxType
from parsing import extract_transaction_info
from services import Services, get_services

logger = logging.getLogger(__name__)

GENERAL_TOPIC = 1  # Telegram forum: the General topic / plain main chat


def _thread_id(message: Message) -> int:
    return message.message_thread_id if message.message_thread_id is not None else GENERAL_TOPIC


def _is_valid_transfer_location(message: Message, usdt_topic_id: int) -> bool:
    """Reply-aware location check.

    In forum groups a reply's thread_id becomes the replied-to message's id,
    so for replies we check where the ORIGINAL message lives.
    """
    topic_mode = bool(usdt_topic_id and usdt_topic_id > GENERAL_TOPIC)
    if message.reply_to_message:
        original_thread = _thread_id(message.reply_to_message)
        return original_thread == (usdt_topic_id if topic_mode else GENERAL_TOPIC)
    current_thread = _thread_id(message)
    return current_thread == (usdt_topic_id if topic_mode else GENERAL_TOPIC)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    settings = svc.settings
    message = update.message
    if not message or message.chat.id != settings.target_group_id:
        return

    msg_kind = "text" if message.text else ("photo" if message.photo else "other")
    logger.info(
        "Received %s message: thread=%s user=%s",
        msg_kind,
        message.message_thread_id,
        message.from_user.id if message.from_user else "anonymous",
    )

    # --- 1. Auto Balance topic: (re)load the sheet --------------------------
    if settings.auto_balance_topic_id and message.message_thread_id == settings.auto_balance_topic_id:
        if message.text and "USDT" in message.text:
            balances = parse_balance_message(message.text)
            if balances:
                user = message.from_user
                await svc.ledger.load(
                    balances,
                    actor_id=user.id if user else None,
                    actor_name=(user.username or user.first_name) if user else None,
                    ref_message_id=message.message_id,
                )
                logger.info("Balance loaded from auto balance topic")
        return

    # Transactions need an accountable sender (anonymous admins don't have one).
    if not message.from_user:
        logger.info("Skipping: message has no from_user (anonymous/channel post)")
        return

    # --- 2. Accounts Matter topic: internal transfers ------------------------
    if (
        settings.accounts_matter_topic_id
        and message.message_thread_id == settings.accounts_matter_topic_id
    ):
        await handle_accounts_matter_message(update, context)
        return

    # --- 3. USDT transfers topic / main chat ---------------------------------
    if not _is_valid_transfer_location(message, settings.usdt_transfers_topic_id):
        logger.info("Skipping: wrong location (thread %s)", _thread_id(message))
        return

    has_photo = bool(message.photo)
    is_reply = bool(message.reply_to_message)
    text = message.text or message.caption or ""

    # --- 3a. Pre-scan sale messages ------------------------------------------
    if has_photo and not is_reply:
        if message.media_group_id:
            # Persist album photos so a staff reply can settle against all of
            # them, even after a restart — and feed the prescan debounce.
            await _store_album_photo(svc, context, message)
            svc.collector.add(f"prescan:{message.media_group_id}", message.message_id)

        tx_check = extract_transaction_info(text)
        if tx_check.is_buy_or_sell and "fee" not in text.lower():
            if message.media_group_id:
                if text:  # captioned first photo starts the debounce
                    await _schedule_album_prescan(svc, context, message, tx_check)
            else:
                asyncio.create_task(prescan_sale_message(svc, context, message, tx_check))
            # fall through — staff may still reply to this message later

    # --- 3b. Staff P2P sell (text-only shorthand) -----------------------------
    if text.strip().lower().startswith("p2p sell"):
        tx_info = extract_transaction_info(text)
        if tx_info.type == TxType.STAFF_P2P_SELL:
            await process_staff_p2p_sell(update, context, tx_info)
            return

    # --- 3c. P2P sell with fee -------------------------------------------------
    if "fee" in text.lower():
        tx_info = extract_transaction_info(text)
        if tx_info.type == TxType.P2P_SELL:
            if tx_info.bank_breakdown:
                await process_p2p_sell_with_breakdown(update, context, tx_info)
                return
            if has_photo:
                if message.media_group_id:
                    key = f"p2p:{message.media_group_id}"

                    async def flush_p2p(items: list, meta: dict) -> None:
                        await process_p2p_sell_with_photos(
                            update, context, meta["tx_info"], items, message=meta["message"]
                        )

                    svc.collector.start(
                        key, message.photo[-1], {"tx_info": tx_info, "message": message}, flush_p2p
                    )
                    return
                await process_p2p_sell_with_photos(update, context, tx_info, [message.photo[-1]])
                return
            await svc.notifier.alert(
                "❌ P2P Sell requires either photos (for OCR) or a bank breakdown in the message",
                reply_to=message,
            )
            return

    # --- 3d. Caption-less photos of a pending P2P album -----------------------
    if has_photo and message.media_group_id:
        if svc.collector.add(f"p2p:{message.media_group_id}", message.photo[-1]):
            return

    # --- 3e. Regular Buy/Sell settlement: requires a photo reply --------------
    if not is_reply or not has_photo:
        logger.info("Skipping: not a photo reply")
        return

    original = message.reply_to_message
    if original.media_group_id:
        await _recover_original_album(svc, context, original)

    if message.media_group_id:
        # Staff replied with an album of receipts.
        key = f"staff:{message.media_group_id}"
        if svc.collector.add(key, message.photo[-1]):
            return

        original_text = _resolve_transaction_text(message)
        if not original_text:
            logger.info("Skipping staff album: no transaction text found")
            return
        tx_info = extract_transaction_info(original_text)
        if tx_info.type == TxType.STAFF_P2P_SELL:
            await process_staff_p2p_sell(update, context, tx_info)
            return
        if not tx_info.is_buy_or_sell:
            logger.info("Skipping staff album: not a Buy/Sell transaction")
            return

        async def flush_staff(items: list, meta: dict) -> None:
            await process_buy_sell(
                update, context, meta["tx_info"], items, message=meta["message"]
            )

        svc.collector.start(
            key, message.photo[-1], {"tx_info": tx_info, "message": message}, flush_staff
        )
        return

    # Single-photo reply.
    original_text = _resolve_transaction_text(message)
    if not original_text:
        logger.info("Skipping: original message has no text")
        return
    tx_info = extract_transaction_info(original_text)
    if not tx_info.is_buy_or_sell:
        logger.info("Skipping: not a Buy/Sell transaction message")
        return
    logger.info(
        "Processing %s: %.4f USDT = %.0f MMK", tx_info.type.value, tx_info.usdt, tx_info.mmk
    )
    await process_buy_sell(update, context, tx_info, [message.photo[-1]])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_transaction_text(message: Message) -> str:
    """Transaction text: the replied-to message's text, else the staff's own
    caption when it itself parses as a transaction."""
    original = message.reply_to_message
    original_text = (original.text or original.caption or "") if original else ""
    if original_text:
        return original_text
    staff_text = message.text or message.caption or ""
    if staff_text and extract_transaction_info(staff_text).type is not None:
        logger.info("Using staff reply text as transaction info")
        return staff_text
    return ""


async def _store_album_photo(svc: Services, context, message: Message) -> None:
    """Download and persist one album photo (idempotent)."""
    media_group_id = message.media_group_id
    if await svc.photos.has_photo(media_group_id, message.message_id):
        return
    try:
        photo_bytes = await svc.download_photo_bytes(context.bot, message.photo[-1])
        await svc.photos.save(media_group_id, message.message_id, photo_bytes)
    except Exception as e:
        logger.error("Failed to store album photo: %s", e)


async def _schedule_album_prescan(svc: Services, context, message: Message, tx_info) -> None:
    """Debounced pre-scan of a sale album (starts on the captioned photo)."""
    key = f"prescan:{message.media_group_id}"
    media_group_id = message.media_group_id

    async def flush(items: list, meta: dict) -> None:
        await prescan_sale_media_group(svc, context, media_group_id, meta["tx_info"])

    svc.collector.start(key, message.message_id, {"tx_info": tx_info}, flush)


async def _recover_original_album(svc: Services, context, original: Message) -> None:
    """Best-effort recovery of an album the bot never saw (e.g. posted while
    the bot was down).

    Telegram offers no 'fetch album' API, so we probe adjacent message ids by
    forwarding them to the same chat, harvesting photos, and deleting the
    forwards — the same workaround as v1, kept because it is load-bearing.
    """
    media_group_id = original.media_group_id
    if await svc.photos.get_group(media_group_id):
        return  # already stored

    chat_id = original.chat.id
    logger.info("Recovering album %s around message %s", media_group_id, original.message_id)
    try:
        photo_bytes = await svc.download_photo_bytes(context.bot, original.photo[-1])
        await svc.photos.save(media_group_id, original.message_id, photo_bytes)
    except Exception as e:
        logger.error("Failed to save original album photo: %s", e)

    for direction in (1, -1):
        for offset in range(1, 10):
            msg_id = original.message_id + direction * offset
            if msg_id <= 0:
                break
            try:
                forwarded = await context.bot.forward_message(
                    chat_id=chat_id, from_chat_id=chat_id, message_id=msg_id
                )
                try:
                    if not forwarded.photo:
                        break
                    fwd_bytes = await svc.download_photo_bytes(context.bot, forwarded.photo[-1])
                    await svc.photos.save(media_group_id, msg_id, fwd_bytes)
                finally:
                    try:
                        await context.bot.delete_message(
                            chat_id=chat_id, message_id=forwarded.message_id
                        )
                    except Exception:
                        pass
            except Exception:
                break

    recovered = await svc.photos.get_group(media_group_id)
    logger.info("Recovered %d photo(s) for album %s", len(recovered), media_group_id)
