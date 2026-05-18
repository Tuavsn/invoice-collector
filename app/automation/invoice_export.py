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


async def _find_button_by_tooltip(page: Page, tooltip_text: str):
    """Find button containing icon_ketxuat SVG, match by Ant tooltip text."""
    # Dùng JS để tìm các button chứa g#icon_ketxuat vì SVG không query được bằng xpath thường
    button_count = await page.evaluate("""
        () => document.querySelectorAll('button').length
    """)

    indices = await page.evaluate("""
        () => {
            const buttons = [...document.querySelectorAll('button')];
            return buttons
                .map((b, i) => ({ i, has: !!b.querySelector('g#icon_ketxuat') }))
                .filter(x => x.has)
                .map(x => x.i);
        }
    """)
    logger.debug("Buttons with icon_ketxuat at indices: {}", indices)

    for idx in indices:
        btn = page.locator('button').nth(idx)

        await btn.dispatch_event("mouseenter")
        await btn.dispatch_event("mouseover")
        await asyncio.sleep(0.5)

        try:
            await page.locator(
                f'.ant-tooltip-inner:has-text("{tooltip_text}")'
            ).wait_for(state="visible", timeout=3_000)
            logger.debug("Button at index {} matched tooltip: '{}'", idx, tooltip_text)
            return btn
        except Exception:
            logger.debug("Button at index {} did not match: '{}'", idx, tooltip_text)
            continue

    return None


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

            target_btn = await _find_button_by_tooltip(page, "Xuất hóa đơn")
            if target_btn is None:
                raise RuntimeError("Cannot find button with tooltip 'Xuất hóa đơn'")

            async with page.expect_download(timeout=60_000) as dl_info:
                await target_btn.click()

            download = await dl_info.value
            suggested = download.suggested_filename or "invoice_list.xlsx"
            dest = save_dir / suggested

            await download.save_as(str(dest))
            emit(f"✓ Invoice list downloaded: {dest.name}")
            return dest

        except Exception as exc:
            logger.warning("Export invoice list attempt {} failed: {}", attempt, exc)
            emit(f"Export attempt {attempt} failed: {exc}")
            if attempt < 3:
                await asyncio.sleep(2)

    emit("❌ Failed to export invoice list after 3 attempts.")
    return None

async def _render_pdf_async(html_path: Path, pdf_path: Path) -> bool:
    """
    Use Playwright to render the invoice HTML/XML to PDF.
    The view HTML inside the ZIP typically references bundled JS/CSS/images
    at relative paths — so we load it as file:// URL to resolve them correctly.
    """
    try:
        from playwright.async_api import async_playwright
 
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
 
            # Load as file URL so relative assets (js, images, css) resolve correctly
            file_url = html_path.resolve().as_uri()
            await page.goto(file_url, wait_until="networkidle", timeout=30_000)
 
            # Đợi nội dung render xong (một số hóa đơn dùng JS để render)
            await asyncio.sleep(1.5)
 
            await page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "15mm", "bottom": "15mm", "left": "10mm", "right": "10mm"},
            )
            await browser.close()
 
        logger.info("PDF generated: {}", pdf_path.name)
        return True
 
    except Exception as exc:
        logger.error("PDF generation failed: {}", exc)
        return False
 
 
def generate_invoice_pdf(invoice_dir: str | Path) -> Optional[Path]:
    """
    Synchronous wrapper.
    Looks for view HTML (from ZIP extraction) in invoice_dir/extracted/,
    falls back to data XML if no HTML found.
    Returns path to generated PDF or None on failure.
    """
    invoice_dir = Path(invoice_dir)
    extracted_dir = invoice_dir / "extracted"
    pdf_path = invoice_dir / "invoice.pdf"
 
    # Tìm file HTML viewer (ưu tiên index.html)
    html_file: Optional[Path] = None
    if extracted_dir.exists():
        for candidate in ["index.html", "index.htm"]:
            p = extracted_dir / candidate
            if p.exists():
                html_file = p
                break
        # Fallback: bất kỳ .html nào
        if html_file is None:
            htmls = list(extracted_dir.glob("*.html")) + list(extracted_dir.glob("*.htm"))
            if htmls:
                html_file = htmls[0]
 
    if html_file is None:
        logger.warning("No view HTML found in {}; cannot generate PDF", extracted_dir)
        return None
 
    logger.info("Rendering PDF from: {}", html_file.name)
    success = asyncio.run(_render_pdf_async(html_file, pdf_path))
    return pdf_path if success else None