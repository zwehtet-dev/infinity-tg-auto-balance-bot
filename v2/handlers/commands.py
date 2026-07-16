"""Slash commands: status, balance ops, user mapping, bank management.

Changes from v1:
- **Authorization**: when ``ADMIN_USER_IDS`` is configured, mutating commands
  (user mapping, bank registration, settings) are admin-only. v1 let anyone
  in the group rewrite the bank registry the OCR verifies receipts against.
- **New commands**: /help, /health (liveness + config check), /audit
  (recent balance mutations).
- All user-supplied values are HTML-escaped before echoing back.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from balances import format_balance_message, parse_balance_message
from notify import esc
from services import Services, get_services

logger = logging.getLogger(__name__)

_STARTED_AT = time.monotonic()


async def _require_admin(svc: Services, update: Update) -> bool:
    user = update.effective_user
    if user and svc.settings.is_admin(user.id):
        return True
    await svc.notifier.command_reply(
        "🔒 This command is restricted to bot administrators."
    )
    logger.warning(
        "Denied admin command %s from user %s",
        update.effective_message.text if update.effective_message else "?",
        user.id if user else "?",
    )
    return False


# ---------------------------------------------------------------- status/help

HELP_TEXT = (
    "✅ <b>Infinity Balance Bot v2</b>\n\n"
    "🔧 Independent mode — balances persisted locally\n"
    "🧾 Every balance change is audited (/audit)\n\n"
    "<b>Commands:</b>\n"
    "/start, /help - This help\n"
    "/balance - Show current balance\n"
    "/load - Load balance (reply to a balance message)\n"
    "/health - Bot liveness and configuration check\n"
    "/audit - Recent balance changes\n"
    "/test - Location/configuration test\n\n"
    "<b>Users:</b>\n"
    "/set_user - Map user to staff prefix (reply or user id)\n"
    "/list_users - List mappings\n"
    "/remove_user - Remove a mapping\n\n"
    "<b>USDT configuration:</b>\n"
    "/set_receiving_usdt_acc - Default receiving account\n"
    "/show_receiving_usdt_acc - Show current default\n"
    "/set_usdt_bank, /edit_usdt_bank, /remove_usdt_bank, /list_usdt_banks\n\n"
    "<b>MMK banks:</b>\n"
    "/set_mmk_bank, /edit_mmk_bank, /remove_mmk_bank, /list_mmk_bank"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await get_services(context).notifier.command_reply(HELP_TEXT)


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    db_ok = True
    try:
        await svc.db.fetchone("SELECT 1")
    except Exception:
        db_ok = False
    uptime = int(time.monotonic() - _STARTED_AT)
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    await svc.notifier.command_reply(
        f"🩺 <b>Health</b>\n\n"
        f"<b>Uptime:</b> {hours}h {minutes}m {seconds}s\n"
        f"<b>Database:</b> {'✅ ok' if db_ok else '❌ unreachable'}\n"
        f"<b>Balance loaded:</b> {'✅ yes' if svc.ledger.is_loaded else '❌ no'}\n"
        f"<b>Config:</b> {esc(svc.settings.summary())}"
    )


async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    try:
        limit = min(25, max(1, int(context.args[0]))) if context.args else 10
    except ValueError:
        limit = 10
    entries = await svc.audit.recent(limit)
    if not entries:
        await svc.notifier.command_reply("📒 Audit log is empty.")
        return
    lines = [f"📒 <b>Last {len(entries)} balance changes</b>\n"]
    for e in entries:
        actor = f" — {esc(e['actor_name'])}" if e["actor_name"] else ""
        lines.append(f"• <code>{esc(e['created_at'])}</code> [{esc(e['tx_type'])}]{actor}\n"
                     f"  {esc(e['description'])}")
    await svc.notifier.command_reply("\n".join(lines))


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    settings = svc.settings
    message = update.message
    thread_id = message.message_thread_id
    normalized = thread_id if thread_id is not None else 1

    usdt_location = (
        f"Topic {settings.usdt_transfers_topic_id}"
        if settings.usdt_transfers_topic_id and settings.usdt_transfers_topic_id > 1
        else "Main chat (General topic)"
    )
    text = (
        f"🧪 <b>Connection Test</b>\n\n"
        f"<b>Current message:</b>\n"
        f"• Chat ID: <code>{message.chat.id}</code>\n"
        f"• Thread ID: <code>{thread_id}</code> (normalized: {normalized})\n\n"
        f"<b>Bot configuration:</b>\n"
        f"• Target group: <code>{settings.target_group_id}</code>\n"
        f"• USDT transfers: {usdt_location}\n"
        f"• Auto balance topic: {settings.auto_balance_topic_id or 'main chat'}\n"
        f"• Alert topic: {settings.alert_topic_id or 'reply to message'}\n\n"
    )
    text += (
        "✅ In the correct group"
        if message.chat.id == settings.target_group_id
        else f"❌ Wrong group (expected {settings.target_group_id})"
    )
    await svc.notifier.reply(message, text, parse_mode="HTML")


# ------------------------------------------------------------------- balance

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    snapshot = svc.ledger.snapshot()
    if not snapshot or snapshot.is_empty:
        await svc.notifier.command_reply("❌ No balance loaded")
        return
    await svc.notifier.command_reply(
        f"📊 <b>Balance:</b>\n\n<pre>{esc(format_balance_message(snapshot))}</pre>"
    )


async def load_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    reply = update.message.reply_to_message
    if not reply or not reply.text:
        await svc.notifier.command_reply("Reply to a balance message with /load")
        return
    balances = parse_balance_message(reply.text)
    if not balances:
        await svc.notifier.command_reply("❌ Could not parse the balance message")
        return
    user = update.effective_user
    await svc.ledger.load(
        balances,
        actor_id=user.id if user else None,
        actor_name=(user.username or user.first_name) if user else None,
        ref_message_id=reply.message_id,
    )
    await svc.notifier.command_reply(
        f"✅ Loaded!\n\n"
        f"MMK banks: {len(balances.mmk)}\n"
        f"USDT banks: {len(balances.usdt)}"
        + (f"\nTHB banks: {len(balances.thb)}" if balances.thb else "")
    )


# --------------------------------------------------------------------- users

async def set_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    if not await _require_admin(svc, update):
        return
    message = update.message

    # Reply form: reply to the user's message with /set_user <prefix>
    if message.reply_to_message and message.reply_to_message.from_user and len(context.args) == 1:
        target = message.reply_to_message.from_user
        prefix = context.args[0]
        await svc.users.set_prefix(target.id, prefix, target.username or target.first_name)
        await svc.notifier.command_reply(
            f"✅ Set prefix '{esc(prefix)}' for @{esc(target.username or target.first_name)} "
            f"(ID: <code>{target.id}</code>)"
        )
        return

    if len(context.args) < 2:
        await svc.notifier.command_reply(
            "Usage:\n"
            "• Reply to the user's message with /set_user &lt;prefix&gt;\n"
            "• Or: /set_user &lt;user_id&gt; &lt;prefix&gt;\n\n"
            "Example: /set_user 123456789 San"
        )
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await svc.notifier.command_reply(
            "❌ Invalid user id. Use /set_user &lt;user_id&gt; &lt;prefix&gt; "
            "or reply to the user's message."
        )
        return
    prefix = context.args[1]
    await svc.users.set_prefix(user_id, prefix, None)
    await svc.notifier.command_reply(
        f"✅ Set prefix '{esc(prefix)}' for user ID <code>{user_id}</code>"
    )


async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    users = await svc.users.list_all()
    if not users:
        await svc.notifier.command_reply(
            "📋 <b>User-Prefix Mappings</b>\n\nNo users registered yet. Use /set_user."
        )
        return
    lines = ["📋 <b>User-Prefix Mappings</b>\n"]
    for idx, user in enumerate(users, 1):
        lines.append(
            f"<b>{idx}. {esc(user['prefix_name'])}</b> — @{esc(user['username'] or 'Unknown')} "
            f"(<code>{user['user_id']}</code>)"
        )
    await svc.notifier.command_reply("\n".join(lines))


async def remove_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    if not await _require_admin(svc, update):
        return
    if not context.args:
        await svc.notifier.command_reply(
            "Usage: /remove_user &lt;user_id&gt;\nUse /list_users to see ids."
        )
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await svc.notifier.command_reply("❌ Invalid user ID — must be numeric.")
        return
    prefix = await svc.users.get_prefix(user_id)
    if not prefix:
        await svc.notifier.command_reply(f"❌ User ID {user_id} not found in mappings.")
        return
    await svc.users.remove(user_id)
    await svc.notifier.command_reply(
        f"✅ Removed mapping: <code>{user_id}</code> → {esc(prefix)}"
    )


# ----------------------------------------------------------- receiving wallet

async def set_receiving_usdt_acc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    if not context.args:
        current = await svc.app_settings.get_receiving_usdt_account()
        await svc.notifier.command_reply(
            f"📊 <b>Current receiving USDT account:</b> <code>{esc(current)}</code>\n\n"
            f"Usage: /set_receiving_usdt_acc &lt;account_name&gt;\n"
            f"Example: /set_receiving_usdt_acc ACT(Wallet)"
        )
        return
    if not await _require_admin(svc, update):
        return
    account = " ".join(context.args)
    await svc.app_settings.set_receiving_usdt_account(account)
    await svc.notifier.command_reply(
        f"✅ Receiving USDT account updated to <code>{esc(account)}</code>. "
        f"Buy transactions will credit this account when the wallet can't be detected."
    )


async def show_receiving_usdt_acc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    current = await svc.app_settings.get_receiving_usdt_account()
    await svc.notifier.command_reply(
        f"📊 <b>Receiving USDT account:</b> <code>{esc(current)}</code>"
    )


# ----------------------------------------------------------------- MMK banks

def _parse_pipe_args(args: list[str], expected: int) -> Optional[list[str]]:
    parts = [p.strip() for p in " ".join(args).split("|")]
    return parts if len(parts) == expected else None


async def set_mmk_bank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    if not context.args:
        accounts = await svc.bank_accounts.list_mmk()
        listing = (
            "\n".join(
                f"• <code>{esc(a['bank_name'])}</code> — {esc(a['account_number'])} "
                f"({esc(a['account_holder'])})"
                for a in accounts
            )
            or "None registered yet."
        )
        await svc.notifier.command_reply(
            f"🏦 <b>Registered MMK bank accounts:</b>\n{listing}\n\n"
            f"Usage: /set_mmk_bank &lt;bank_name&gt; | &lt;account_number&gt; | &lt;holder&gt;\n"
            f"Example: /set_mmk_bank San(KBZ) | 27251127201844001 | CHAW SU THU ZAR"
        )
        return
    if not await _require_admin(svc, update):
        return
    parts = _parse_pipe_args(context.args, 3)
    if not parts:
        await svc.notifier.command_reply(
            "❌ Invalid format. Use: /set_mmk_bank &lt;bank_name&gt; | "
            "&lt;account_number&gt; | &lt;holder&gt; (pipe-separated)."
        )
        return
    bank_name, account_number, holder = parts[0], parts[1].replace(" ", ""), parts[2]
    if "(" not in bank_name or ")" not in bank_name:
        await svc.notifier.command_reply(
            "❌ Bank name must look like <code>Prefix(BankName)</code>, e.g. San(KBZ)."
        )
        return
    await svc.bank_accounts.set_mmk(bank_name, account_number, holder)
    await svc.notifier.command_reply(
        f"✅ <b>MMK bank registered</b>\n\n"
        f"<b>Bank:</b> <code>{esc(bank_name)}</code>\n"
        f"<b>Account:</b> <code>{esc(account_number)}</code>\n"
        f"<b>Holder:</b> <code>{esc(holder)}</code>"
    )


async def edit_mmk_bank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    if not context.args:
        await svc.notifier.command_reply(
            "Usage: /edit_mmk_bank &lt;bank_name&gt; | &lt;new_account&gt; | &lt;new_holder&gt;"
        )
        return
    if not await _require_admin(svc, update):
        return
    parts = _parse_pipe_args(context.args, 3)
    if not parts:
        await svc.notifier.command_reply("❌ Invalid format — three pipe-separated parts required.")
        return
    bank_name, account_number, holder = parts[0], parts[1].replace(" ", ""), parts[2]
    existing = await svc.bank_accounts.get_mmk(bank_name)
    if not existing:
        await svc.notifier.command_reply(
            f"❌ <code>{esc(bank_name)}</code> is not registered. Use /set_mmk_bank first."
        )
        return
    await svc.bank_accounts.set_mmk(bank_name, account_number, holder)
    await svc.notifier.command_reply(
        f"✅ <b>MMK bank updated:</b> <code>{esc(bank_name)}</code>\n\n"
        f"<b>Old:</b> {esc(existing['account_number'])} ({esc(existing['account_holder'])})\n"
        f"<b>New:</b> {esc(account_number)} ({esc(holder)})"
    )


async def remove_mmk_bank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    if not context.args:
        accounts = await svc.bank_accounts.list_mmk()
        listing = "\n".join(f"• <code>{esc(a['bank_name'])}</code>" for a in accounts) or "None."
        await svc.notifier.command_reply(
            f"🏦 <b>Registered MMK banks:</b>\n{listing}\n\nUsage: /remove_mmk_bank &lt;bank_name&gt;"
        )
        return
    if not await _require_admin(svc, update):
        return
    bank_name = " ".join(context.args)
    existing = await svc.bank_accounts.get_mmk(bank_name)
    if not existing:
        await svc.notifier.command_reply(f"❌ <code>{esc(bank_name)}</code> is not registered.")
        return
    await svc.bank_accounts.remove_mmk(bank_name)
    await svc.notifier.command_reply(
        f"✅ Removed MMK bank <code>{esc(bank_name)}</code> "
        f"({esc(existing['account_number'])}, {esc(existing['account_holder'])})"
    )


async def list_mmk_bank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    accounts = await svc.bank_accounts.list_mmk()
    if not accounts:
        await svc.notifier.command_reply(
            "📋 No MMK banks registered. Use /set_mmk_bank to add one."
        )
        return
    lines = ["📋 <b>Registered MMK bank accounts</b>\n"]
    for a in accounts:
        lines.append(
            f"• <code>{esc(a['bank_name'])}</code>\n"
            f"  Account: {esc(a['account_number'])}\n"
            f"  Holder: {esc(a['account_holder'])}"
        )
    await svc.notifier.command_reply("\n".join(lines))


# ---------------------------------------------------------------- USDT banks

async def set_usdt_bank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    if not context.args:
        wallets = await svc.bank_accounts.list_usdt()
        listing = (
            "\n".join(
                f"• <code>{esc(w['bank_name'])}</code> — {esc(w['network'])}\n"
                f"  <code>{esc(w['wallet_address'])}</code>"
                for w in wallets
            )
            or "None registered yet."
        )
        await svc.notifier.command_reply(
            f"💱 <b>Registered USDT wallets:</b>\n{listing}\n\n"
            f"Usage: /set_usdt_bank &lt;name&gt; | &lt;wallet_address&gt; | &lt;network&gt;\n"
            f"Example: /set_usdt_bank ACT(Tron Wallet) | TCFKANz7... | Tron"
        )
        return
    if not await _require_admin(svc, update):
        return
    parts = _parse_pipe_args(context.args, 3)
    if not parts:
        await svc.notifier.command_reply(
            "❌ Invalid format. Use: /set_usdt_bank &lt;name&gt; | &lt;wallet&gt; | &lt;network&gt;."
        )
        return
    bank_name, wallet, network = parts[0], parts[1].replace(" ", ""), parts[2]
    await svc.bank_accounts.set_usdt(bank_name, wallet, network)
    await svc.notifier.command_reply(
        f"✅ <b>USDT wallet registered</b>\n\n"
        f"<b>Name:</b> <code>{esc(bank_name)}</code>\n"
        f"<b>Wallet:</b> <code>{esc(wallet)}</code>\n"
        f"<b>Network:</b> {esc(network)}"
    )


async def edit_usdt_bank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    if not context.args:
        await svc.notifier.command_reply(
            "Usage: /edit_usdt_bank &lt;name&gt; | &lt;new_wallet&gt; | &lt;new_network&gt;"
        )
        return
    if not await _require_admin(svc, update):
        return
    parts = _parse_pipe_args(context.args, 3)
    if not parts:
        await svc.notifier.command_reply("❌ Invalid format — three pipe-separated parts required.")
        return
    bank_name, wallet, network = parts[0], parts[1].replace(" ", ""), parts[2]
    existing = await svc.bank_accounts.get_usdt(bank_name)
    if not existing:
        await svc.notifier.command_reply(
            f"❌ <code>{esc(bank_name)}</code> is not registered. Use /set_usdt_bank first."
        )
        return
    await svc.bank_accounts.set_usdt(bank_name, wallet, network)
    await svc.notifier.command_reply(
        f"✅ <b>USDT wallet updated:</b> <code>{esc(bank_name)}</code>\n\n"
        f"<b>Old:</b> {esc(existing['wallet_address'])} ({esc(existing['network'])})\n"
        f"<b>New:</b> {esc(wallet)} ({esc(network)})"
    )


async def remove_usdt_bank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    if not context.args:
        wallets = await svc.bank_accounts.list_usdt()
        listing = "\n".join(f"• <code>{esc(w['bank_name'])}</code>" for w in wallets) or "None."
        await svc.notifier.command_reply(
            f"💱 <b>Registered USDT wallets:</b>\n{listing}\n\nUsage: /remove_usdt_bank &lt;name&gt;"
        )
        return
    if not await _require_admin(svc, update):
        return
    bank_name = " ".join(context.args)
    existing = await svc.bank_accounts.get_usdt(bank_name)
    if not existing:
        await svc.notifier.command_reply(f"❌ <code>{esc(bank_name)}</code> is not registered.")
        return
    await svc.bank_accounts.remove_usdt(bank_name)
    await svc.notifier.command_reply(
        f"✅ Removed USDT wallet <code>{esc(bank_name)}</code> "
        f"({esc(existing['wallet_address'])}, {esc(existing['network'])})"
    )


async def list_usdt_banks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    wallets = await svc.bank_accounts.list_usdt()
    if not wallets:
        await svc.notifier.command_reply(
            "📋 No USDT wallets registered. Use /set_usdt_bank to add one."
        )
        return
    lines = ["📋 <b>Registered USDT wallets</b>\n"]
    for w in wallets:
        lines.append(
            f"• <code>{esc(w['bank_name'])}</code> ({esc(w['network'])})\n"
            f"  <code>{esc(w['wallet_address'])}</code>"
        )
    await svc.notifier.command_reply("\n".join(lines))
