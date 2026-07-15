"""Buy / Sell transaction processing.

v1 had four near-duplicate processors (single/bulk × buy/sell, ~1200 lines).
v2 has one buy flow and one sell flow, each taking a *list* of photos — a
single photo is just a one-element list.

Flow recap (unchanged semantics, see ../botflow.md in the repo root):

BUY  — customer sends USDT to one of our wallets; staff pays out MMK.
  * sale message  (reply target has no photo): OCR customer's USDT receipt(s),
    identify our receiving wallet, cache the result, wait for staff.
  * staff reply   (reply target has photo):   OCR staff's MMK receipt(s)
    against the staff's own banks, then apply: -MMK, +USDT.

SELL — customer sends MMK to one of our banks; staff sends USDT out.
  * sale message: OCR customer's MMK receipt(s) against ALL registered banks,
    cache, wait for staff.
  * staff reply:  OCR staff's USDT receipt(s) (amount + network fee), then
    apply: +MMK, -USDT from the staff's account for the receipt's wallet type.

Reliability upgrades over v1:
- every balance mutation goes through the atomic, persisted, audited ledger;
- an idempotency claim per (staff message, action) prevents double-applying
  a transaction when Telegram redelivers an update;
- failed OCR aborts with a clear alert instead of a stack trace.
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Message, Update
from telegram.ext import ContextTypes

from balances import LedgerError
from handlers.common import LOW_CONFIDENCE, PhotoSource, mmk_mismatch, to_b64_list, usdt_mismatch
from models import BalanceChange, Currency, ReceiptOcrRecord, TxInfo, TxType
from notify import esc
from parsing import parse_reply_fee, parse_reply_source_bank
from services import Services, get_services

logger = logging.getLogger(__name__)


async def process_buy_sell(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tx_info: TxInfo,
    photos: list[PhotoSource],
    message: Optional[Message] = None,
) -> None:
    """Entry point for both flows; dispatches on tx type and message role."""
    svc = get_services(context)
    message = message or update.message
    if message is None:
        return

    if not svc.ledger.is_loaded:
        await svc.notifier.alert(
            "❌ Balance not loaded. Post the balance message in the auto balance topic first.",
            reply_to=message,
        )
        return
    if not photos:
        await svc.notifier.alert("❌ No receipt", reply_to=message)
        return

    original = message.reply_to_message
    original_has_photo = bool(original and original.photo)

    if tx_info.type == TxType.BUY:
        if original_has_photo:
            await _buy_staff_reply(context, svc, tx_info, photos, message)
        else:
            await _buy_sale_message(context, svc, tx_info, photos, message)
    elif tx_info.type == TxType.SELL:
        if original_has_photo:
            await _sell_staff_reply(context, svc, tx_info, photos, message)
        else:
            await _sell_sale_message(context, svc, tx_info, photos, message)


# ---------------------------------------------------------------------------
# BUY
# ---------------------------------------------------------------------------

async def _buy_sale_message(
    context, svc: Services, tx_info: TxInfo, photos: list[PhotoSource], message: Message
) -> None:
    """Customer's USDT receipt(s) arrive. Identify our wallet, cache, wait."""
    sender = message.from_user
    sender_name = sender.username or sender.first_name or str(sender.id)
    images = await to_b64_list(svc, context.bot, photos)
    if not images:
        await svc.notifier.alert("❌ Could not download receipt photo(s)", reply_to=message)
        return

    wallets = await svc.usdt_wallets_for_ocr()
    if not wallets:
        await svc.notifier.alert(
            "❌ No USDT wallets registered. Add one with /set_usdt_bank first.", reply_to=message
        )
        return

    total_usdt = 0.0
    detected_wallet: Optional[str] = None
    confidence = 0.0

    if len(images) == 1:
        # Single receipt: strict wallet match (the customer must have paid
        # into one of OUR registered wallets).
        match = await svc.ocr.match_usdt_receipt(images[0], wallets)
        if match and match.amount > 0:
            total_usdt = match.amount
            best_id, confidence = match.best()
            if best_id and confidence > 0:
                detected_wallet = wallets[best_id - 1]["bank_name"]
        else:
            total_usdt = tx_info.usdt
            logger.warning("Buy: USDT receipt OCR failed, using message amount %.4f", total_usdt)

        if not detected_wallet:
            await svc.notifier.alert(
                "❌ USDT wallet not recognized. The receipt does not match any registered wallet.",
                reply_to=message,
            )
            return
        if confidence < LOW_CONFIDENCE:
            await svc.notifier.status(
                f"⚠️ Low confidence wallet match: {esc(detected_wallet)} ({confidence:.0f}%)"
            )
    else:
        # Album of receipts: sum received amounts; identify the wallet from
        # the first receipt that matches.
        for idx, image in enumerate(images, 1):
            received = await svc.ocr.extract_usdt_received(image)
            if received and received.received_amount > 0:
                total_usdt += received.received_amount
                logger.info("Buy receipt %d: %.4f USDT received", idx, received.received_amount)
        if total_usdt == 0:
            total_usdt = tx_info.usdt
        match = await svc.ocr.match_usdt_receipt(images[0], wallets)
        if match:
            best_id, confidence = match.best()
            if best_id and confidence > 0:
                detected_wallet = wallets[best_id - 1]["bank_name"]

    if usdt_mismatch(tx_info.usdt, total_usdt):
        await svc.notifier.status(
            f"⚠️ <b>USDT Amount Mismatch Warning</b>\n\n"
            f"<b>Transaction:</b> Buy\n"
            f"<b>Sender:</b> @{esc(sender_name)}\n"
            f"<b>Expected (from message):</b> {tx_info.usdt:.4f} USDT\n"
            f"<b>Detected (from OCR):</b> {total_usdt:.4f} USDT"
        )

    await svc.ocr_cache.save(
        ReceiptOcrRecord(
            message_id=message.message_id,
            receipt_index=0,
            transaction_type="buy",
            detected_amount=None,
            detected_bank=detected_wallet,
            detected_usdt=total_usdt,
            media_group_id=message.media_group_id,
        ),
        raw={"confidence": confidence, "receipts": len(images)},
    )
    await svc.notifier.status(
        f"📥 Buy: {total_usdt:.4f} USDT → {esc(detected_wallet or 'wallet TBD')} "
        f"| Waiting for MMK receipt"
    )


async def _buy_staff_reply(
    context, svc: Services, tx_info: TxInfo, photos: list[PhotoSource], message: Message
) -> None:
    """Staff's MMK receipt(s) arrive as a reply: settle the buy."""
    original = message.reply_to_message
    claim_kind = "buy_settle"
    if not await svc.idempotency.try_claim(message.message_id, claim_kind):
        logger.info("Buy settle %s already processed — skipping duplicate", message.message_id)
        return

    try:
        user = message.from_user
        user_prefix, display_name = await svc.staff_identity(user)
        balances = svc.ledger.snapshot()

        images = await to_b64_list(svc, context.bot, photos)
        if not images:
            await svc.notifier.alert("❌ Could not download receipt photo(s)", reply_to=message)
            raise _Abort()

        # --- MMK side: staff's own banks -----------------------------------
        staff_text = message.text or message.caption or ""
        mmk_fee = parse_reply_fee(staff_text)

        # Explicit bank override: "From San(KBZ)" in the staff reply wins over
        # OCR — the bank comes from the text, OCR is only used for the amount.
        specified_bank = None
        specified_name = parse_reply_source_bank(staff_text)
        if specified_name:
            specified_bank = balances.find(Currency.MMK, specified_name)
            if not specified_bank:
                await svc.notifier.alert(
                    f"❌ Specified bank '{esc(specified_name)}' not found in registered MMK banks",
                    reply_to=message,
                )
                raise _Abort()

        total_mmk = 0.0
        detected_bank = specified_bank
        for idx, image in enumerate(images, 1):
            result = await svc.ocr.detect_mmk_bank_and_amount(image, balances.mmk, user_prefix)
            if result and result.amount:
                total_mmk += result.amount
                if detected_bank is None and result.bank:
                    detected_bank = result.bank
                logger.info("Buy MMK receipt %d: %.0f MMK", idx, result.amount)
            else:
                logger.warning("Buy: could not read MMK receipt %d", idx)
        if specified_bank:
            detected_bank = specified_bank
        if not detected_bank or total_mmk == 0:
            await svc.notifier.alert(
                "❌ Cannot read MMK receipt. Make sure it is one of your registered bank accounts.",
                reply_to=message,
            )
            raise _Abort()
        total_mmk += mmk_fee

        # --- USDT side: cached prescan or on-demand OCR of the original ----
        detected_usdt = tx_info.usdt
        detected_wallet: Optional[str] = None
        cached = await svc.ocr_cache.get_by_message(original.message_id)
        if not cached and original.media_group_id:
            cached = await svc.ocr_cache.get_by_media_group(original.media_group_id)
        if cached:
            detected_usdt = sum(r.detected_usdt or 0 for r in cached) or tx_info.usdt
            detected_wallet = next((r.detected_bank for r in cached if r.detected_bank), None)
            logger.info("Buy: using pre-scanned USDT %.4f -> %s", detected_usdt, detected_wallet)
        elif original.photo:
            orig_image = await svc.download_photo_b64(context.bot, original.photo[-1])
            wallets = await svc.usdt_wallets_for_ocr()
            if wallets:
                match = await svc.ocr.match_usdt_receipt(orig_image, wallets)
                if match and match.amount > 0:
                    detected_usdt = match.amount
                    best_id, _ = match.best()
                    if best_id:
                        detected_wallet = wallets[best_id - 1]["bank_name"]

        if mmk_mismatch(tx_info.mmk, total_mmk, ratio=0.1):
            await svc.notifier.status(
                f"⚠️ <b>MMK Amount Mismatch Warning</b>\n\n"
                f"<b>Transaction:</b> Buy\n"
                f"<b>Staff:</b> {esc(user_prefix)}\n"
                f"<b>Expected (from message):</b> {tx_info.mmk:,.0f} MMK\n"
                f"<b>Detected (from OCR):</b> {total_mmk:,.0f} MMK\n"
                f"<b>Difference:</b> {abs(total_mmk - tx_info.mmk):,.0f} MMK"
            )

        # --- Resolve receiving USDT account ---------------------------------
        receiving = detected_wallet or await svc.app_settings.get_receiving_usdt_account()
        changes = [
            BalanceChange(Currency.MMK, detected_bank.bank_name, -total_mmk),
        ]
        usdt_leg_missing = svc.ledger.snapshot().find(Currency.USDT, receiving) is None
        if not usdt_leg_missing:
            changes.append(BalanceChange(Currency.USDT, receiving, +detected_usdt))

        result = await svc.ledger.apply(
            changes,
            tx_type="buy",
            description=(
                f"Buy: -{total_mmk:,.0f} MMK ({detected_bank.bank_name}) | "
                f"+{detected_usdt:.4f} USDT ({receiving})"
            ),
            actor_id=user.id,
            actor_name=display_name,
            ref_message_id=message.message_id,
        )

        if usdt_leg_missing:
            await svc.notifier.alert(
                f"⚠️ USDT account '{esc(receiving)}' not found in balance — "
                f"USDT was NOT credited. Adjust manually.",
                reply_to=message,
            )

        # Cleanup caches for the settled sale
        await svc.ocr_cache.delete_by_message(original.message_id)
        if original.media_group_id:
            await svc.ocr_cache.delete_by_media_group(original.media_group_id)
            await svc.photos.delete_group(original.media_group_id)

        await svc.notifier.post_balance(result.balance_text)
        fee_note = f" (incl. fee {mmk_fee:,.0f})" if mmk_fee else ""
        bank_source = " (specified in text)" if specified_bank else ""
        await svc.notifier.status(
            f"✅ Buy: -{total_mmk:,.0f} MMK{fee_note} ({esc(detected_bank.bank_name)}{bank_source}) | "
            f"+{detected_usdt:.4f} USDT ({esc(receiving)})"
        )
    except _Abort:
        await svc.idempotency.release(message.message_id, claim_kind)
    except LedgerError as e:
        await svc.idempotency.release(message.message_id, claim_kind)
        await svc.notifier.alert(f"❌ {e}", reply_to=message)
    except Exception:
        await svc.idempotency.release(message.message_id, claim_kind)
        raise


# ---------------------------------------------------------------------------
# SELL
# ---------------------------------------------------------------------------

async def _sell_sale_message(
    context, svc: Services, tx_info: TxInfo, photos: list[PhotoSource], message: Message
) -> None:
    """Customer's MMK receipt(s) arrive. Detect bank+amount, cache, wait."""
    sender = message.from_user
    sender_name = sender.username or sender.first_name or str(sender.id)
    balances = svc.ledger.snapshot()

    images = await to_b64_list(svc, context.bot, photos)
    if not images:
        await svc.notifier.alert("❌ Could not download receipt photo(s)", reply_to=message)
        return

    total_mmk = 0.0
    detected_bank_name: Optional[str] = None
    best_confidence = 0.0
    for idx, image in enumerate(images):
        result = await svc.detect_mmk_any_bank(image, balances)
        if result and result.amount:
            total_mmk += result.amount
            if detected_bank_name is None and result.bank and result.confidence >= LOW_CONFIDENCE:
                detected_bank_name = result.bank.bank_name
                best_confidence = result.confidence
            await svc.ocr_cache.save(
                ReceiptOcrRecord(
                    message_id=message.message_id,
                    receipt_index=idx,
                    transaction_type="sell",
                    detected_amount=result.amount,
                    detected_bank=result.bank.bank_name if result.bank else None,
                    detected_usdt=None,
                    media_group_id=message.media_group_id,
                ),
                raw={"confidence": result.confidence},
            )

    if total_mmk == 0:
        total_mmk = tx_info.mmk
        logger.warning("Sell: OCR failed on all receipts, using message amount")
    if not detected_bank_name:
        await svc.notifier.alert(
            "❌ Could not detect the MMK bank from the receipt. "
            "Make sure it matches one of the registered MMK bank accounts.",
            reply_to=message,
        )
        return

    if mmk_mismatch(tx_info.mmk, total_mmk, ratio=0.1):
        await svc.notifier.status(
            f"⚠️ <b>MMK Amount Mismatch Warning</b>\n\n"
            f"<b>Transaction:</b> Sell\n"
            f"<b>Sender:</b> @{esc(sender_name)}\n"
            f"<b>Expected (from message):</b> {tx_info.mmk:,.0f} MMK\n"
            f"<b>Detected (from OCR):</b> {total_mmk:,.0f} MMK"
        )
    if best_confidence < LOW_CONFIDENCE:
        await svc.notifier.status(
            f"⚠️ <b>Low Confidence Bank Detection</b>\n\n"
            f"<b>Detected Bank:</b> {esc(detected_bank_name)}\n"
            f"<b>Confidence:</b> {best_confidence:.0f}%\n\n"
            f"Please verify the receipt matches the correct bank account."
        )

    await svc.notifier.status(
        f"📥 <b>Sell Transaction — MMK Receipt Processed</b>\n\n"
        f"<b>Sender:</b> @{esc(sender_name)}\n"
        f"<b>MMK Detected:</b> {total_mmk:,.0f} ({esc(detected_bank_name)})\n"
        f"<b>Expected USDT:</b> {tx_info.usdt:.4f}\n\n"
        f"⏳ Waiting for staff to send the USDT receipt..."
    )


async def _sell_staff_reply(
    context, svc: Services, tx_info: TxInfo, photos: list[PhotoSource], message: Message
) -> None:
    """Staff's USDT receipt(s) arrive as a reply: settle the sell."""
    original = message.reply_to_message
    claim_kind = "sell_settle"
    if not await svc.idempotency.try_claim(message.message_id, claim_kind):
        logger.info("Sell settle %s already processed — skipping duplicate", message.message_id)
        return

    media_group_to_cleanup: Optional[str] = None
    try:
        user = message.from_user
        user_prefix, display_name = await svc.staff_identity(user)
        balances = svc.ledger.snapshot()
        staff_text = message.text or message.caption or ""
        mmk_fee = parse_reply_fee(staff_text)

        # Explicit bank override: "From San(Kpay P)" in the staff reply.
        specified_bank = None
        specified_name = parse_reply_source_bank(staff_text)
        if specified_name:
            specified_bank = balances.find(Currency.MMK, specified_name)
            if not specified_bank:
                await svc.notifier.alert(
                    f"❌ Specified bank '{esc(specified_name)}' not found in registered MMK banks",
                    reply_to=message,
                )
                raise _Abort()

        # --- MMK side: prescanned cache, else stored photos, else original --
        total_mmk = 0.0
        receipt_count = 0
        detected_bank = specified_bank

        cached = await svc.ocr_cache.get_by_message(original.message_id)
        if not cached and original.media_group_id:
            cached = await svc.ocr_cache.get_by_media_group(original.media_group_id)

        if cached:
            logger.info("Sell: using %d pre-scanned receipt(s)", len(cached))
            for record in cached:
                if record.detected_amount:
                    total_mmk += record.detected_amount
                    receipt_count += 1
                    if detected_bank is None and record.detected_bank:
                        detected_bank = balances.find(Currency.MMK, record.detected_bank)
            media_group_to_cleanup = cached[0].media_group_id
            if original.media_group_id:
                await svc.ocr_cache.delete_by_media_group(original.media_group_id)
            else:
                await svc.ocr_cache.delete_by_message(original.message_id)
        else:
            # Collect the customer's photos: stored album if we have it,
            # otherwise the single replied-to photo.
            sources: list[PhotoSource] = []
            mg_id, stored = await svc.photos.get_group_by_message(original.message_id)
            if not stored and original.media_group_id:
                mg_id = original.media_group_id
                stored = await svc.photos.get_group(original.media_group_id)
            if stored:
                sources = [path for _, path in stored]
                media_group_to_cleanup = mg_id
            elif original.photo:
                sources = [original.photo[-1]]

            for idx, image in enumerate(await to_b64_list(svc, context.bot, sources), 1):
                result = await svc.detect_mmk_any_bank(image, balances)
                if result and result.amount:
                    total_mmk += result.amount
                    receipt_count += 1
                    if detected_bank is None and result.bank:
                        detected_bank = result.bank
                else:
                    logger.warning("Sell: could not read MMK receipt %d", idx)

        if specified_bank:
            detected_bank = specified_bank
        if (receipt_count == 0 and total_mmk == 0) or detected_bank is None:
            await svc.notifier.alert("❌ Cannot read the customer's MMK receipt", reply_to=message)
            raise _Abort()

        total_mmk += mmk_fee
        if mmk_mismatch(tx_info.mmk, total_mmk, ratio=0.5):
            await svc.notifier.status(
                f"⚠️ <b>MMK Amount Mismatch Warning</b>\n\n"
                f"<b>Transaction:</b> Sell\n"
                f"<b>Staff:</b> {esc(user_prefix)}\n"
                f"<b>Receipts:</b> {receipt_count}\n"
                f"<b>Expected (from message):</b> {tx_info.mmk:,.0f} MMK\n"
                f"<b>Detected (from OCR):</b> {total_mmk:,.0f} MMK"
            )

        # --- USDT side: staff's outgoing receipt(s) -------------------------
        images = await to_b64_list(svc, context.bot, photos)
        total_usdt = 0.0
        bank_type = "swift"
        got_usdt = False
        for image in images:
            sent = await svc.ocr.extract_usdt_sent(image)
            if sent:
                total_usdt += sent.total_amount
                if not got_usdt:
                    bank_type = sent.bank_type or "swift"
                got_usdt = True
        if not got_usdt:
            await svc.notifier.alert("❌ Cannot read the USDT receipt", reply_to=message)
            raise _Abort()

        expected_usdt_bank = f"{user_prefix}({bank_type.capitalize()})"
        usdt_bank = balances.find(Currency.USDT, expected_usdt_bank)

        changes = [BalanceChange(Currency.MMK, detected_bank.bank_name, +total_mmk)]
        if usdt_bank is not None:
            changes.append(BalanceChange(Currency.USDT, usdt_bank.bank_name, -total_usdt))

        result = await svc.ledger.apply(
            changes,
            tx_type="sell",
            description=(
                f"Sell: +{total_mmk:,.0f} MMK ({detected_bank.bank_name}) | "
                f"-{total_usdt:.4f} USDT ({expected_usdt_bank})"
            ),
            actor_id=user.id,
            actor_name=display_name,
            ref_message_id=message.message_id,
        )

        if usdt_bank is None:
            await svc.notifier.alert(
                f"⚠️ USDT bank '{esc(expected_usdt_bank)}' not found — "
                f"USDT was NOT deducted. Adjust manually.",
                reply_to=message,
            )

        if media_group_to_cleanup:
            await svc.photos.delete_group(media_group_to_cleanup)

        await svc.notifier.post_balance(result.balance_text)
        mmk_display = f"{total_mmk:,.0f}"
        if mmk_fee > 0:
            mmk_display += f" (Receipts: {total_mmk - mmk_fee:,.0f} + Fee: {mmk_fee:,.0f})"
        elif receipt_count > 1:
            mmk_display += f" ({receipt_count} receipts)"
        bank_source = " (specified in text)" if specified_bank else ""
        await svc.notifier.status(
            f"✅ Sell: +{mmk_display} ({esc(detected_bank.bank_name)}{bank_source}) | "
            f"-{total_usdt:.4f} USDT"
        )
    except _Abort:
        await svc.idempotency.release(message.message_id, claim_kind)
    except LedgerError as e:
        await svc.idempotency.release(message.message_id, claim_kind)
        await svc.notifier.alert(f"❌ {e}", reply_to=message)
    except Exception:
        await svc.idempotency.release(message.message_id, claim_kind)
        raise


class _Abort(Exception):
    """Internal control-flow: abort processing after the user was informed."""
