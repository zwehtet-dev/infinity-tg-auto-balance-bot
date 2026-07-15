"""P2P sell flows — staff sells USDT to another exchange (not a customer).

Three variants, matching v1:

1. **Staff P2P sell** (``P2P Sell U×R =M ... to D(B) From S(B)``):
   both banks named in the text → direct +MMK / -USDT, no OCR.
2. **Breakdown P2P sell** (``... fee-F`` plus ``AMOUNT to Prefix(Bank)`` lines):
   MMK split across named banks, USDT (amount + fee) from the staff's
   Binance-preferred account, no OCR.
3. **Receipt P2P sell** (``... fee-F`` with photos): each MMK receipt is
   OCR'd against the *staff's own* banks; USDT (amount + fee) from the
   staff's Binance-preferred account.
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Message, Update
from telegram.ext import ContextTypes

from balances import LedgerError
from handlers.common import PhotoSource, mmk_mismatch, to_b64_list
from models import BalanceChange, Currency, TxInfo
from notify import esc
from services import Services, get_services

logger = logging.getLogger(__name__)


async def _guard(svc: Services, message: Message, claim_kind: str) -> bool:
    """Common preconditions; returns True when processing may proceed."""
    if not svc.ledger.is_loaded:
        await svc.notifier.alert(
            "❌ Balance not loaded. Post the balance message in the auto balance topic first.",
            reply_to=message,
        )
        return False
    if not await svc.idempotency.try_claim(message.message_id, claim_kind):
        logger.info("%s %s already processed — skipping duplicate", claim_kind, message.message_id)
        return False
    return True


async def process_staff_p2p_sell(
    update: Update, context: ContextTypes.DEFAULT_TYPE, tx_info: TxInfo
) -> None:
    svc = get_services(context)
    message = update.message
    if not await _guard(svc, message, "staff_p2p_sell"):
        return
    try:
        user = message.from_user
        _, display_name = await svc.staff_identity(user)
        result = await svc.ledger.apply(
            [
                BalanceChange(Currency.MMK, tx_info.dest_bank, +tx_info.mmk),
                BalanceChange(Currency.USDT, tx_info.src_bank, -tx_info.usdt),
            ],
            tx_type="staff_p2p_sell",
            description=(
                f"Staff P2P Sell: +{tx_info.mmk:,.0f} MMK to {tx_info.dest_bank} | "
                f"-{tx_info.usdt:.4f} USDT from {tx_info.src_bank}"
            ),
            actor_id=user.id,
            actor_name=display_name,
            ref_message_id=message.message_id,
        )
        await svc.notifier.post_balance(result.balance_text)
        await svc.notifier.status(
            f"✅ <b>P2P Sell Transaction Processed</b>\n\n"
            f"<b>MMK:</b> +{tx_info.mmk:,.0f} MMK to {esc(tx_info.dest_bank)}\n"
            f"<b>USDT:</b> -{tx_info.usdt:.4f} USDT from {esc(tx_info.src_bank)}"
        )
    except LedgerError as e:
        await svc.idempotency.release(message.message_id, "staff_p2p_sell")
        await svc.notifier.alert(f"❌ {e}", reply_to=message)
    except Exception:
        await svc.idempotency.release(message.message_id, "staff_p2p_sell")
        raise


async def process_p2p_sell_with_breakdown(
    update: Update, context: ContextTypes.DEFAULT_TYPE, tx_info: TxInfo
) -> None:
    svc = get_services(context)
    message = update.message
    if not await _guard(svc, message, "p2p_breakdown"):
        return
    try:
        user = message.from_user
        user_prefix, display_name = await svc.staff_identity(user)
        balances = svc.ledger.snapshot()

        total_mmk = sum(entry.amount for entry in tx_info.bank_breakdown)
        if abs(total_mmk - tx_info.mmk) > 1000:
            await svc.notifier.status(
                f"⚠️ <b>MMK Amount Mismatch Warning</b>\n\n"
                f"<b>Transaction:</b> P2P Sell\n"
                f"<b>Staff:</b> {esc(user_prefix)}\n"
                f"<b>Expected (from message):</b> {tx_info.mmk:,.0f} MMK\n"
                f"<b>Total from breakdown:</b> {total_mmk:,.0f} MMK\n"
                f"<b>Difference:</b> {abs(total_mmk - tx_info.mmk):,.0f} MMK"
            )

        usdt_bank = svc.find_staff_usdt_bank(balances, user_prefix)
        if not usdt_bank:
            await svc.notifier.alert(
                f"❌ No USDT bank found for prefix '{esc(user_prefix)}'. "
                f"For P2P sell, a Binance account is preferred.",
                reply_to=message,
            )
            await svc.idempotency.release(message.message_id, "p2p_breakdown")
            return

        changes = [
            BalanceChange(Currency.MMK, entry.bank_name, +entry.amount)
            for entry in tx_info.bank_breakdown
        ]
        changes.append(BalanceChange(Currency.USDT, usdt_bank.bank_name, -tx_info.total_usdt))

        result = await svc.ledger.apply(
            changes,
            tx_type="p2p_sell",
            description=(
                f"P2P Sell (breakdown): +{total_mmk:,.0f} MMK across "
                f"{len(tx_info.bank_breakdown)} bank(s) | "
                f"-{tx_info.total_usdt:.4f} USDT ({usdt_bank.bank_name})"
            ),
            actor_id=user.id,
            actor_name=display_name,
            ref_message_id=message.message_id,
        )
        await svc.notifier.post_balance(result.balance_text)
        await svc.notifier.status(
            _p2p_summary(
                title="P2P Sell Transaction Processed (Bank Breakdown)",
                staff=user_prefix,
                banks=[(e.bank_name, e.amount) for e in tx_info.bank_breakdown],
                total_mmk=total_mmk,
                usdt_bank_name=usdt_bank.bank_name,
                tx_info=tx_info,
            )
        )
    except LedgerError as e:
        await svc.idempotency.release(message.message_id, "p2p_breakdown")
        await svc.notifier.alert(f"❌ {e}", reply_to=message)
    except Exception:
        await svc.idempotency.release(message.message_id, "p2p_breakdown")
        raise


async def process_p2p_sell_with_photos(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tx_info: TxInfo,
    photos: list[PhotoSource],
    message: Optional[Message] = None,
) -> None:
    svc = get_services(context)
    message = message or update.message
    if not await _guard(svc, message, "p2p_receipts"):
        return
    try:
        user = message.from_user
        user_prefix, display_name = await svc.staff_identity(user)
        balances = svc.ledger.snapshot()

        images = await to_b64_list(svc, context.bot, photos)
        detected: list[tuple[str, float]] = []  # (bank_name, amount)
        for idx, image in enumerate(images, 1):
            result = await svc.ocr.detect_mmk_bank_and_amount(image, balances.mmk, user_prefix)
            if result and result.amount and result.bank:
                detected.append((result.bank.bank_name, result.amount))
                logger.info(
                    "P2P receipt %d: %.0f MMK -> %s", idx, result.amount, result.bank.bank_name
                )
            else:
                logger.warning("P2P Sell: could not process receipt %d", idx)

        if not detected:
            await svc.notifier.alert(
                "❌ Could not detect bank/amount from the MMK receipt(s). "
                "Make sure they match one of your registered bank accounts.",
                reply_to=message,
            )
            await svc.idempotency.release(message.message_id, "p2p_receipts")
            return

        total_mmk = sum(amount for _, amount in detected)
        if mmk_mismatch(tx_info.mmk, total_mmk, ratio=0.5):
            await svc.notifier.status(
                f"⚠️ <b>MMK Amount Mismatch Warning</b>\n\n"
                f"<b>Transaction:</b> P2P Sell\n"
                f"<b>Staff:</b> {esc(user_prefix)}\n"
                f"<b>Expected (from message):</b> {tx_info.mmk:,.0f} MMK\n"
                f"<b>Detected (from OCR):</b> {total_mmk:,.0f} MMK\n"
                f"<b>Receipts:</b> {len(detected)}\n\n"
                f"⚠️ Processing with the OCR-detected amount."
            )

        usdt_bank = svc.find_staff_usdt_bank(balances, user_prefix)
        if not usdt_bank:
            await svc.notifier.alert(
                f"❌ No USDT bank found for prefix '{esc(user_prefix)}'. "
                f"For P2P sell, a Binance account is preferred.",
                reply_to=message,
            )
            await svc.idempotency.release(message.message_id, "p2p_receipts")
            return

        changes = [BalanceChange(Currency.MMK, name, +amount) for name, amount in detected]
        changes.append(BalanceChange(Currency.USDT, usdt_bank.bank_name, -tx_info.total_usdt))

        result = await svc.ledger.apply(
            changes,
            tx_type="p2p_sell",
            description=(
                f"P2P Sell: +{total_mmk:,.0f} MMK ({len(detected)} receipt(s)) | "
                f"-{tx_info.total_usdt:.4f} USDT ({usdt_bank.bank_name})"
            ),
            actor_id=user.id,
            actor_name=display_name,
            ref_message_id=message.message_id,
        )
        await svc.notifier.post_balance(result.balance_text)
        await svc.notifier.status(
            _p2p_summary(
                title="P2P Sell Transaction Processed",
                staff=user_prefix,
                banks=detected,
                total_mmk=total_mmk,
                usdt_bank_name=usdt_bank.bank_name,
                tx_info=tx_info,
                receipts=len(detected),
            )
        )
    except LedgerError as e:
        await svc.idempotency.release(message.message_id, "p2p_receipts")
        await svc.notifier.alert(f"❌ {e}", reply_to=message)
    except Exception:
        await svc.idempotency.release(message.message_id, "p2p_receipts")
        raise


def _p2p_summary(
    title: str,
    staff: str,
    banks: list[tuple[str, float]],
    total_mmk: float,
    usdt_bank_name: str,
    tx_info: TxInfo,
    receipts: Optional[int] = None,
) -> str:
    if len(banks) == 1:
        mmk_summary = f"+{total_mmk:,.0f} ({esc(banks[0][0])})"
    else:
        details = ", ".join(f"+{amount:,.0f} ({esc(name)})" for name, amount in banks)
        mmk_summary = f"+{total_mmk:,.0f} total ({details})"
    text = (
        f"✅ <b>{title}</b>\n\n"
        f"<b>Staff:</b> {esc(staff)}\n"
        f"<b>MMK:</b> {mmk_summary}\n"
        f"<b>USDT:</b> -{tx_info.total_usdt:.4f} ({esc(usdt_bank_name)})\n"
        f"<b>Fee:</b> {tx_info.fee:.4f} USDT\n"
        f"<b>Rate:</b> {tx_info.rate:.5f}"
    )
    if receipts is not None:
        text += f"\n<b>Receipts:</b> {receipts}"
    return text
