from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import Page

async def _render_pdf_async(html_path: Path, pdf_path: Path) -> bool:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page    = await browser.new_page()
            await page.goto(html_path.resolve().as_uri(), wait_until="networkidle", timeout=3_000)
            await asyncio.sleep(1.5)
            await page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "15mm", "bottom": "15mm", "left": "10mm", "right": "10mm"},
            )
            await browser.close()
        return True
    except Exception as exc:
        logger.error("PDF generation failed: {}", exc)
        return False


def generate_invoice_pdf(invoice_dir: str | Path) -> Optional[Path]:
    invoice_dir   = Path(invoice_dir)
    extracted_dir = invoice_dir / "extracted"
    pdf_path      = invoice_dir / "invoice.pdf"

    html_file: Optional[Path] = None
    if extracted_dir.exists():
        for candidate in ["index.html", "index.htm"]:
            p = extracted_dir / candidate
            if p.exists():
                html_file = p
                break
        if html_file is None:
            htmls = list(extracted_dir.glob("*.html")) + list(extracted_dir.glob("*.htm"))
            if htmls:
                html_file = htmls[0]

    if html_file is None:
        logger.warning("No view HTML found in {}", extracted_dir)
        return None

    success = asyncio.run(_render_pdf_async(html_file, pdf_path))
    return pdf_path if success else None