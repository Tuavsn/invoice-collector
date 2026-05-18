"""
Per-invoice detail automation.

Pipeline cho mỗi dòng hóa đơn:
  1. Đọc metadata từ bảng (extract_row_data)
  2. Kiểm tra DB — nếu đã crawl rồi thì bỏ qua (skip)
  3. Click chọn dòng
  4. Download ZIP chứa .xml + .html (download_zip)
  5. Giải nén, phân loại file (extract_invoice_zip)
  6. Parse XML qua XmlService (single source of truth)
  7. Trả về result dict đầy đủ để _persist_invoice() lưu vào DB

Không còn tự parse XML tại đây — tất cả delegate sang XmlService.
"""
from __future__ import annotations

import asyncio
import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from xml.etree import ElementTree as ET

from loguru import logger
from playwright.async_api import Page

from app.db.repository import InvoiceRepository
from app.services.xml_service import XmlService
from app.utils.paths import ensure_invoice_dir


# ─────────────────────────────────────── ZIP extraction

class ZipExtractResult:
    __slots__ = ("data_xml_path", "view_html_path", "all_paths")

    def __init__(self) -> None:
        self.data_xml_path:  Optional[Path] = None
        self.view_html_path: Optional[Path] = None
        self.all_paths: List[Path] = []


def extract_invoice_zip(zip_path: Path, dest_dir: Path) -> ZipExtractResult:
    """
    Giải nén ZIP từ GDT và phân loại:
      - File .xml có root element <HDon>  → data_xml_path
      - File .html / .htm                 → view_html_path
    Flatten path: bỏ thư mục con trong ZIP, chỉ giữ tên file.
    """
    result = ZipExtractResult()
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not zip_path.exists():
        logger.warning("ZIP not found: {}", zip_path)
        return result

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                if member.filename.endswith("/"):
                    continue

                content  = zf.read(member.filename)
                out_path = dest_dir / Path(member.filename).name
                out_path.write_bytes(content)
                result.all_paths.append(out_path)

                ext = Path(member.filename).suffix.lower()

                if ext == ".xml":
                    try:
                        root      = ET.fromstring(content)
                        local_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
                        if local_tag == "HDon":
                            result.data_xml_path = out_path
                            logger.debug("Data XML identified: {}", out_path.name)
                        else:
                            logger.debug(
                                "XML skipped (root=<{}>): {}", local_tag, out_path.name
                            )
                    except ET.ParseError:
                        logger.warning("XML parse error, skipped: {}", out_path.name)

                elif ext in (".html", ".htm"):
                    result.view_html_path = out_path
                    logger.debug("View HTML identified: {}", out_path.name)

    except zipfile.BadZipFile:
        logger.error("File ZIP không hợp lệ: {}", zip_path.name)
    except Exception as exc:
        logger.exception("Lỗi khi giải nén {}: {}", zip_path.name, exc)

    logger.info(
        "ZIP extracted → data_xml={}, view_html={}",
        result.data_xml_path.name  if result.data_xml_path  else None,
        result.view_html_path.name if result.view_html_path else None,
    )
    return result


# ─────────────────────────────────────── Row data extractor

async def extract_row_data(row_locator) -> Dict[str, Any]:
    """Đọc text từng cell của một dòng trong bảng hóa đơn."""
    cells = row_locator.locator("td")
    count = await cells.count()
    texts: List[str] = []
    for i in range(count):
        try:
            texts.append((await cells.nth(i).inner_text()).strip())
        except Exception:
            texts.append("")

    def _get(idx: int, default: str = "") -> str:
        return texts[idx] if idx < len(texts) else default

    return {
        "row_index":      _get(0),
        "tax_code":       _get(1),
        "invoice_no":     _get(2),
        "invoice_symbol": _get(3),
        "invoice_form":   _get(4),
        "issue_date_str": _get(5),
        "buyer_info":     _get(6),
        "amount_str":     _get(7,  "0"),
        "vat_rate_str":   _get(8),
        "vat_str":        _get(9,  "0"),
        "exempt_str":     _get(10, "0"),
        "total_str":      _get(11, "0"),
        "currency":       _get(12),
        "invoice_type":   _get(13),
        "status":         _get(14),
        "raw_texts":      texts,
    }


# ─────────────────────────────────────── Amount / date helpers

def parse_amount(raw: str) -> float:
    from app.services.xml_service import _parse_amount
    return _parse_amount(raw)


def parse_date_str(raw: str) -> Optional[date]:
    from app.utils.dates import parse_date
    return parse_date(raw)


# ─────────────────────────────────────── Already-crawled check

def _is_already_crawled(invoice_no: str) -> bool:
    """
    Trả về True nếu hóa đơn đã tồn tại trong DB với status không phải 'failed'/'error'.

    Yêu cầu InvoiceRepository có method:
        get_by_invoice_no(invoice_no: str) -> Optional[Invoice]
    Nếu chưa có, thêm vào repository.py:
        @staticmethod
        def get_by_invoice_no(invoice_no: str):
            return Invoice.query.filter_by(invoice_no=invoice_no).first()
    """
    if not invoice_no or invoice_no.startswith("unknown_"):
        return False
    try:
        existing = InvoiceRepository.get_by_invoice_no(invoice_no)
        if existing and existing.status not in ("failed", "error"):
            return True
    except Exception as exc:
        logger.warning("DB check for invoice #{} failed: {}", invoice_no, exc)
    return False


# ─────────────────────────────────────── Download button helper

async def _find_button_by_tooltip(page: Page, tooltip_text: str):
    indices = await page.evaluate("""
        () => {
            const buttons = [...document.querySelectorAll('button')];
            return buttons
                .map((b, i) => ({ i, has: !!b.querySelector('g#icon_ketxuat') }))
                .filter(x => x.has)
                .map(x => x.i);
        }
    """)
    logger.debug("Buttons with icon_ketxuat: {}", indices)

    for idx in indices:
        btn = page.locator("button").nth(idx)
        await btn.dispatch_event("mouseenter")
        await btn.dispatch_event("mouseover")
        await asyncio.sleep(0.5)
        try:
            await page.locator(
                f'.ant-tooltip-inner:has-text("{tooltip_text}")'
            ).wait_for(state="visible", timeout=3_000)
            return btn
        except Exception:
            continue
    return None


async def download_zip(page: Page, invoice_dir: Path) -> Optional[Path]:
    """Click nút 'Xuất xml' và lưu file ZIP trả về."""
    dest = invoice_dir / "invoice.zip"
    try:
        btn = await _find_button_by_tooltip(page, "Xuất xml")
        if btn is None:
            logger.warning("XML export button not found")
            return None
        async with page.expect_download(timeout=60_000) as dl_info:
            await btn.click()
        download = await dl_info.value
        await download.save_as(str(dest))
        logger.info("ZIP downloaded → {}", dest.name)
        return dest
    except Exception as exc:
        logger.warning("download_zip failed: {}", exc)
        return None


# ─────────────────────────────────────── Main per-row processor

# Sentinel để phân biệt "bỏ qua có chủ đích" với "lỗi thật" (None)
class _Skipped:
    pass

SKIPPED = _Skipped()


async def process_invoice_row(
    page: Page,
    row_locator,
    row_index: int,
    emit_fn: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Pipeline xử lý một dòng hóa đơn.
    Trả về:
      - dict  → thành công, cần persist
      - SKIPPED → hóa đơn đã có trong DB, bỏ qua
      - None  → lỗi nghiêm trọng
    """

    def emit(msg: str) -> None:
        logger.info(msg)
        if emit_fn:
            emit_fn(msg)

    try:
        # ── 1. Đọc dữ liệu bảng
        row_data       = await extract_row_data(row_locator)
        invoice_no     = row_data.get("invoice_no") or f"unknown_{row_index}"
        issue_date_str = row_data.get("issue_date_str", "")
        issue_date     = parse_date_str(issue_date_str) or date.today()

        # ── 2. Skip nếu đã crawl
        if _is_already_crawled(invoice_no):
            emit(f"  ⏭ Row {row_index + 1}: Invoice #{invoice_no} already in DB — skipped.")
            return SKIPPED

        emit(f"Processing row {row_index + 1}: Invoice #{invoice_no} ({issue_date_str})")

        inv_dir = ensure_invoice_dir(invoice_no, issue_date)

        # ── 3. Click chọn dòng
        await row_locator.click()
        await asyncio.sleep(0.3)

        # ── 4. Download ZIP
        zip_path = await download_zip(page, inv_dir)

        # ── 5. Giải nén
        extract = ZipExtractResult()
        if zip_path and zip_path.exists():
            extract = extract_invoice_zip(zip_path, inv_dir / "extracted")

        # ── 6. Parse XML
        xml_meta:   Dict[str, Any]       = {}
        line_items: List[Dict[str, Any]] = []

        if extract.data_xml_path and extract.data_xml_path.exists():
            raw_bytes  = extract.data_xml_path.read_bytes()
            xml_meta   = XmlService.parse_metadata(raw_bytes)
            line_items = XmlService.parse_line_items(raw_bytes)
            logger.info(
                "XML parsed: {} line items | seller={} | total={}",
                len(line_items),
                xml_meta.get("seller_name", "—"),
                xml_meta.get("total_amount", 0),
            )
        else:
            logger.warning(
                "Row {}: không có data XML — chỉ dùng dữ liệu từ bảng.", row_index + 1
            )

        # ── 7. Build result dict
        def xml_or(key: str, fallback: Any = "") -> Any:
            return xml_meta.get(key) or fallback

        result: Dict[str, Any] = {
            "invoice_no":     xml_or("invoice_no",     invoice_no),
            "invoice_symbol": xml_or("invoice_symbol", row_data.get("invoice_symbol", "")),
            "invoice_form":   xml_or("invoice_form",   row_data.get("invoice_form",   "")),
            "invoice_type":   xml_or("invoice_type",   row_data.get("invoice_type",   "")),
            "issue_date":     xml_or("issue_date",     issue_date_str),
            "status":         row_data.get("status", "downloaded"),
            "currency":       xml_or("currency",       row_data.get("currency",       "")),
            "payment_method": xml_or("payment_method", ""),
            "seller_name":      xml_or("seller_name"),
            "seller_tax_code":  xml_or("seller_tax_code"),
            "seller_address":   xml_or("seller_address"),
            "seller_phone":     xml_or("seller_phone"),
            "seller_email":     xml_or("seller_email"),
            "seller_bank":      xml_or("seller_bank"),
            "seller_bank_name": xml_or("seller_bank_name"),
            "buyer_name":     xml_or("buyer_name"),
            "buyer_tax_code": xml_or("buyer_tax_code"),
            "buyer_address":  xml_or("buyer_address"),
            "amount":       xml_meta.get("amount")       if "amount"       in xml_meta else parse_amount(row_data.get("amount_str", "0")),
            "vat_rate":     xml_or("vat_rate",     row_data.get("vat_rate_str", "")),
            "vat_amount":   xml_meta.get("vat_amount")   if "vat_amount"   in xml_meta else parse_amount(row_data.get("vat_str",    "0")),
            "total_amount": xml_meta.get("total_amount") if "total_amount" in xml_meta else parse_amount(row_data.get("total_str",  "0")),
            "total_in_words":     xml_or("total_in_words"),
            "tax_authority_code": xml_or("tax_authority_code"),
            "qr_data":            xml_or("qr_data"),
            "line_items":  line_items,
            "zip_path":       str(zip_path)               if zip_path                  else None,
            "xml_data_path":  str(extract.data_xml_path)  if extract.data_xml_path    else None,
            "view_html_path": str(extract.view_html_path) if extract.view_html_path   else None,
            "pdf_path":       None,
            "invoice_dir":    str(inv_dir),
            "has_zip":  zip_path is not None and zip_path.exists(),
            "has_xml":  extract.data_xml_path is not None,
            "has_html": extract.view_html_path is not None,
            "has_pdf":  False,
        }

        # ── 8. Ghi metadata.json
        meta_path = inv_dir / "metadata.json"
        meta_path.write_text(
            json.dumps(result, default=str, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result["metadata_path"] = str(meta_path)

        emit(
            f"  ✓ #{result['invoice_no']} — "
            f"ZIP={'✓' if result['has_zip'] else '✗'} "
            f"XML={'✓' if result['has_xml'] else '✗'} "
            f"HTML={'✓' if result['has_html'] else '✗'} "
            f"items={len(line_items)}"
        )
        await asyncio.sleep(0.3)
        return result

    except Exception as exc:
        logger.exception("process_invoice_row({}) failed: {}", row_index, exc)
        emit(f"  ✗ Row {row_index + 1} failed: {exc}")
        return None


# ─────────────────────────────────────── Backward-compat helpers

def parse_invoice_metadata(xml_content: bytes) -> Dict[str, Any]:
    return XmlService.parse_metadata(xml_content)


def parse_line_items(xml_content: bytes) -> List[Dict[str, Any]]:
    return XmlService.parse_line_items(xml_content)