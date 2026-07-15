"""Debounced collection of Telegram media groups (albums).

Telegram delivers each photo of an album as a separate update with a shared
``media_group_id`` and no "album complete" signal. v1 used fixed sleeps
(1.5s here, 8s there): too short loses photos on slow networks, too long
makes every album feel sluggish.

The collector instead **debounces**: each arriving item resets a short quiet
timer, and the group is flushed once no new photo has arrived for
``quiet_seconds`` (bounded by ``max_wait_seconds`` so a stuck stream can't
hold a group forever). Faster in the common case, more reliable in the slow
one.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

FlushCallback = Callable[[list[Any], dict[str, Any]], Awaitable[None]]


@dataclass
class _Group:
    items: list[Any]
    meta: dict[str, Any]
    callback: FlushCallback
    started_at: float
    timer: Optional[asyncio.Task] = None
    done: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MediaGroupCollector:
    def __init__(self, quiet_seconds: float = 2.5, max_wait_seconds: float = 20.0):
        self._quiet = quiet_seconds
        self._max_wait = max_wait_seconds
        self._groups: dict[str, _Group] = {}

    def has(self, key: str) -> bool:
        return key in self._groups

    def start(self, key: str, first_item: Any, meta: dict[str, Any], callback: FlushCallback) -> bool:
        """Begin collecting a group. Returns False if it already exists."""
        if key in self._groups:
            return False
        group = _Group(
            items=[first_item], meta=meta, callback=callback, started_at=time.monotonic()
        )
        self._groups[key] = group
        self._reschedule(key, group)
        logger.info("Media group %s: collecting (first item)", key)
        return True

    def add(self, key: str, item: Any) -> bool:
        """Append to an existing group. Returns False if the group is unknown
        (caller decides whether that photo means something else)."""
        group = self._groups.get(key)
        if group is None or group.done:
            return False
        group.items.append(item)
        self._reschedule(key, group)
        logger.info("Media group %s: %d items collected", key, len(group.items))
        return True

    def _reschedule(self, key: str, group: _Group) -> None:
        if group.timer and not group.timer.done():
            group.timer.cancel()
        elapsed = time.monotonic() - group.started_at
        delay = min(self._quiet, max(0.1, self._max_wait - elapsed))
        group.timer = asyncio.create_task(self._flush_after(key, delay))

    async def _flush_after(self, key: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # a newer photo rescheduled us
        group = self._groups.get(key)
        if group is None or group.done:
            return
        async with group.lock:
            if group.done:
                return
            group.done = True
        self._groups.pop(key, None)
        logger.info("Media group %s: flushing %d items", key, len(group.items))
        try:
            await group.callback(group.items, group.meta)
        except Exception:
            logger.error("Media group %s callback failed:\n%s", key, traceback.format_exc())
