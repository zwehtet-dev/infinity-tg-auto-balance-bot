"""Outbound messaging: alerts, statuses, balance posts, command replies.

Centralizes three things v1 scattered across every handler:

- routing (alert topic vs. reply vs. main chat),
- **HTML escaping** of user-controlled values (usernames, bank names, free
  text). v1 interpolated these raw into ``parse_mode='HTML'`` messages — a
  markup-injection bug that could break or spoof bot output,
- retry-once on transient Telegram send errors, and a log-only fallback so a
  failed notification never crashes transaction processing.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Optional

from telegram import Bot, Message
from telegram.error import TelegramError

from config import Settings

logger = logging.getLogger(__name__)


def esc(value: object) -> str:
    """HTML-escape any user-controlled value before interpolation."""
    return html.escape(str(value), quote=False)


class Notifier:
    def __init__(self, bot: Bot, settings: Settings):
        self._bot = bot
        self._settings = settings

    async def _send(
        self,
        text: str,
        thread_id: Optional[int],
        parse_mode: Optional[str],
        reply_to: Optional[Message] = None,
    ) -> None:
        for attempt in (1, 2):
            try:
                if reply_to is not None:
                    await reply_to.reply_text(text, parse_mode=parse_mode)
                else:
                    await self._bot.send_message(
                        chat_id=self._settings.target_group_id,
                        message_thread_id=thread_id or None,
                        text=text,
                        parse_mode=parse_mode,
                    )
                return
            except TelegramError as e:
                logger.warning("Send failed (attempt %d): %s", attempt, e)
                if attempt == 1:
                    await asyncio.sleep(1.0)
        logger.error("Dropped outgoing message after retries: %.120s", text)

    async def alert(self, text: str, reply_to: Optional[Message] = None) -> None:
        """Error/warning. Goes to the alert topic when configured, else as a
        reply to the triggering message."""
        if self._settings.alert_topic_id:
            await self._send(text, self._settings.alert_topic_id, None)
        elif reply_to is not None:
            await self._send(text, None, None, reply_to=reply_to)
        else:
            await self._send(text, None, None)

    async def status(self, text: str, parse_mode: Optional[str] = "HTML") -> None:
        """Success / progress / info — alert topic (or main chat)."""
        await self._send(text, self._settings.alert_topic_id or None, parse_mode)

    async def command_reply(self, text: str, parse_mode: Optional[str] = "HTML") -> None:
        await self._send(text, self._settings.alert_topic_id or None, parse_mode)

    async def post_balance(self, balance_text: str) -> None:
        """Publish the updated sheet to the auto-balance topic."""
        await self._send(balance_text, self._settings.auto_balance_topic_id or None, None)
