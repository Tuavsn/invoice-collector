from __future__ import annotations

import asyncio
import base64
from typing import Optional

from loguru import logger
from playwright.async_api import Locator

from app.config import Config

_CAPTCHA_SELECTOR = "img[alt='captcha']"


async def get_captcha_image_b64(modal: Locator) -> Optional[str]:
    try:
        element = modal.locator(_CAPTCHA_SELECTOR).first
        await element.wait_for(state="visible", timeout=10_000)
        raw_bytes = await element.screenshot()
    except Exception as exc:
        logger.error("Failed to screenshot captcha: {}", exc)
        return None

    try:
        (Config.SCREENSHOT_PATH / "captcha_latest.png").write_bytes(raw_bytes)
    except Exception:
        pass

    return base64.b64encode(raw_bytes).decode("utf-8")


async def refresh_captcha(modal: Locator) -> None:
    try:
        element = modal.locator(_CAPTCHA_SELECTOR).first
        await element.wait_for(state="visible", timeout=5_000)
        await element.click()
        await asyncio.sleep(1)
    except Exception as exc:
        logger.debug("Captcha refresh failed: {}", exc)