"""
Playwright browser lifecycle management.
All automation modules receive a BrowserContext from here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from app.config import Config


class BrowserManager:
    """Manages a single Chromium browser instance for the crawler session."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def start(self) -> BrowserContext:
        """Launch Chromium and create a persistent context."""
        logger.info("Starting Playwright browser (headless={})", Config.PLAYWRIGHT_HEADLESS)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=Config.PLAYWRIGHT_HEADLESS,
            slow_mo=Config.PLAYWRIGHT_SLOW_MO,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
            locale="vi-VN",
        )
        self._context.set_default_timeout(Config.PLAYWRIGHT_TIMEOUT)
        logger.info("Browser context ready.")
        return self._context

    async def new_page(self) -> Page:
        if not self._context:
            await self.start()
        return await self._context.new_page()  # type: ignore[union-attr]

    async def close(self) -> None:
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:
            logger.warning("Error during browser close: {}", exc)
        finally:
            self._context = None
            self._browser = None
            self._playwright = None
            logger.info("Browser closed.")

    async def screenshot(self, page: Page, name: str) -> Path:
        """Capture a screenshot and save to logs/screenshots/."""
        path = Config.SCREENSHOT_PATH / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.debug("Screenshot saved: {}", path)
        return path