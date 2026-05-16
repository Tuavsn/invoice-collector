# captcha.py
"""
CAPTCHA image capture — chụp ảnh và trả về base64 để user tự nhập.
"""
from __future__ import annotations

import asyncio
import base64
from typing import Optional

from loguru import logger
from playwright.async_api import Locator

from app.config import Config


async def get_captcha_image_b64(
    modal: Locator,
    captcha_selector: str = "img[alt='captcha']",
) -> Optional[str]:
    """Chụp captcha trong modal và trả về chuỗi PNG base64."""
    try:
        element = modal.locator(captcha_selector).first
        await element.wait_for(state="visible", timeout=10_000)
        raw_bytes = await element.screenshot()
    except Exception as exc:
        logger.error("Failed to screenshot captcha '{}': {}", captcha_selector, exc)
        return None

    try:
        debug_path = Config.SCREENSHOT_PATH / "captcha_latest.png"
        debug_path.write_bytes(raw_bytes)
    except Exception:
        pass

    return base64.b64encode(raw_bytes).decode("utf-8")


async def refresh_captcha(
    modal: Locator,
    captcha_selector: str = "img[alt='captcha']",
) -> None:
    """Click vào ảnh captcha trong modal để load captcha mới."""
    try:
        element = modal.locator(captcha_selector).first
        await element.wait_for(state="visible", timeout=5_000)
        await element.click()
        await asyncio.sleep(1)
    except Exception as exc:
        logger.debug("Captcha refresh failed: {}", exc)