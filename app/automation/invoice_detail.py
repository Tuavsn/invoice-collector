"""
Per-invoice detail automation.
Opens each row's detail modal and downloads XML, PDF, and ZIP attachments.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger
from playwright.async_api import Page

from app.config import Config
from app.utils.paths import ensure_invoice_dir


# ─────────────────────────────────────────────────────── Row extraction helpers

async def extract_row_data(row_locator) -> Dict[str, Any]:
    """Read visible cell text from a table row into a dict."""
    cells = row_locator.locator("td")
    count = await cells.count()
    texts = []
    for i in range(count):
        try:
            t = await cells.nth(i).inner_text()
            texts.append(t.strip())
        except Exception:
            texts.append("")

    # Positional mapping based on GDT table layout:
    # 0=STT, 1=Ký hiệu, 2=Số HĐ, 3=Ngày, 4=Người bán, 5=Người mua, 6=Tiền, 7=Thuế, 8=Tổng
    mapping: Dict[str, Any] = {
        "row_index": texts[0] if len(texts) > 0 else "",
        "invoice_symbol": texts[1] if len(texts) > 1 else "",
        "invoice_no": texts[2] if len(texts) > 2 else "",
        "issue_date_str": texts[3] if len(texts) > 3 else "",
        "seller_name": texts[4] if len(texts) > 4 else "",
        "buyer_name": texts[5] if len(texts) > 5 else "",
        "amount_str": texts[6] if len(texts) > 6 else "0",
        "vat_str": texts[7] if len(texts) > 7 else "0",
        "total_str": texts[8] if len(texts) > 8 else "0",
        "raw_texts": texts,
    }
    return mapping


def parse_amount(raw: str) -> float:
    """Convert Vietnamese number string '1.234.567' → 1234567.0."""
    cleaned = raw.replace(".", "").replace(",", ".").replace("VNĐ", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_date_str(raw: str) -> Optional[date]:
    """Parse dd/MM/yyyy date string."""
    from app.utils.dates import parse_date
    return parse_date(raw)


# ─────────────────────────────────────────────────────── Modal / detail handler

async def open_invoice_detail(page: Page, row_locator) -> bool:
    """Click a row to open the detail modal; wait for it to appear."""
    for attempt in range(1, 4):
        try:
            await row_locator.click()
            # Wait for modal / detail panel
            await page.wait_for_selector(
                ".ant-modal-content, .invoice-detail-panel, #invoice-detail",
                timeout=15_000,
            )
            await asyncio.sleep(0.5)
            return True
        except Exception as exc:
            logger.warning("Open detail attempt {}: {}", attempt, exc)
            await asyncio.sleep(1)
    return False


async def close_invoice_detail(page: Page) -> None:
    """Close the detail modal."""
    try:
        close_btn = page.locator(
            ".ant-modal-close, button:has-text('Đóng'), button:has-text('Close')"
        ).first
        if await close_btn.count() > 0:
            await close_btn.click()
            await asyncio.sleep(0.5)
    except Exception as exc:
        logger.debug("Modal close: {}", exc)
        # Fallback: press Escape
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)


# ─────────────────────────────────────────────────────── Download helpers

async def download_file_from_button(
    page: Page,
    button_selector: str,
    dest_path: Path,
    timeout: int = 60_000,
) -> Optional[Path]:
    """Click a download button and save the file to dest_path."""
    try:
        btn = page.locator(button_selector).first
        if await btn.count() == 0:
            return None

        async with page.expect_download(timeout=timeout) as dl_info:
            await btn.click()

        download = await dl_info.value
        await download.save_as(str(dest_path))
        logger.info("Downloaded → {}", dest_path.name)
        return dest_path

    except Exception as exc:
        logger.warning("Download failed ({}): {}", button_selector, exc)
        return None


async def download_xml(page: Page, invoice_dir: Path) -> Optional[Path]:
    """Download the XML file from the open detail modal."""
    dest = invoice_dir / "invoice.xml"

    # Try xpath export xml button first
    selectors = [
        'xpath=//*[@id="icon_ketxuat"]/ancestor::button',
        '[title="Xuất xml"]',
        'button:has-text("XML")',
        'button:has-text("Xuất XML")',
    ]

    for sel in selectors:
        result = await download_file_from_button(page, sel, dest)
        if result:
            return result

    return None


async def download_pdf(page: Page, invoice_dir: Path) -> Optional[Path]:
    """Download the PDF file from the open detail modal."""
    dest = invoice_dir / "invoice.pdf"

    selectors = [
        '[title="Xuất PDF"]',
        'button:has-text("PDF")',
        'button:has-text("Xuất PDF")',
        'a[href*=".pdf"]',
    ]

    for sel in selectors:
        result = await download_file_from_button(page, sel, dest)
        if result:
            return result

    return None


async def download_zip(page: Page, invoice_dir: Path) -> Optional[Path]:
    """Download ZIP attachment if present."""
    dest = invoice_dir / "attachments.zip"
    result = await download_file_from_button(
        page, 'button:has-text("ZIP"), [title="Tải đính kèm"]', dest
    )
    return result


# ─────────────────────────────────────────────────────── Main per-row processor

async def process_invoice_row(
    page: Page,
    row_locator,
    row_index: int,
    emit_fn: Optional[Callable] = None,
) -> Optional[Dict[str, Any]]:
    """
    Full pipeline for a single invoice row:
    1. Extract row metadata
    2. Open detail modal
    3. Download XML, PDF, optional ZIP
    4. Close modal
    5. Return collected metadata dict
    """

    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    try:
        row_data = await extract_row_data(row_locator)
        invoice_no = row_data.get("invoice_no", f"unknown_{row_index}")
        issue_date_str = row_data.get("issue_date_str", "")
        issue_date = parse_date_str(issue_date_str) or date.today()

        emit(f"Processing row {row_index + 1}: Invoice #{invoice_no} ({issue_date_str})")

        # Create storage directory
        inv_dir = ensure_invoice_dir(invoice_no, issue_date)

        # Open modal
        opened = await open_invoice_detail(page, row_locator)
        if not opened:
            emit(f"  ⚠ Could not open detail for #{invoice_no}")
            return None

        # Downloads
        xml_path = await download_xml(page, inv_dir)
        pdf_path = await download_pdf(page, inv_dir)
        await download_zip(page, inv_dir)

        # Build result dict
        result = {
            "invoice_no": invoice_no,
            "invoice_symbol": row_data.get("invoice_symbol", ""),
            "seller_name": row_data.get("seller_name", ""),
            "buyer_name": row_data.get("buyer_name", ""),
            "issue_date_str": issue_date_str,
            "issue_date": issue_date,
            "amount": parse_amount(row_data.get("amount_str", "0")),
            "vat_amount": parse_amount(row_data.get("vat_str", "0")),
            "total_amount": parse_amount(row_data.get("total_str", "0")),
            "xml_path": str(xml_path) if xml_path else None,
            "pdf_path": str(pdf_path) if pdf_path else None,
            "has_xml": xml_path is not None and xml_path.exists(),
            "has_pdf": pdf_path is not None and pdf_path.exists(),
            "invoice_dir": str(inv_dir),
        }

        # Save raw metadata
        raw_path = inv_dir / "metadata.json"
        raw_path.write_text(
            json.dumps(result, default=str, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result["metadata_path"] = str(raw_path)

        emit(
            f"  ✓ #{invoice_no} — XML={'✓' if result['has_xml'] else '✗'} "
            f"PDF={'✓' if result['has_pdf'] else '✗'}"
        )

        await close_invoice_detail(page)
        await asyncio.sleep(0.3)
        return result

    except Exception as exc:
        logger.exception("process_invoice_row({}) failed: {}", row_index, exc)
        emit(f"  ✗ Row {row_index + 1} failed: {exc}")
        try:
            await close_invoice_detail(page)
        except Exception:
            pass
        return None