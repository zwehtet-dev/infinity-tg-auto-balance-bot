"""Internal transfers between the business's own accounts (Accounts Matter topic).

Two formats:
- **Coin transfer** (explicit fee, no OCR):
  ``San (binance) to OKM(Wallet) 10 USDT-0.47 USDT(fee) = 9.53 USDT``
  → debit the sent amount, credit the received (post-fee) amount.
- **Plain transfer** (``San(Wave Channel) to NDT (Wave)`` + receipt photos):
  OCR the amount(s) off the receipt(s) and move the sum between accounts.
  Works across MMK / USDT / THB — the accounts are looked up in any section.
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Message, Update
from telegram.ext import ContextTypes

from balances import LedgerError
from handlers.common import PhotoSource, to_b64_list
from models import BalanceChange, Currency
from notify import esc
from parsing import parse_coin_transfer, parse_internal_transfer
from services import Services, get_services

logger = logging.getLogger(__name__)

_USDT_KEYWORDS = ("swift", "wallet", "binance")


async def handle_accounts_matter_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Entry point for messages in the Accounts Matter topic."""
    svc = get_services(context)
    message = update.message
    text = message.text or message.caption or ""

    # Caption-less photos of an album whose first (captioned) photo already
    # started a pending transfer: just append them.
    if message.photo and message.media_group_id:
        if svc.collector.add(f"internal:{message.media_group_id}", message.photo[-1]):
            return

    coin = parse_coin_transfer(text)
    if coin:
        await _process_coin_transfer(svc, message, coin)
        return

    transfer = parse_internal_transfer(text)
    if not transfer:
        logger.info("Accounts Matter: not a transfer message, ignoring")
        return
    from_name, to_name = transfer

    if not message.photo:
        await svc.notifier.alert("❌ No receipt photo", reply_to=message)
        return

    if message.media_group_id:
        # Album of receipts: collect with debounce, then process the batch.
        key = f"internal:{message.media_group_id}"

        async def flush(items: list, meta: dict) -> None:
            await _process_internal_transfer(
                svc, context, meta["message"], meta["from"], meta["to"], items
            )

        svc.collector.start(
            key,
            message.photo[-1],
            {"message": message, "from": from_name, "to": to_name},
            flush,
        )
        return

    await _process_internal_transfer(svc, context, message, from_name, to_name, [message.photo[-1]])


async def _process_coin_transfer(svc: Services, message: Message, coin: dict) -> None:
    claim_kind = "coin_transfer"
    if not svc.ledger.is_loaded:
        await svc.notifier.alert(
            "❌ Balance not loaded. Post the balance message in the auto balance topic first.",
            reply_to=message,
        )
        return
    if not await svc.idempotency.try_claim(message.message_id, claim_kind):
        return
    try:
        user = message.from_user
        result = await svc.ledger.apply(
            [
                BalanceChange(Currency.USDT, coin["from_bank"], -coin["sent"]),
                BalanceChange(Currency.USDT, coin["to_bank"], +coin["received"]),
            ],
            tx_type="coin_transfer",
            description=(
                f"Coin transfer: {coin['from_bank']} → {coin['to_bank']} | "
                f"sent {coin['sent']:.4f}, fee {coin['fee']:.4f}, received {coin['received']:.4f} USDT"
            ),
            actor_id=user.id if user else None,
            actor_name=(user.username or user.first_name) if user else None,
            ref_message_id=message.message_id,
        )
        await svc.notifier.post_balance(result.balance_text)
        await svc.notifier.status(
            f"✅ Transfer: {esc(coin['from_bank'])} → {esc(coin['to_bank'])} | "
            f"{coin['received']:.4f} USDT (fee {coin['fee']:.4f})"
        )
    except LedgerError as e:
        await svc.idempotency.release(message.message_id, claim_kind)
        await svc.notifier.alert(f"❌ {e}", reply_to=message)
    except Exception:
        await svc.idempotency.release(message.message_id, claim_kind)
        raise


async def _process_internal_transfer(
    svc: Services,
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    from_name: str,
    to_name: str,
    photos: list[PhotoSource],
) -> None:
    claim_kind = "internal_transfer"
    if not svc.ledger.is_loaded:
        await svc.notifier.alert(
            "❌ Balance not loaded. Post the balance message in the auto balance topic first.",
            reply_to=message,
        )
        return
    if not await svc.idempotency.try_claim(message.message_id, claim_kind):
        return
    try:
        balances = svc.ledger.snapshot()
        names = (from_name + " " + to_name).lower()
        is_usdt = any(k in names for k in _USDT_KEYWORDS)

        # OCR every receipt and sum the amounts.
        total = 0.0
        receipt_count = 0
        from_is_swift_wallet = any(
            k in from_name.lower() for k in ("swift", "wallet")
        )
        for idx, image in enumerate(await to_b64_list(svc, context.bot, photos), 1):
            amount: Optional[float] = None
            if is_usdt:
                sent = await svc.ocr.extract_usdt_sent(image)
                if sent:
                    # Leaving a Swift/personal wallet costs sender the network
                    # fee (total); into Binance we count the displayed amount.
                    amount = sent.total_amount if from_is_swift_wallet else sent.amount
            else:
                amount = await svc.ocr.extract_amount(image)
            if amount:
                total += amount
                receipt_count += 1
                logger.info("Internal transfer receipt %d: %.4f", idx, amount)
            else:
                logger.warning("Internal transfer: could not read receipt %d", idx)

        if receipt_count == 0:
            await svc.notifier.alert(
                "❌ Could not detect the transfer amount from the receipt(s)", reply_to=message
            )
            await svc.idempotency.release(message.message_id, claim_kind)
            return

        # Resolve the two accounts in any currency section.
        from_found = balances.find_any(from_name)
        to_found = balances.find_any(to_name)
        if not from_found:
            await svc.notifier.alert(f"❌ Source bank not found: {esc(from_name)}", reply_to=message)
            await svc.idempotency.release(message.message_id, claim_kind)
            return
        if not to_found:
            await svc.notifier.alert(
                f"❌ Destination bank not found: {esc(to_name)}", reply_to=message
            )
            await svc.idempotency.release(message.message_id, claim_kind)
            return
        from_currency, from_bank = from_found
        to_currency, to_bank = to_found

        user = message.from_user
        result = await svc.ledger.apply(
            [
                BalanceChange(from_currency, from_bank.bank_name, -total),
                BalanceChange(to_currency, to_bank.bank_name, +total),
            ],
            tx_type="internal_transfer",
            description=(
                f"Internal transfer: {from_bank.bank_name} → {to_bank.bank_name} | "
                f"{total:,.4f} ({receipt_count} receipt(s))"
            ),
            actor_id=user.id if user else None,
            actor_name=(user.username or user.first_name) if user else None,
            ref_message_id=message.message_id,
        )

        currency_label = "USDT" if is_usdt else ("THB" if "thb" in names else "MMK")
        new_from = result.balances.find(from_currency, from_bank.bank_name)
        new_to = result.balances.find(to_currency, to_bank.bank_name)
        receipt_info = f" ({receipt_count} receipts)" if receipt_count > 1 else ""

        await svc.notifier.post_balance(result.balance_text)
        await svc.notifier.status(
            f"✅ <b>Internal Transfer Processed</b>\n\n"
            f"<b>From:</b> {esc(from_bank.bank_name)}\n"
            f"<b>To:</b> {esc(to_bank.bank_name)}\n"
            f"<b>Amount:</b> {total:,.4f} {currency_label}{receipt_info}\n\n"
            f"<b>New Balances:</b>\n"
            f"{esc(from_bank.bank_name)}: {new_from.amount if new_from else 0:,.4f} {currency_label}\n"
            f"{esc(to_bank.bank_name)}: {new_to.amount if new_to else 0:,.4f} {currency_label}"
        )
    except LedgerError as e:
        await svc.idempotency.release(message.message_id, claim_kind)
        await svc.notifier.alert(f"❌ {e}", reply_to=message)
    except Exception:
        await svc.idempotency.release(message.message_id, claim_kind)
        raise
