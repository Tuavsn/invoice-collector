"""
Invoice list export automation.
Handles downloading the invoice list Excel/CSV from the search results page.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Optional

from loguru import logger
from playwright.async_api import Page

from app.config import Config


async def export_invoice_list(
    page: Page,
    save_dir: Optional[Path] = None,
    emit_fn: Optional[Callable] = None,
) -> Optional[Path]:
    """
    Click the 'Xuất hóa đơn' (export invoice list) button and download the file.
    Returns the path to the downloaded file, or None on failure.
    """

    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    save_dir = save_dir or Config.DOWNLOAD_PATH
    save_dir.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, 4):
        try:
            emit(f"Exporting invoice list (attempt {attempt})…")

            # Locate export button by xpath — tooltip 'Xuất hóa đơn'
            export_btn = page.locator(
                'xpath=//*[@id="icon_ketxuat"]/ancestor::button'
            ).first

            if await export_btn.count() == 0:
                # Fallback: find by tooltip text
                export_btn = page.locator('[title="Xuất hóa đơn"]').first

            async with page.expect_download(timeout=60_000) as dl_info:
                await export_btn.click()

            download = await dl_info.value
            suggested = download.suggested_filename or "invoice_list.xlsx"
            dest = save_dir / suggested

            await download.save_as(str(dest))
            emit(f"✓ Invoice list downloaded: {dest.name}")
            return dest

        except Exception as exc:
            logger.warning("Export invoice list attempt {} failed: {}", attempt, exc)
            emit(f"Export attempt {attempt} failed: {exc}")
            await asyncio.sleep(2)

    emit("❌ Failed to export invoice list.")
    return None