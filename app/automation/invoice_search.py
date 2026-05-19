"""
Invoice search page automation.
Navigates to the search page, sets date filters, and triggers search.

Cấu trúc tab 2 cấp:
  Cấp 1 — Tab tìm kiếm chính:
    • Tra cứu hóa đơn điện tử bán ra   (SALE)
    • Tra cứu hóa đơn điện tử mua vào  (PURCHASE)

  Cấp 2 — Sub-tab bên trong mỗi tab cấp 1:
    • Hóa đơn điện tử              (EINVOICE)
    • Hóa đơn có mã khởi tạo từ máy tính tiền  (POS)

Thứ tự crawl được định nghĩa trong CRAWL_PLAN (xem cuối file).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from loguru import logger
from playwright.async_api import Page

from app.config import Config


SEARCH_URL = Config.GDT_SEARCH_URL


# ──────────────────────────────────────────────────────────────────────────────
# Data-classes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SubTabConfig:
    """Một sub-tab bên trong tab tìm kiếm cấp 1."""
    name: str           # Tên hiển thị cho log
    tab_label: str      # Substring trên nút tab cấp 2
    panel_text: str     # Text unique trong tabpanel cấp 2 (dùng để scope locator)
    invoice_type: str   # Giá trị lưu DB: "sale_einvoice" | "sale_pos" | "purchase_einvoice" | "purchase_pos"


@dataclass
class MainTabConfig:
    """Tab tìm kiếm cấp 1 (Bán ra / Mua vào)."""
    name: str               # Tên hiển thị cho log
    tab_label: str          # Substring trên nút tab cấp 1
    panel_text: str         # Text unique trong tabpanel cấp 1
    sub_tabs: List[SubTabConfig] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Cấu hình các tab — thứ tự trong list = thứ tự crawl
# ──────────────────────────────────────────────────────────────────────────────

CRAWL_PLAN: List[MainTabConfig] = [
    MainTabConfig(
        name="Bán ra",
        tab_label="Tra cứu hóa đơn điện tử bán",
        panel_text="Danh sách hóa đơn điện tử bán",
        sub_tabs=[
            SubTabConfig(
                name="Bán ra / Hóa đơn điện tử",
                tab_label="Hóa đơn điện tử",
                panel_text="Danh sách hóa đơn điện tử bán",
                invoice_type="sale_einvoice",
            ),
            SubTabConfig(
                name="Bán ra / Máy tính tiền",
                tab_label="Hóa đơn có mã khởi tạo từ máy tính tiền",
                panel_text="Danh sách hóa đơn có mã khởi tạo từ máy tính tiền",
                invoice_type="sale_pos",
            ),
        ],
    ),
    MainTabConfig(
        name="Mua vào",
        tab_label="Tra cứu hóa đơn điện tử mua",
        panel_text="Danh sách hóa đơn điện tử mua",
        sub_tabs=[
            SubTabConfig(
                name="Mua vào / Hóa đơn điện tử",
                tab_label="Hóa đơn điện tử",
                panel_text="Danh sách hóa đơn điện tử mua",
                invoice_type="purchase_einvoice",
            ),
            SubTabConfig(
                name="Mua vào / Máy tính tiền",
                tab_label="Hóa đơn có mã khởi tạo từ máy tính tiền",
                panel_text="Danh sách hóa đơn có mã khởi tạo từ máy tính tiền",
                invoice_type="purchase_pos",
            ),
        ],
    ),
]

# Legacy alias — giữ backward compat với code cũ import TAB_CONFIGS
TAB_CONFIGS = CRAWL_PLAN


# ──────────────────────────────────────────────────────────────────────────────
# Navigation
# ──────────────────────────────────────────────────────────────────────────────

async def navigate_to_search(page: Page, emit_fn: Optional[Callable] = None) -> bool:
    """Navigate to the invoice search page directly via URL."""

    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    try:
        emit("Navigating to invoice search page…")
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30_000)
        await page.wait_for_selector(
            'button[type="submit"]:has-text("Tìm kiếm"), .ant-tabs, form',
            state="visible",
            timeout=15_000,
        )
        emit("Search page loaded.")
        return True

    except Exception as exc:
        logger.error("Failed to navigate to search page: {}", exc)
        emit(f"Navigation failed: {exc}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Panel scoping helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_main_panel(page: Page, main_tab: MainTabConfig):
    """Tabpanel cấp 1 (Bán ra / Mua vào)."""
    return page.get_by_role("tabpanel").filter(has_text=main_tab.panel_text)


def _get_sub_panel(page: Page, sub_tab: SubTabConfig):
    """Tabpanel cấp 2 (Hóa đơn điện tử / Máy tính tiền).
    Ant Design render tabpanel nào đang active ra DOM — filter bằng unique text."""
    return page.get_by_role("tabpanel").filter(has_text=sub_tab.panel_text)


# ──────────────────────────────────────────────────────────────────────────────
# Tab switching — cấp 1
# ──────────────────────────────────────────────────────────────────────────────

async def switch_to_main_tab(
    page: Page,
    main_tab: MainTabConfig,
    emit_fn: Optional[Callable] = None,
) -> bool:
    """Click tab tìm kiếm cấp 1 (Bán ra / Mua vào) và chờ panel visible."""

    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    try:
        tab_locator = page.get_by_role(
            "tab", name=re.compile(re.escape(main_tab.tab_label), re.IGNORECASE)
        )
        if await tab_locator.count() == 0:
            emit(f"Main tab '{main_tab.name}' not found — skipping.")
            return False

        await tab_locator.first.click()
        await _get_main_panel(page, main_tab).wait_for(state="visible", timeout=2_000)
        emit(f"Switched to main tab: {main_tab.name}")
        return True

    except Exception as exc:
        logger.warning("Main tab switch failed for '{}': {}", main_tab.name, exc)
        emit(f"Main tab switch warning ({main_tab.name}): {exc}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Tab switching — cấp 2
# ──────────────────────────────────────────────────────────────────────────────

async def switch_to_sub_tab(
    page: Page,
    main_tab: MainTabConfig,
    sub_tab: SubTabConfig,
    emit_fn: Optional[Callable] = None,
) -> bool:
    """Click sub-tab cấp 2 bên trong panel của main_tab đã active."""

    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    try:
        # Scope vào panel cấp 1 trước để tránh nhầm tab cùng tên ở panel khác
        main_panel = _get_main_panel(page, main_tab)

        sub_tab_locator = main_panel.get_by_role(
            "tab", name=re.compile(re.escape(sub_tab.tab_label), re.IGNORECASE)
        )
        if await sub_tab_locator.count() == 0:
            emit(f"Sub-tab '{sub_tab.name}' not found — skipping.")
            return False

        await sub_tab_locator.first.click()
        await _get_sub_panel(page, sub_tab).wait_for(state="visible", timeout=2_000)
        emit(f"Switched to sub-tab: {sub_tab.name}")
        return True

    except Exception as exc:
        logger.warning("Sub-tab switch failed for '{}': {}", sub_tab.name, exc)
        emit(f"Sub-tab switch warning ({sub_tab.name}): {exc}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Legacy alias — giữ để crawler_engine cũ không bị break
# ──────────────────────────────────────────────────────────────────────────────

async def switch_to_purchase_tab(page: Page, emit_fn: Optional[Callable] = None) -> bool:
    return await switch_to_main_tab(page, CRAWL_PLAN[1], emit_fn)  # index 1 = Mua vào


async def switch_to_tab(page: Page, tab, emit_fn: Optional[Callable] = None) -> bool:
    """Legacy shim — tab có thể là MainTabConfig hoặc SubTabConfig."""
    if isinstance(tab, MainTabConfig):
        return await switch_to_main_tab(page, tab, emit_fn)
    # Không còn context main_tab → best effort
    logger.warning("switch_to_tab() called with SubTabConfig — use switch_to_sub_tab() instead")
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Date filter — scoped vào main tab panel
# ──────────────────────────────────────────────────────────────────────────────

async def set_date_filter(
    page: Page,
    start_date: str,
    end_date: str,
    emit_fn: Optional[Callable] = None,
    main_tab: Optional[MainTabConfig] = None,
    # legacy kwarg — bỏ qua nếu được truyền vào
    tab: Optional[object] = None,
) -> bool:
    """Set date filter cho main_tab đang active. Mặc định tab Mua vào (legacy compat)."""

    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    # Resolve main_tab từ legacy `tab` kwarg nếu cần
    if main_tab is None:
        if isinstance(tab, MainTabConfig):
            main_tab = tab
        else:
            main_tab = CRAWL_PLAN[1]  # default: Mua vào (legacy)

    try:
        emit(f"[{main_tab.name}] Setting date range: {start_date} → {end_date}")
        panel = _get_main_panel(page, main_tab)

        await _fill_date_picker(page, panel, "#tngay", start_date)
        await _fill_date_picker(page, panel, "#dngay", end_date)

        emit(f"[{main_tab.name}] Date range set: {start_date} → {end_date}")
        return True

    except Exception as exc:
        logger.error("Failed to set date filter for '{}': {}", main_tab.name, exc)
        emit(f"Date filter error ({main_tab.name}): {exc}")
        return False


async def _fill_date_picker(page: Page, panel, container_selector: str, date_str: str) -> None:
    """Fill a date picker scoped to the given tabpanel.

    Follows the Playwright recording pattern:
        panel.locator('#tngay').getByPlaceholder('Chọn thời điểm').click()
        page.getByRole('textbox', { name: 'Chọn thời điểm' }).nth(2).fill(...)

    The `.nth(2)` global fallback is kept for the fill step because after the
    picker opens, Ant Design renders a floating input outside the tabpanel DOM.
    """
    inp_in_panel = panel.locator(container_selector).get_by_placeholder("Chọn thời điểm")
    await inp_in_panel.click()

    inp = page.get_by_role("textbox", name="Chọn thời điểm").nth(2)
    await inp.press("Control+a")
    await inp.fill(date_str)
    await inp.press("Enter")

    try:
        await page.locator(".ant-picker-dropdown").wait_for(state="hidden", timeout=3_000)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Search — scoped vào sub-tab panel
# ──────────────────────────────────────────────────────────────────────────────

async def perform_search(
    page: Page,
    emit_fn: Optional[Callable] = None,
    sub_tab: Optional[SubTabConfig] = None,
    # legacy kwarg
    tab: Optional[object] = None,
) -> bool:
    """Click search button scoped vào panel của sub_tab, chờ kết quả."""

    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    if sub_tab is None:
        if isinstance(tab, SubTabConfig):
            sub_tab = tab
        else:
            # legacy default: mua vào / hóa đơn điện tử
            sub_tab = CRAWL_PLAN[1].sub_tabs[0]

    for attempt in range(1, 4):
        try:
            emit(f"[{sub_tab.name}] Clicking search button (attempt {attempt})…")
            panel = _get_sub_panel(page, sub_tab)
            btn = panel.locator('button[type="submit"]:has-text("Tìm kiếm")')
            await btn.click()

            await page.wait_for_selector(
                "tbody.ant-table-tbody tr.ant-table-row",
                state="visible",
                timeout=30_000,
            )
            emit(f"[{sub_tab.name}] ✓ Search complete — results table visible.")
            return True

        except Exception as exc:
            logger.warning("[{}] Search attempt {} failed: {}", sub_tab.name, attempt, exc)
            if attempt < 3:
                await asyncio.sleep(2)

    emit(f"[{sub_tab.name}] ❌ Search failed after 3 attempts.")
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Page size
# ──────────────────────────────────────────────────────────────────────────────

async def set_page_size(page: Page, size: int = 50) -> None:
    """Change the results per-page selector."""
    try:
        combo = page.get_by_role("combobox").filter(has_text=re.compile(r"^\d+$")).last
        if await combo.count() == 0:
            logger.warning("Page size combobox not found")
            return

        await combo.click()

        await page.wait_for_selector(
            f'[role="option"]:has-text("{size}"), .ant-select-item-option:has-text("{size}")',
            state="visible",
            timeout=5_000,
        )

        option = page.get_by_role("option", name=str(size)).first
        if await option.count() == 0:
            option = page.locator(f'.ant-select-item-option:has-text("{size}")').first

        await option.click()
        await page.wait_for_load_state("networkidle", timeout=20_000)
        logger.info("Page size set to {}", size)

    except Exception as exc:
        logger.warning("Could not set page size: {}", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Total rows
# ──────────────────────────────────────────────────────────────────────────────

async def get_total_rows(page: Page) -> int:
    """Attempt to read the total result count from the pagination info."""
    try:
        info = page.locator(".ant-pagination-total-text, .total-text")
        if await info.count() > 0:
            text = await info.first.inner_text()
            numbers = re.findall(r"\d+", text)
            if numbers:
                return int(numbers[-1])
    except Exception:
        pass

    rows = page.locator("tbody.ant-table-tbody tr.ant-table-row")
    return await rows.count()