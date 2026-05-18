"""
XML parsing service cho hóa đơn điện tử Việt Nam (GDT / ETAX).

Đây là nguồn duy nhất (single source of truth) cho mọi logic parse XML.
Cả invoice_detail.py lẫn các module khác đều import từ đây — không tự parse lại.

Hỗ trợ:
  - File .xml thông thường
  - File .zip chứa .xml bên trong

Schema chính: TT78/2021 — root <HDon> > <DLHDon> > <NDHDon>
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from loguru import logger


# ─────────────────────────────────────── Low-level XML helpers

def _strip_ns(tag: str) -> str:
    """Bỏ namespace prefix: '{http://...}Tag' → 'Tag'."""
    return tag.split("}")[-1] if "}" in tag else tag


def _find_text(root: ET.Element, *tag_names: str) -> Optional[str]:
    """
    Trả về text của element đầu tiên khớp tên tag (không phân biệt namespace).
    Thử lần lượt từng tên trong tag_names cho đến khi tìm thấy.
    """
    for name in tag_names:
        for el in root.iter():
            if _strip_ns(el.tag) == name and el.text and el.text.strip():
                return el.text.strip()
    return None


def _find_text_in_parent(
    root: ET.Element, parent_tag: str, child_tag: str
) -> Optional[str]:
    """
    Tìm <child_tag> bên trong <parent_tag> đầu tiên gặp được.
    Hữu ích khi cùng tên tag (vd <Ten>, <MST>) xuất hiện ở nhiều block (NBan, NMua).
    """
    for el in root.iter():
        if _strip_ns(el.tag) == parent_tag:
            for child in el:
                if _strip_ns(child.tag) == child_tag and child.text:
                    return child.text.strip()
    return None


def _text_at_path(root: ET.Element, path: str) -> str:
    """
    root.find(path).text — trả về "" nếu không tìm thấy.
    Dùng cho xpath đơn giản không có namespace.
    """
    node = root.find(path)
    return (node.text or "").strip() if node is not None else ""


def _parse_amount(raw: Optional[str]) -> float:
    """
    Chuyển chuỗi số kiểu Việt Nam → float.
      '1.234.567'   → 1234567.0
      '1,234,567'   → 1234567.0
      '12345.67'    → 12345.67
    """
    if not raw:
        return 0.0
    s = str(raw).strip()
    # Phát hiện dấu thập phân: nếu có cả '.' và ',' thì phần cuối là thập phân
    if "." in s and "," in s:
        # '1.234.567,89' → Châu Âu / VN → loại '.' rồi đổi ',' thành '.'
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            # '1,234,567.89' → US
            s = s.replace(",", "")
    elif "," in s:
        # Không có '.': '1234567,89' → thập phân kiểu VN/EU
        s = s.replace(",", ".")
    else:
        # Chỉ có '.' — nếu xuất hiện > 1 lần thì đó là dấu ngàn (VN)
        if s.count(".") > 1:
            s = s.replace(".", "")
    # Bỏ ký hiệu tiền tệ
    s = s.replace("VNĐ", "").replace("VND", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_date(raw: Optional[str]) -> Optional[date]:
    """Parse ngày từ chuỗi, thử nhiều định dạng phổ biến."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[: len(fmt)], fmt).date()
        except ValueError:
            continue
    # Fallback: chỉ lấy 10 ký tự đầu
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    logger.warning("Không parse được ngày: '{}'", raw)
    return None


# ─────────────────────────────────────── ZIP / XML file opener

def open_xml_from_path(path: Path) -> Optional[Tuple[ET.Element, str]]:
    """
    Mở và parse XML từ:
      - File .xml thông thường
      - File .zip → tìm file .xml không phải HTML bên trong

    Returns (root_element, source_filename) hoặc None nếu thất bại.
    """
    if not path.exists():
        logger.warning("File not found: {}", path)
        return None

    suffix = path.suffix.lower()
    try:
        if suffix == ".zip":
            with zipfile.ZipFile(path, "r") as zf:
                xml_names = [
                    n for n in zf.namelist()
                    if n.lower().endswith(".xml") and not n.startswith("__")
                ]
                if not xml_names:
                    logger.error("Không tìm thấy .xml trong ZIP: {}", path.name)
                    return None
                # Ưu tiên file không chứa "html" trong tên
                chosen = next(
                    (n for n in xml_names if "html" not in n.lower()), xml_names[0]
                )
                xml_bytes = zf.read(chosen)
                root = ET.fromstring(xml_bytes.decode("utf-8"))
                return root, chosen
        else:
            return ET.parse(str(path)).getroot(), path.name

    except zipfile.BadZipFile:
        logger.error("File ZIP không hợp lệ: {}", path.name)
    except ET.ParseError as exc:
        logger.error("XML parse error {}: {}", path.name, exc)
    except Exception as exc:
        logger.exception("Lỗi khi mở {}: {}", path.name, exc)
    return None


def parse_xml_bytes(xml_bytes: bytes) -> Optional[ET.Element]:
    """Parse XML từ bytes, trả về root element hoặc None."""
    try:
        return ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        logger.warning("XML parse error: {}", exc)
        return None


# ─────────────────────────────────────── TTKhac block helper

def _ttkhac_dict(elem: ET.Element) -> Dict[str, str]:
    """
    Chuyển block:
      <TTKhac>
        <TTin><TTruong>KEY</TTruong><DLieu>VAL</DLieu></TTin>
        ...
      </TTKhac>
    thành dict {KEY: VAL}.
    Chỉ đọc TTin con trực tiếp của TTKhac (không đệ quy lên cha).
    """
    result: Dict[str, str] = {}
    ttkhac = elem.find("TTKhac")
    if ttkhac is None:
        return result
    for ttin in ttkhac.findall("TTin"):
        key = (ttin.findtext("TTruong") or "").strip()
        val = (ttin.findtext("DLieu")   or "").strip()
        if key:
            result[key] = val
    return result


# ─────────────────────────────────────── Public API

class XmlService:
    """
    Service parse hóa đơn XML GDT.
    Tất cả các module nên dùng class này thay vì tự parse.
    """

    # ── Invoice metadata ─────────────────────────────────────────────────────

    @staticmethod
    def parse_metadata(xml_bytes: bytes) -> Dict[str, Any]:
        """
        Parse toàn bộ metadata từ <HDon>.
        Trả về dict với tất cả field đã được chuẩn hóa kiểu dữ liệu.

        Các field trả về:
          invoice_no, invoice_symbol, invoice_form, invoice_type,
          issue_date (date | None), currency, payment_method,
          seller_name, seller_tax_code, seller_address,
          seller_phone, seller_email, seller_bank, seller_bank_name,
          buyer_name, buyer_tax_code, buyer_address,
          amount (float), vat_amount (float), total_amount (float),
          total_in_words, tax_authority_code, qr_data
        """
        if not xml_bytes:
            return {}

        root = parse_xml_bytes(xml_bytes)
        if root is None:
            return {}

        def t(path: str) -> str:
            return _text_at_path(root, path)

        # Fallback tìm theo tên tag khi path không khớp
        def ft(*names: str) -> str:
            return _find_text(root, *names) or ""

        return {
            # ── TTChung
            "invoice_no":     t(".//SHDon")     or ft("SHDon", "So", "invoiceNumber"),
            "invoice_symbol": t(".//KHHDon")    or ft("KHHDon", "KyHieu", "invoiceSeries"),
            "invoice_form":   t(".//KHMSHDon")  or ft("KHMSHDon"),
            "invoice_type":   t(".//THDon")     or ft("THDon"),
            "issue_date":     _parse_date(
                                  t(".//NLap") or ft("NLap", "NgayLap", "invoiceIssuedDate")
                              ),
            "currency":       t(".//DVTTe")     or ft("DVTTe"),
            "payment_method": t(".//HTTToan")   or ft("HTTToan"),
            # ── Người bán
            "seller_name":      t(".//NBan/Ten")      or _find_text_in_parent(root, "NBan", "Ten")      or ft("TNNBan", "TenNguoiBan", "sellerLegalName"),
            "seller_tax_code":  t(".//NBan/MST")      or _find_text_in_parent(root, "NBan", "MST")      or ft("MST", "MaSoThue", "sellerTaxCode"),
            "seller_address":   t(".//NBan/DChi")     or _find_text_in_parent(root, "NBan", "DChi")     or ft("DChi"),
            "seller_phone":     t(".//NBan/SDThoai")  or _find_text_in_parent(root, "NBan", "SDThoai")  or ft("SDThoai"),
            "seller_email":     t(".//NBan/DCTDTu")   or _find_text_in_parent(root, "NBan", "DCTDTu")   or ft("DCTDTu"),
            "seller_bank":      t(".//NBan/STKNHang") or _find_text_in_parent(root, "NBan", "STKNHang") or ft("STKNHang"),
            "seller_bank_name": t(".//NBan/TNHang")   or _find_text_in_parent(root, "NBan", "TNHang")   or ft("TNHang"),
            # ── Người mua
            "buyer_name":     t(".//NMua/Ten") or _find_text_in_parent(root, "NMua", "Ten") or ft("TNNMua", "TenNguoiMua", "buyerLegalName"),
            "buyer_tax_code": t(".//NMua/MST") or _find_text_in_parent(root, "NMua", "MST") or ft("MSTNMua", "MaSoThueNguoiMua", "buyerTaxCode"),
            "buyer_address":  t(".//NMua/DChi") or _find_text_in_parent(root, "NMua", "DChi") or ft("DChiNMua"),
            # ── Thanh toán (raw string → float)
            "amount":       _parse_amount(t(".//TToan/TgTCThue")  or ft("TgTCThue", "TongTienChuaThue", "totalAmountWithoutVat")),
            "vat_amount":   _parse_amount(t(".//TToan/TgTThue")   or ft("TgTThue",  "TongTienThue",    "totalVatAmount")),
            "total_amount": _parse_amount(t(".//TToan/TgTTTBSo")  or ft("TgTTTBSo", "TongTienThanhToan","totalAmount")),
            "total_in_words": t(".//TToan/TgTTTBChu") or ft("TgTTTBChu"),
            # ── Mã cơ quan thuế / QR
            "tax_authority_code": t(".//MCCQT")     or ft("MCCQT"),
            "qr_data":            t(".//DLQRCode")  or ft("DLQRCode"),
        }

    # ── Line items ────────────────────────────────────────────────────────────

    @staticmethod
    def parse_line_items(xml_bytes: bytes) -> List[Dict[str, Any]]:
        """
        Parse danh sách hàng hóa/dịch vụ từ <HDon>.

        Schema TT78/2021:
          HDon > DLHDon > NDHDon > DSHHDVu > HHDVu

        Tiền thuế VAT từng dòng nằm trong TTKhac:
          HHDVu > TTKhac > TTin[TTruong=VATAmount] > DLieu
        """
        if not xml_bytes:
            return []

        root = parse_xml_bytes(xml_bytes)
        if root is None:
            return []

        # Tìm tất cả <HHDVu>
        hhdvu_list = root.findall(".//HHDVu")
        if not hhdvu_list:
            hhdvu_list = [e for e in root.iter() if _strip_ns(e.tag) == "HHDVu"]

        items: List[Dict[str, Any]] = []
        for idx, elem in enumerate(hhdvu_list):
            extra = _ttkhac_dict(elem)

            def ev(tag_name: str) -> str:
                return (elem.findtext(tag_name) or "").strip()

            item: Dict[str, Any] = {
                "stt":        ev("STT") or str(idx + 1),
                "ten_hang":   ev("THHDVu"),
                "don_vi":     ev("DVTinh"),
                "so_luong":   ev("SLuong"),
                "don_gia":    _parse_amount(ev("DGia")),
                "tl_ck":      _parse_amount(ev("TLCKhau")),   # tỉ lệ chiết khấu %
                "st_ck":      _parse_amount(ev("STCKhau")),   # số tiền chiết khấu
                "thanh_tien": _parse_amount(ev("ThTien")),    # thành tiền trước thuế
                "thue_suat":  ev("TSuat"),
                "tien_thue":  _parse_amount(extra.get("VATAmount", "0")),  # tiền thuế VAT
                "tong_tien":  _parse_amount(extra.get("Amount",    "0")),  # thành tiền gồm thuế
            }
            items.append(item)

        logger.debug("Parsed {} line items", len(items))
        return items

    # ── Convenience: parse cả từ file path ───────────────────────────────────

    @staticmethod
    def parse_from_path(xml_path: Path) -> Optional[Dict[str, Any]]:
        """
        Parse metadata từ file .xml hoặc .zip.
        Trả về dict hoặc None nếu không đọc được.
        """
        result = open_xml_from_path(xml_path)
        if result is None:
            return None
        root, source_name = result
        try:
            # Serialise root lại thành bytes để dùng parse_metadata
            xml_bytes = ET.tostring(root, encoding="unicode").encode("utf-8")
            meta = XmlService.parse_metadata(xml_bytes)
            logger.debug(
                "Parsed XML ({}): #{} total={}",
                source_name, meta.get("invoice_no"), meta.get("total_amount"),
            )
            return meta
        except Exception as exc:
            logger.exception("Lỗi parse_from_path {}: {}", xml_path.name, exc)
            return None

    @staticmethod
    def line_items_from_path(xml_path: Path) -> List[Dict[str, Any]]:
        """Parse line items từ file .xml hoặc .zip."""
        result = open_xml_from_path(xml_path)
        if result is None:
            return []
        root, _ = result
        xml_bytes = ET.tostring(root, encoding="unicode").encode("utf-8")
        return XmlService.parse_line_items(xml_bytes)