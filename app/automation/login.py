# login.py
"""
GDT portal login — hiển thị captcha cho user tự nhập qua UI.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Callable, Optional

from loguru import logger
from playwright.async_api import Page

from app.automation.captcha import get_captcha_image_b64, refresh_captcha
from app.automation.browser import BrowserManager
from app.config import Config

MAX_ATTEMPTS    = Config.CRAWLER_MAX_RETRIES
LOGIN_URL       = Config.GDT_LOGIN_URL
CAPTCHA_TIMEOUT = 120  # giây chờ user nhập tối đa

# URL chỉ xuất hiện khi đã đăng nhập
_LOGGED_IN_KEYWORDS = ("tra-cuu", "quan-ly", "dashboard")
# Selector chỉ xuất hiện khi đã đăng nhập
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
    """
    Entry point chính. Kiểm tra session còn sống không, nếu không thì login mới.
    Trả về True nếu đang ở trạng thái đã đăng nhập.
    """
    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    # Có session đã lưu → thử dùng lại
    if browser_manager.has_saved_session:
        emit("🔄 Phát hiện session cũ — kiểm tra còn hợp lệ không…")
        if await _check_session_valid(page, emit):
            return True

        # Session hết hạn → xóa và login lại
        emit("⚠ Session hết hạn — xóa và đăng nhập lại…")
        browser_manager.clear_session()

    # Login mới và lưu session nếu thành công
    success = await attempt_login(
        page=page,
        username=username,
        password=password,
        emit_fn=emit_fn,
        emit_captcha_fn=emit_captcha_fn,
        captcha_event=captcha_event,
        get_captcha_answer=get_captcha_answer,
    )

    if success:
        await browser_manager.save_session()

    return success


async def _check_session_valid(
    page: Page,
    emit: Callable[[str], None],
) -> bool:
    """
    Điều hướng đến portal, trả về True nếu vẫn đang đăng nhập.
    Không raise — mọi lỗi đều coi là session không hợp lệ.
    """
    try:
        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)
        if await _is_logged_in(page):
            emit("✓ Session còn hợp lệ — bỏ qua bước đăng nhập.")
            return True
    except Exception as exc:
        logger.warning("Kiểm tra session thất bại: {}", exc)
    return False


async def attempt_login(
    page: Page,
    username: str,
    password: str,
    emit_fn: Optional[Callable[[str], None]] = None,
    emit_captcha_fn: Optional[Callable[[str], None]] = None,
    captcha_event: Optional[threading.Event] = None,
    get_captcha_answer: Optional[Callable[[], str]] = None,
) -> bool:
    """
    Thực hiện login thủ công qua form + captcha.
    Trả về True nếu login thành công.
    Không tự lưu session — việc đó do ensure_logged_in() đảm nhận.
    """
    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        emit(f"Login attempt {attempt}/{MAX_ATTEMPTS}…")
        try:
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

            # Đóng modal thông báo nếu có
            try:
                close_btn = await page.wait_for_selector(
                    "button.ant-modal-close", timeout=3_000
                )
                if close_btn:
                    await close_btn.click()
                    await page.wait_for_selector(".ant-modal", state="hidden", timeout=3_000)
                    emit("Modal dismissed.")
            except Exception:
                pass

            # Mở form login và lấy modal làm root cho mọi thao tác
            await page.click(".home-header-menu-item span:has-text('Đăng nhập')")
            modal = page.get_by_role("dialog")
            await modal.wait_for(state="visible", timeout=15_000)

            # Điền credentials trong modal
            await modal.locator("input#username").fill(username)
            await modal.locator("input#password").fill(password)
            emit("Credentials filled.")

            # Lấy ảnh captcha và gửi lên UI
            b64 = await get_captcha_image_b64(modal)
            if not b64:
                emit("⚠ Không chụp được captcha — thử lại…")
                continue

            if emit_captcha_fn:
                emit_captcha_fn(b64)
            emit(f"🖼 Captcha đã gửi lên UI — chờ nhập (tối đa {CAPTCHA_TIMEOUT}s)…")

            # Chờ user nhập (threading.Event, không block event loop)
            if captcha_event:
                captcha_event.clear()
                timed_out = await _wait_for_threading_event(captcha_event, CAPTCHA_TIMEOUT)
                if timed_out:
                    emit("⚠ Quá thời gian chờ nhập captcha — refresh và thử lại…")
                    await refresh_captcha(modal)
                    continue

            captcha_text = get_captcha_answer() if get_captcha_answer else ""
            if not captcha_text:
                emit("⚠ Captcha rỗng — thử lại…")
                await refresh_captcha(modal)
                continue

            emit(f"⌨ Captcha nhận được: '{captcha_text}'")

            # Điền captcha và submit trong modal
            await modal.locator("input#cvalue").fill(captcha_text)
            await modal.locator("button[type='submit']").click()
            await asyncio.sleep(2)

            if await _is_logged_in(page):
                emit("✓ Login thành công.")
                return True

            error_text = await _get_error_message(modal)
            emit(f"✗ Login thất bại: {error_text} — thử lại…")
            await refresh_captcha(modal)
            await asyncio.sleep(1)

        except Exception as exc:
            logger.exception("Login attempt {} raised: {}", attempt, exc)
            emit(f"Lỗi attempt {attempt}: {exc}")
            await asyncio.sleep(2)

    emit("❌ Đã hết số lần thử login.")
    return False


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _wait_for_threading_event(
    event: threading.Event, timeout: float
) -> bool:
    """
    Chờ một threading.Event mà không block asyncio event loop.
    Trả về True nếu timeout, False nếu event được set đúng hạn.
    """
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: event.wait(timeout=timeout)
    )
    return not result  # event.wait() → True nếu set, False nếu timeout


async def _is_logged_in(page: Page) -> bool:
    if any(k in page.url for k in _LOGGED_IN_KEYWORDS):
        return True
    try:
        el = await page.query_selector(_LOGGED_IN_SELECTORS)
        return el is not None
    except Exception:
        return False


async def _get_error_message(modal) -> str:
    try:
        el = modal.locator(".ant-alert-message, .error-message, .login-error").first
        await el.wait_for(state="visible", timeout=2_000)
        return (await el.inner_text()).strip()
    except Exception:
        pass
    return "unknown error"