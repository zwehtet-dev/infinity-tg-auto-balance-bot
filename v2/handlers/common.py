"""Shared helpers for transaction handlers."""

from __future__ import annotations

import base64
import logging
from typing import Iterable, Union

from telegram import Bot, PhotoSize

from services import Services

logger = logging.getLogger(__name__)

# A receipt image can arrive as a live Telegram photo, a path to a photo we
# stored on disk, or raw bytes. Handlers accept any mix.
PhotoSource = Union[PhotoSize, str, bytes]

LOW_CONFIDENCE = 50.0


async def to_b64_list(svc: Services, bot: Bot, photos: Iterable[PhotoSource]) -> list[str]:
    """Normalize any mix of photo sources into base64 strings for OCR.

    Failures on individual photos are logged and skipped so one broken
    download doesn't sink a whole media group.
    """
    images: list[str] = []
    for photo in photos:
        try:
            if isinstance(photo, PhotoSize):
                images.append(await svc.download_photo_b64(bot, photo))
            elif isinstance(photo, str):
                images.append(Services.read_file_b64(photo))
            elif isinstance(photo, (bytes, bytearray)):
                images.append(base64.b64encode(bytes(photo)).decode("utf-8"))
        except Exception as e:
            logger.error("Failed to load receipt photo: %s", e)
    return images


def mmk_mismatch(expected: float, actual: float, ratio: float = 0.1) -> bool:
    """MMK amounts differ beyond tolerance (flat 1000 MMK or a fraction)."""
    if not expected or expected <= 0:
        return False
    return abs(actual - expected) > max(1000.0, expected * ratio)


def usdt_mismatch(expected: float, actual: float) -> bool:
    if not expected or expected <= 0:
        return False
    return abs(actual - expected) > max(0.5, expected * 0.01)
