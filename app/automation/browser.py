from __future__ import annotations

from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.config import Config


class BrowserManager:
    def __init__(self, session_file: Optional[str] = None) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._session_path: Path = Path(session_file) if session_file else Config.SESSION_PATH

    @property
    def has_saved_session(self) -> bool:
        return self._session_path.exists() and self._session_path.stat().st_size > 0

    async def start(self) -> BrowserContext:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=Config.PLAYWRIGHT_HEADLESS,
            slow_mo=Config.PLAYWRIGHT_SLOW_MO,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--disable-extensions"],
        )
        context_kwargs = dict(
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
            locale="vi-VN",
            ignore_https_errors=True,
        )
        if self.has_saved_session:
            context_kwargs["storage_state"] = str(self._session_path)

        self._context = await self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(Config.PLAYWRIGHT_TIMEOUT)
        return self._context

    async def save_session(self) -> Path:
        if not self._context:
            raise RuntimeError("Browser context chưa được khởi tạo.")
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(self._session_path))
        return self._session_path

    def clear_session(self) -> None:
        if self._session_path.exists():
            self._session_path.unlink()

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
            self._context = self._browser = self._playwright = None

    async def screenshot(self, page: Page, name: str) -> Path:
        path = Config.SCREENSHOT_PATH / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        return path