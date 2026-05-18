"""
Invoice search page automation.
Navigates to the search page, sets date filters, and triggers search.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from loguru import logger
from playwright.async_api import Page

from app.config import Config


SEARCH_URL = Config.GDT_SEARCH_URL


async def navigate_to_search(page: Page, emit_fn: Optional[Callable] = None) -> bool:
    """Navigate to the invoice search page and tick the required checkbox."""

    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    try:
        emit("Navigating to invoice search page…")
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30_000)
        await asyncio.sleep(2)
        emit("Search page loaded.")
        return True

    except Exception as exc:
        logger.error("Failed to navigate to search page: {}", exc)
        emit(f"Navigation failed: {exc}")
        return False


async def set_date_filter(
    page: Page,
    start_date: str,
    end_date: str,
    emit_fn: Optional[Callable] = None,
) -> bool:
    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    try:
        emit(f"Setting date range: {start_date} → {end_date}")

        await _fill_date_picker(page, "#tngay", start_date)
        await asyncio.sleep(0.3)

        await _fill_date_picker(page, "#dngay", end_date)
        await asyncio.sleep(0.3)

        emit(f"Date range set: {start_date} → {end_date}")
        return True

    except Exception as exc:
        logger.error("Failed to set date filter: {}", exc)
        emit(f"Date filter error: {exc}")
        return False

async def _fill_date_picker(page: Page, container_selector: str, date_str: str) -> None:
    """
    Click date picker container, then fill the focused textbox (nth(2)).
    Recorded from Playwright inspector.
    """
    # Click vào container để mở picker và focus input
    container = page.locator(container_selector).get_by_role("textbox", name="Chọn thời điểm")
    await container.click()
    await asyncio.sleep(0.3)

    # Sau khi click, input active luôn là nth(2)
    inp = page.get_by_role("textbox", name="Chọn thời điểm").nth(2)
    await inp.press("Control+a")
    await inp.fill(date_str)
    await inp.press("Enter")

async def perform_search(page: Page, emit_fn: Optional[Callable] = None) -> bool:
    """Click the search button and wait for results."""

    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    for attempt in range(1, 4):
        try:
            emit(f"Clicking search button (attempt {attempt})…")
            btn = page.locator('button[type="submit"]:has-text("Tìm kiếm")')
            await btn.click()

            # Wait for loading overlay to disappear and table to appear
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await asyncio.sleep(2)

            # Check if results table appeared
            table = page.locator("tbody.ant-table-tbody")
            if await table.count() > 0:
                emit("✓ Search complete — results table visible.")
                return True

            emit("Search table not visible yet, waiting…")
            await asyncio.sleep(3)

        except Exception as exc:
            logger.warning("Search attempt {} failed: {}", attempt, exc)
            await asyncio.sleep(2)

    emit("❌ Search failed after 3 attempts.")
    return False


async def set_page_size(page: Page, size: int = 50) -> None:
    """Change the results per-page selector to *size*."""
    try:
        selector_trigger = page.locator(".ant-select-selection")
        if await selector_trigger.count() > 0:
            await selector_trigger.last.click()
            await asyncio.sleep(0.5)
            option = page.locator(".ant-select-dropdown-menu-item", has_text=str(size))
            if await option.count() > 0:
                await option.click()
                await asyncio.sleep(1)
                logger.info("Page size set to {}", size)
    except Exception as exc:
        logger.warning("Could not set page size: {}", exc)


async def get_total_rows(page: Page) -> int:
    """Attempt to read the total result count from the pagination info."""
    try:
        # Ant Design pagination typically shows: "Total X items"
        info = page.locator(".ant-pagination-total-text, .total-text")
        if await info.count() > 0:
            text = await info.first.inner_text()
            import re
            numbers = re.findall(r"\d+", text)
            if numbers:
                return int(numbers[-1])
    except Exception:
        pass

    # Fallback: count rows
    rows = page.locator("tbody.ant-table-tbody tr.ant-table-row")
    return await rows.count()