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
        "row_index":              _get(0),
        "tax_code":               _get(1),
        "invoice_form":           _get(2),
        "invoice_symbol":         _get(3),
        "invoice_no":             _get(4),
        "issue_date_str":         _get(5),
        "invoice_info":           _get(6),
        "amount_before_tax":      _get(7, "0"),
        "tax_amount":             _get(8, "0"),
        "discount_amount":        _get(9, "0"),
        "fee_amount":             _get(10, "0"),
        "total_payment":          _get(11, "0"),
        "currency":               _get(12),
        "invoice_status":         _get(13),
        "processing_result":      _get(14),
        "verification_result":    _get(15),
        "related_invoice":        _get(16),
        "related_info":           _get(17),
        "raw_texts":              texts,
    }


# ─────────────────────────────────────── Amount / date helpers

def parse_amount(raw: str) -> float:
    from app.services.xml_service import _parse_amount
    return _parse_amount(raw)


def parse_date_str(raw: str) -> Optional[date]:
    from app.utils.dates import parse_date
    return parse_date(raw)


# ─────────────────────────────────────── Already-crawled check

def _is_already_crawled(
    invoice_no: str,
    invoice_symbol: Optional[str] = None,
    invoice_form: Optional[str] = None,
    issue_date_str: str = "",
    total_payment: str = "0",
) -> bool:
    """
    Trả về True nếu hóa đơn đã tồn tại trong DB và không ở trạng thái lỗi.

    Check theo composite key (invoice_no + invoice_symbol + invoice_form) thay
    vì chỉ invoice_no — tránh bỏ qua nhầm hóa đơn điều chỉnh/thay thế có cùng
    số nhưng khác ký hiệu / mẫu.

    issue_date_str và total_payment được log ra để dễ debug, không dùng để query
    (DB đã có các trường này từ XML nên không nên so sánh chuỗi thô từ bảng).
    """
    if not invoice_no or invoice_no.startswith("unknown_"):
        return False
    try:
        existing = InvoiceRepository.exists_by_composite_key(
            invoice_no=invoice_no,
            invoice_symbol=invoice_symbol or None,
            invoice_form=invoice_form or None,
        )
        if existing and existing.status not in ("failed", "error"):
            logger.debug(
                "Skip #{} symbol={} form={} date={} total={} — already in DB (id={}, status={})",
                invoice_no, invoice_symbol, invoice_form,
                issue_date_str, total_payment,
                existing.id, existing.status,
            )
            return True
    except Exception as exc:
        logger.warning("DB check for invoice #{} failed: {}", invoice_no, exc)
    return False


# ─────────────────────────────────────── Download button helper

async def _find_button_by_tooltip(page: Page, tooltip_text: str):
    """
    Locate the export button by its SVG icon ID, then verify via tooltip.
    Uses dispatch_event to trigger hover without sleeping — waits for tooltip
    to appear with a tight timeout before moving to next candidate.
    """
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
        try:
            await page.locator(
                f'.ant-tooltip-inner:has-text("{tooltip_text}")'
            ).wait_for(state="visible", timeout=2_000)
            return btn
        except Exception:
            # Tooltip didn't appear — dismiss and try next button
            await btn.dispatch_event("mouseleave")
            continue
    return None


async def download_zip(page: Page, invoice_dir: Path) -> Optional[Path]:
    """
    Click 'Xuất xml' button and save the downloaded ZIP.

    Strategy:
      1. Fast path — positional selector from recording (div:nth-child(8) > .ant-btn)
         Works as long as button order doesn't change.
      2. Fallback — tooltip-based detection via _find_button_by_tooltip.
    """
    dest = invoice_dir / "invoice.zip"

    async def _do_download(btn) -> Optional[Path]:
        try:
            async with page.expect_download(timeout=60_000) as dl_info:
                await btn.click()
            download = await dl_info.value
            await download.save_as(str(dest))
            logger.info("ZIP downloaded → {}", dest.name)
            return dest
        except Exception as exc:
            logger.warning("Download click failed: {}", exc)
            return None

    # ── Fast path
    fast_btn = page.locator("div:nth-child(8) > .ant-btn").first
    if await fast_btn.count() > 0:
        result = await _do_download(fast_btn)
        if result:
            return result
        logger.debug("Fast-path download failed, trying tooltip fallback…")

    # ── Fallback: tooltip detection
    btn = await _find_button_by_tooltip(page, "Xuất xml")
    if btn is None:
        logger.warning("XML export button not found via tooltip either")
        return None
    return await _do_download(btn)


# ─────────────────────────────────────── Main per-row processor

class _Skipped:
    pass

SKIPPED = _Skipped()


async def process_invoice_row(
    page: Page,
    row_locator,
    row_index: int,
    emit_fn: Optional[Callable[[str], None]] = None,
    invoice_category: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Pipeline xử lý một dòng hóa đơn.
    invoice_category: giá trị từ SubTabConfig (sale_einvoice / sale_pos /
                      purchase_einvoice / purchase_pos) — phân loại crawl,
                      khác hoàn toàn với invoice_type (THDon từ XML).
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
        if _is_already_crawled(
            invoice_no=invoice_no,
            invoice_symbol=row_data.get("invoice_symbol") or None,
            invoice_form=row_data.get("invoice_form") or None,
            issue_date_str=issue_date_str,
            total_payment=row_data.get("total_payment", "0"),
        ):
            emit(f"  ⏭ Row {row_index + 1}: Invoice #{invoice_no} already in DB — skipped.")
            return SKIPPED

        emit(f"Processing row {row_index + 1}: Invoice #{invoice_no} ({issue_date_str})")

        inv_dir = ensure_invoice_dir(invoice_no, issue_date)

        # ── 3. Click chọn dòng, wait for action panel / buttons to appear
        await row_locator.click()
        try:
            await page.wait_for_selector(
                ".ant-btn, button",
                state="visible",
                timeout=5_000,
            )
        except Exception:
            pass  # Buttons may already be present

        # ── 4. Download ZIP
        zip_path = await download_zip(page, inv_dir)

        # ── 5. Giải nén
        extract = ZipExtractResult()
        if zip_path and zip_path.exists():
            extract = extract_invoice_zip(zip_path, inv_dir / "extracted")

        # ── 6. Parse XML
        xml_meta:      Dict[str, Any]       = {}
        line_items:    List[Dict[str, Any]] = []
        vat_breakdown: List[Dict[str, Any]] = []

        if extract.data_xml_path and extract.data_xml_path.exists():
            raw_bytes     = extract.data_xml_path.read_bytes()
            xml_meta      = XmlService.parse_metadata(raw_bytes)
            line_items    = XmlService.parse_line_items(raw_bytes)
            vat_breakdown = xml_meta.pop("vat_breakdown", [])
            logger.info(
                "XML parsed: {} line items | {} vat brackets | seller={} | total={}",
                len(line_items),
                len(vat_breakdown),
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
            # ── Hóa đơn
            "invoice_no":     xml_or("invoice_no",     invoice_no),
            "invoice_symbol": xml_or("invoice_symbol", row_data.get("invoice_symbol", "")),
            "invoice_form":   xml_or("invoice_form",   row_data.get("invoice_form",   "")),
            # invoice_type  = THDon từ XML (loại hóa đơn theo chuẩn GDT, ví dụ "1"=GTGT)
            "invoice_type":   xml_meta.get("invoice_type_gdt"),
            # invoice_category = phân loại crawl từ SubTabConfig — KHÔNG lấy từ XML
            "invoice_category": invoice_category,
            "issue_date":     xml_or("issue_date",     issue_date_str),
            # status luôn là "downloaded" tại bước crawl; các trạng thái khác
            # (failed, error, ...) được set bởi các bước xử lý sau
            "status":         "downloaded",
            "currency":       xml_or("currency",       row_data.get("currency", "")),
            "exchange_rate":  xml_meta.get("exchange_rate"),
            "payment_method": xml_meta.get("payment_method"),
            # ── XML meta
            "xml_version":       xml_meta.get("xml_version"),
            "software_tax_code": xml_meta.get("software_tax_code"),
            "is_adjustment":     xml_meta.get("is_adjustment"),
            "portal_link":       xml_meta.get("portal_link"),
            "fkey":              xml_meta.get("fkey"),
            # ── Ngày ký
            "seller_signing_time": xml_meta.get("seller_signing_time"),
            "tax_signing_time":    xml_meta.get("tax_signing_time"),
            # ── Người bán — tất cả chỉ từ XML, không fallback từ bảng
            "seller_name":      xml_meta.get("seller_name"),
            "seller_tax_code":  xml_or("seller_tax_code", row_data.get("tax_code", "")),
            "seller_address":   xml_meta.get("seller_address"),
            "seller_phone":     xml_meta.get("seller_phone"),
            "seller_email":     xml_meta.get("seller_email"),
            "seller_bank":      xml_meta.get("seller_bank"),
            "seller_bank_name": xml_meta.get("seller_bank_name"),
            "seller_fax":       xml_meta.get("seller_fax"),
            "seller_website":   xml_meta.get("seller_website"),
            # ── Người mua — tất cả chỉ từ XML
            "buyer_name":      xml_meta.get("buyer_name"),
            "buyer_tax_code":  xml_meta.get("buyer_tax_code"),
            "buyer_address":   xml_meta.get("buyer_address"),
            "buyer_phone":     xml_meta.get("buyer_phone"),
            "buyer_email":     xml_meta.get("buyer_email"),
            "buyer_bank":      xml_meta.get("buyer_bank"),
            "buyer_bank_name": xml_meta.get("buyer_bank_name"),
            # ── Số tiền (ưu tiên XML, fallback bảng)
            "amount":             xml_meta.get("amount")       if "amount"       in xml_meta else parse_amount(row_data.get("amount_before_tax", "0")),
            "vat_rate":           xml_meta.get("vat_rate"),
            "vat_amount":         xml_meta.get("vat_amount")   if "vat_amount"   in xml_meta else parse_amount(row_data.get("tax_amount",        "0")),
            "total_amount":       xml_meta.get("total_amount") if "total_amount" in xml_meta else parse_amount(row_data.get("total_payment",     "0")),
            "total_in_words":     xml_meta.get("total_in_words"),
            "discount_amount":    xml_meta.get("discount_amount"),
            "non_taxable_amount": xml_meta.get("non_taxable_amount"),
            "other_amount":       xml_meta.get("other_amount"),
            # ── Thuế chi tiết
            "vat_breakdown": vat_breakdown,
            # ── Mã CQT / QR
            "tax_authority_code": xml_meta.get("tax_authority_code"),
            "qr_data":            xml_meta.get("qr_data"),
            # ── Hàng hóa / dịch vụ
            "line_items":  line_items,
            # ── Đường dẫn file
            "zip_path":       str(zip_path)               if zip_path                else None,
            "xml_data_path":  str(extract.data_xml_path)  if extract.data_xml_path  else None,
            "view_html_path": str(extract.view_html_path) if extract.view_html_path else None,
            "pdf_path":       None,
            "invoice_dir":    str(inv_dir),
            # ── Flags
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
            f"items={len(line_items)} vat_brackets={len(vat_breakdown)}"
        )
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