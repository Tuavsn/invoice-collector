from __future__ import annotations

import asyncio
import threading
from typing import Callable, Optional

from loguru import logger
from playwright.async_api import Page

from app.automation.captcha import get_captcha_image_b64, refresh_captcha
from app.automation.browser import BrowserManager
from app.config import Config

_MAX_ATTEMPTS    = Config.CRAWLER_MAX_RETRIES
_LOGIN_URL       = Config.GDT_LOGIN_URL
_CAPTCHA_TIMEOUT = 120
_LOGGED_IN_KEYWORDS  = ("tra-cuu", "quan-ly", "dashboard")
_LOGGED_IN_SELECTORS = ".ant-dropdown-trigger, .user-info, #logout"


async def ensure_logged_in(
    page: Page,
    browser_manager: BrowserManager,
    username: str,
    password: str,
    emit_fn: Optional[Callable[[str], None]] = None,
    emit_captcha_fn: Optional[Callable[[str], None]] = None,
    captcha_event: Optional[threading.Event] = None,
    get_captcha_answer: Optional[Callable[[], str]] = None,
) -> bool:
    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    if browser_manager.has_saved_session:
        emit("Phát hiện session cũ — kiểm tra còn hợp lệ không…")
        if await _check_session_valid(page, emit):
            return True
        emit("Session hết hạn — xóa và đăng nhập lại…")
        browser_manager.clear_session()

    success = await attempt_login(
        page=page, username=username, password=password,
        emit_fn=emit_fn, emit_captcha_fn=emit_captcha_fn,
        captcha_event=captcha_event, get_captcha_answer=get_captcha_answer,
    )
    if success:
        await browser_manager.save_session()
    return success


async def attempt_login(
    page: Page,
    username: str,
    password: str,
    emit_fn: Optional[Callable[[str], None]] = None,
    emit_captcha_fn: Optional[Callable[[str], None]] = None,
    captcha_event: Optional[threading.Event] = None,
    get_captcha_answer: Optional[Callable[[], str]] = None,
) -> bool:
    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        emit(f"Login attempt {attempt}/{_MAX_ATTEMPTS}…")
        try:
            await page.goto(_LOGIN_URL, wait_until="networkidle", timeout=30_000)

            try:
                close_btn = await page.wait_for_selector("button.ant-modal-close", timeout=3_000)
                if close_btn:
                    await close_btn.click()
                    await page.wait_for_selector(".ant-modal", state="hidden", timeout=3_000)
            except Exception:
                pass

            await page.click(".home-header-menu-item span:has-text('Đăng nhập')")
            modal = page.get_by_role("dialog")
            await modal.wait_for(state="visible", timeout=15_000)

            await modal.locator("input#username").fill(username)
            await modal.locator("input#password").fill(password)

            b64 = await get_captcha_image_b64(modal)
            if not b64:
                emit("Không chụp được captcha — thử lại…")
                continue

            if emit_captcha_fn:
                emit_captcha_fn(b64)

            if captcha_event:
                captcha_event.clear()
                timed_out = await _wait_for_event(captcha_event, _CAPTCHA_TIMEOUT)
                if timed_out:
                    await refresh_captcha(modal)
                    continue

            captcha_text = get_captcha_answer() if get_captcha_answer else ""
            if not captcha_text:
                await refresh_captcha(modal)
                continue

            await modal.locator("input#cvalue").fill(captcha_text)
            await modal.locator("button[type='submit']").click()
            await asyncio.sleep(2)

            if await _is_logged_in(page):
                emit("✓ Login thành công.")
                return True

            error_text = await _get_error_message(modal)
            emit(f"Login thất bại: {error_text}")
            await refresh_captcha(modal)
            await asyncio.sleep(1)

        except Exception as exc:
            logger.exception("Login attempt {} raised: {}", attempt, exc)
            await asyncio.sleep(2)

    emit("Đã hết số lần thử login.")
    return False


async def _check_session_valid(page: Page, emit: Callable[[str], None]) -> bool:
    try:
        await page.goto(_LOGIN_URL, wait_until="networkidle", timeout=30_000)
        if await _is_logged_in(page):
            emit("✓ Session còn hợp lệ.")
            return True
    except Exception as exc:
        logger.warning("Kiểm tra session thất bại: {}", exc)
    return False


async def _wait_for_event(event: threading.Event, timeout: float) -> bool:
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: event.wait(timeout=timeout))
    return not result


async def _is_logged_in(page: Page) -> bool:
    if any(k in page.url for k in _LOGGED_IN_KEYWORDS):
        return True
    try:
        return await page.query_selector(_LOGGED_IN_SELECTORS) is not None
    except Exception:
        return False


async def _get_error_message(modal) -> str:
    try:
        el = modal.locator(".ant-alert-message, .error-message, .login-error").first
        await el.wait_for(state="visible", timeout=3_000)
        return (await el.inner_text()).strip()
    except Exception:
        return "unknown error"