"""Immediate ("pre-scan") OCR of sale messages.

When a Buy/Sell sale message arrives with photos, we OCR the receipts right
away and cache the result in the DB. When staff later reply, the settlement
uses the cached numbers instead of re-OCR'ing — faster replies, fewer API
calls, and the cache survives restarts.

Errors here are non-fatal by design: a failed pre-scan only means the staff
reply falls back to on-demand OCR.
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Message
from telegram.ext import ContextTypes

from handlers.common import LOW_CONFIDENCE, mmk_mismatch, usdt_mismatch
from models import ReceiptOcrRecord, TxInfo, TxType
from notify import esc
from services import Services

logger = logging.getLogger(__name__)


async def prescan_sale_message(
    svc: Services,
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    tx_info: TxInfo,
) -> None:
    """Single-photo sale message: OCR and cache immediately."""
    if not svc.ledger.is_loaded:
        logger.warning("Prescan skipped: balance not loaded")
        return
    if not message.photo:
        return
    if not await svc.idempotency.try_claim(message.message_id, "prescan"):
        return

    try:
        image = await svc.download_photo_b64(context.bot, message.photo[-1])
        if tx_info.type == TxType.SELL:
            await _prescan_sell_receipt(
                svc, message.message_id, message.media_group_id, 0, image, tx_info,
                notify=True,
            )
        elif tx_info.type == TxType.BUY:
            await _prescan_buy_receipt(
                svc, message.message_id, message.media_group_id, 0, image, tx_info,
                notify=True,
            )
    except Exception as e:
        logger.error("Prescan failed for message %s: %s", message.message_id, e)


async def prescan_sale_media_group(
    svc: Services,
    context: ContextTypes.DEFAULT_TYPE,
    media_group_id: str,
    tx_info: TxInfo,
) -> None:
    """Album sale message: photos were already stored on disk by the router;
    OCR them all and post one combined summary."""
    if not svc.ledger.is_loaded:
        logger.warning("Prescan (album) skipped: balance not loaded")
        return

    stored = await svc.photos.get_group(media_group_id)
    if not stored:
        logger.warning("Prescan: no stored photos for media group %s", media_group_id)
        return

    total = 0.0
    detected_bank: Optional[str] = None
    best_confidence = 0.0

    for idx, (msg_id, file_path) in enumerate(stored):
        try:
            image = Services.read_file_b64(file_path)
            if tx_info.type == TxType.SELL:
                receipt = await _prescan_sell_receipt(
                    svc, msg_id, media_group_id, idx, image, tx_info, notify=False
                )
                if receipt:
                    amount, bank, confidence = receipt
                    total += amount
                    if detected_bank is None and confidence >= LOW_CONFIDENCE:
                        detected_bank, best_confidence = bank, confidence
            elif tx_info.type == TxType.BUY:
                usdt = await _prescan_buy_receipt(
                    svc, msg_id, media_group_id, idx, image, tx_info, notify=False
                )
                if usdt:
                    total += usdt
        except Exception as e:
            logger.error("Prescan receipt %d failed: %s", idx + 1, e)

    if tx_info.type == TxType.SELL:
        mismatch = mmk_mismatch(tx_info.mmk, total, ratio=0.1)
        emoji = "⚠️" if mismatch or best_confidence < LOW_CONFIDENCE else "📥"
        text = (
            f"{emoji} <b>Sale Receipts Detected ({len(stored)} photos)</b>\n\n"
            f"<b>Type:</b> SELL\n"
            f"<b>Expected MMK:</b> {tx_info.mmk:,.0f}\n"
            f"<b>Total Detected MMK:</b> {total:,.0f}\n"
            f"<b>Detected Bank:</b> {esc(detected_bank or 'Unknown')}\n"
            f"<b>Best Confidence:</b> {best_confidence:.0f}%"
        )
        if mismatch:
            text += f"\n\n⚠️ <b>Amount Mismatch!</b> Difference: {abs(total - tx_info.mmk):,.0f} MMK"
        if best_confidence < LOW_CONFIDENCE:
            text += "\n⚠️ <b>Low Confidence!</b> Bank detection may be inaccurate"
        await svc.notifier.status(text)
    else:
        mismatch = usdt_mismatch(tx_info.usdt, total)
        emoji = "⚠️" if mismatch else "📥"
        text = (
            f"{emoji} <b>Sale Receipts Detected ({len(stored)} photos)</b>\n\n"
            f"<b>Type:</b> BUY\n"
            f"<b>Expected USDT:</b> {tx_info.usdt:.4f}\n"
            f"<b>Total Detected USDT:</b> {total:.4f}"
        )
        if mismatch:
            text += f"\n\n⚠️ <b>Amount Mismatch!</b> Difference: {abs(total - tx_info.usdt):.4f} USDT"
        await svc.notifier.status(text)

    logger.info("Prescan complete for media group %s: total=%.4f", media_group_id, total)


async def _prescan_sell_receipt(
    svc: Services,
    message_id: int,
    media_group_id: Optional[str],
    receipt_index: int,
    image: str,
    tx_info: TxInfo,
    notify: bool,
) -> Optional[tuple[float, Optional[str], float]]:
    balances = svc.ledger.snapshot()
    result = await svc.detect_mmk_any_bank(image, balances)
    if not result:
        if notify:
            await svc.notifier.status(
                f"⚠️ <b>Sale Receipt OCR Failed</b>\n\n"
                f"<b>Type:</b> SELL\n"
                f"<b>Expected MMK:</b> {tx_info.mmk:,.0f}\n\n"
                f"Could not detect amount/bank. Staff will need to verify manually."
            )
        return None

    bank_name = result.bank.bank_name if result.bank else None
    await svc.ocr_cache.save(
        ReceiptOcrRecord(
            message_id=message_id,
            receipt_index=receipt_index,
            transaction_type="sell",
            detected_amount=result.amount,
            detected_bank=bank_name,
            detected_usdt=None,
            media_group_id=media_group_id,
        ),
        raw={"confidence": result.confidence},
    )
    if notify:
        mismatch = mmk_mismatch(tx_info.mmk, result.amount, ratio=0.1)
        emoji = "⚠️" if mismatch or result.confidence < LOW_CONFIDENCE else "📥"
        text = (
            f"{emoji} <b>Sale Receipt Detected</b>\n\n"
            f"<b>Type:</b> SELL\n"
            f"<b>Expected MMK:</b> {tx_info.mmk:,.0f}\n"
            f"<b>Detected MMK:</b> {result.amount:,.0f}\n"
            f"<b>Detected Bank:</b> {esc(bank_name or 'Unknown')}\n"
            f"<b>Confidence:</b> {result.confidence:.0f}%"
        )
        if mismatch:
            text += f"\n\n⚠️ <b>Amount Mismatch!</b> Difference: {abs(result.amount - tx_info.mmk):,.0f} MMK"
        if result.confidence < LOW_CONFIDENCE:
            text += "\n⚠️ <b>Low Confidence!</b> Bank detection may be inaccurate"
        await svc.notifier.status(text)
    return result.amount, bank_name, result.confidence


async def _prescan_buy_receipt(
    svc: Services,
    message_id: int,
    media_group_id: Optional[str],
    receipt_index: int,
    image: str,
    tx_info: TxInfo,
    notify: bool,
) -> Optional[float]:
    # Match against registered wallets so settlement knows where the USDT
    # landed; fall back to amount-only extraction.
    detected_usdt = 0.0
    detected_wallet: Optional[str] = None
    confidence = 0.0

    wallets = await svc.usdt_wallets_for_ocr()
    if wallets:
        match = await svc.ocr.match_usdt_receipt(image, wallets)
        if match and match.amount > 0:
            detected_usdt = match.amount
            best_id, confidence = match.best()
            if best_id and confidence > 0:
                detected_wallet = wallets[best_id - 1]["bank_name"]
    if detected_usdt == 0:
        sent = await svc.ocr.extract_usdt_sent(image)
        if sent:
            detected_usdt = sent.total_amount

    if detected_usdt == 0:
        if notify:
            await svc.notifier.status(
                f"⚠️ <b>Sale Receipt OCR Failed</b>\n\n"
                f"<b>Type:</b> BUY\n"
                f"<b>Expected USDT:</b> {tx_info.usdt:.4f}\n\n"
                f"Could not detect the amount. Staff will need to verify manually."
            )
        return None

    await svc.ocr_cache.save(
        ReceiptOcrRecord(
            message_id=message_id,
            receipt_index=receipt_index,
            transaction_type="buy",
            detected_amount=None,
            detected_bank=detected_wallet,
            detected_usdt=detected_usdt,
            media_group_id=media_group_id,
        ),
        raw={"confidence": confidence},
    )
    if notify:
        mismatch = usdt_mismatch(tx_info.usdt, detected_usdt)
        emoji = "⚠️" if mismatch else "📥"
        text = (
            f"{emoji} <b>Sale Receipt Detected</b>\n\n"
            f"<b>Type:</b> BUY\n"
            f"<b>Expected USDT:</b> {tx_info.usdt:.4f}\n"
            f"<b>Detected USDT:</b> {detected_usdt:.4f}\n"
            f"<b>Wallet:</b> {esc(detected_wallet or 'Unknown')}"
        )
        if mismatch:
            text += f"\n\n⚠️ <b>Amount Mismatch!</b> Difference: {abs(detected_usdt - tx_info.usdt):.4f} USDT"
        await svc.notifier.status(text)
    return detected_usdt
