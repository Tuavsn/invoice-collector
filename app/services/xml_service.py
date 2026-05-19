"""
XmlService — single source of truth cho việc parse XML hóa đơn điện tử Việt Nam.

Chuẩn Thông tư 78/2021, schema v2.1.0 (GDT).

Tất cả field được extract từ XML đều có tên khớp với models.py.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

from loguru import logger

# Namespace mặc định của xmldsig (dùng khi đọc SigningTime)
_NS = {"ds": "http://www.w3.org/2000/09/xmldsig#"}


# ─────────────────────────────────────────────────────────────── helpers

def _parse_amount(raw: Any) -> float:
    """Chuyển chuỗi số (có thể có dấu chấm/phẩy phân cách) sang float."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = re.sub(r"[^\d\-.]", "", str(raw).replace(",", "."))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _text(element: Optional[ET.Element], tag: str, ns: str = "") -> Optional[str]:
    """Lấy text của thẻ con đầu tiên khớp tag, trả None nếu không có."""
    if element is None:
        return None
    found = element.find(f"{ns}{tag}" if ns else tag)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _float(element: Optional[ET.Element], tag: str) -> Optional[float]:
    raw = _text(element, tag)
    if raw is None:
        return None
    return _parse_amount(raw)


def _parse_signing_time(raw: Optional[str]) -> Optional[datetime]:
    """Parse ISO datetime như '2026-04-29T13:22:55'."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def _get_ttkhac(element: Optional[ET.Element], key: str) -> Optional[str]:
    """
    Đọc giá trị từ block <TTKhac> theo tên trường <TTruong>.
    <TTKhac>
      <TTin>
        <TTruong>PortalLink</TTruong>
        <DLieu>http://...</DLieu>
      </TTin>
    </TTKhac>
    """
    if element is None:
        return None
    for ttin in element.findall("TTKhac/TTin"):
        truong = _text(ttin, "TTruong")
        if truong and truong.strip() == key:
            return _text(ttin, "DLieu")
    return None


def _strip_ns(tag: str) -> str:
    """Bỏ namespace prefix khỏi tag, ví dụ '{http://...}Signature' → 'Signature'."""
    return tag.split("}")[-1] if "}" in tag else tag


# ─────────────────────────────────────────────────────────────── main service

class XmlService:
    """
    Parse XML hóa đơn điện tử GDT (HDon schema).

    Public API:
      XmlService.parse_metadata(raw_bytes)  → Dict[str, Any]
      XmlService.parse_line_items(raw_bytes) → List[Dict[str, Any]]
    """

    @staticmethod
    def parse_metadata(raw_bytes: bytes) -> Dict[str, Any]:
        """
        Trả về dict chứa toàn bộ thông tin header, người bán, người mua,
        tổng tiền, mã CQT, QR, ngày ký — khớp với tên field trong models.py.
        """
        try:
            root = ET.fromstring(raw_bytes)
        except ET.ParseError as exc:
            logger.error("XML parse error: {}", exc)
            return {}

        # Tìm node DLHDon (bỏ qua namespace nếu có)
        dlhdon = root.find("DLHDon") or root
        ttchung = dlhdon.find("TTChung")
        ndhdon  = dlhdon.find("NDHDon")
        nban    = ndhdon.find("NBan")   if ndhdon else None
        nmua    = ndhdon.find("NMua")   if ndhdon else None
        ttoan   = ndhdon.find("TToan")  if ndhdon else None

        # ── TTChung ──────────────────────────────────────────────────────────
        xml_version       = _text(ttchung, "PBan")
        invoice_type      = _text(ttchung, "THDon")
        invoice_form      = _text(ttchung, "KHMSHDon")
        invoice_symbol    = _text(ttchung, "KHHDon")
        invoice_no        = _text(ttchung, "SHDon")
        issue_date_str    = _text(ttchung, "NLap")
        currency          = _text(ttchung, "DVTTe")
        exchange_rate_raw = _text(ttchung, "TGia")
        payment_method    = _text(ttchung, "HTTToan")
        software_tax_code = _text(ttchung, "MSTTCGP")
        is_adjustment_raw = _text(ttchung, "HDCTTChinh")

        exchange_rate  = _parse_amount(exchange_rate_raw) if exchange_rate_raw else None
        is_adjustment  = int(is_adjustment_raw) if is_adjustment_raw is not None and is_adjustment_raw.isdigit() else None

        # TTKhac cấp TTChung (PortalLink, Fkey)
        portal_link = _get_ttkhac(ttchung, "PortalLink")
        fkey        = _get_ttkhac(ttchung, "Fkey")

        # ── Người bán (NBan) ─────────────────────────────────────────────────
        seller_name      = _text(nban, "Ten")
        seller_tax_code  = _text(nban, "MST")
        seller_address   = _text(nban, "DChi")
        seller_phone     = _text(nban, "SDThoai")
        seller_email     = _text(nban, "DCTDTu")
        seller_bank      = _text(nban, "STKNHang")
        seller_bank_name = _text(nban, "TNHang")
        seller_fax       = _text(nban, "Fax")
        seller_website   = _text(nban, "Website")

        # ── Người mua (NMua) ─────────────────────────────────────────────────
        buyer_name      = _text(nmua, "Ten")
        buyer_tax_code  = _text(nmua, "MST")
        buyer_address   = _text(nmua, "DChi")
        buyer_phone     = _text(nmua, "SDThoai")
        buyer_email     = _text(nmua, "DCTDTu")
        buyer_bank      = _text(nmua, "STKNHang")
        buyer_bank_name = _text(nmua, "TNHang")

        # ── Tổng tiền (TToan) ────────────────────────────────────────────────
        amount             = _float(ttoan, "TgTCThue")   or 0.0
        vat_amount         = _float(ttoan, "TgTThue")    or 0.0
        total_amount       = _float(ttoan, "TgTTTBSo")   or 0.0
        total_in_words     = _text(ttoan, "TgTTTBChu")
        discount_amount    = _float(ttoan, "TTCKTMai")
        non_taxable_amount = _float(ttoan, "TGTKCThue")
        other_amount       = _float(ttoan, "TGTKhac")

        # ── Chi tiết thuế theo mức thuế suất (THTTLTSuat) ───────────────────
        vat_breakdown: List[Dict[str, Any]] = []
        if ttoan is not None:
            for ltsu in ttoan.findall("THTTLTSuat/LTSuat"):
            	vat_breakdown.append({
                    "vat_rate":  _text(ltsu, "TSuat"),
                    "amount":    _parse_amount(_text(ltsu, "ThTien")),
                    "vat_amount": _parse_amount(_text(ltsu, "TThue")),
                })

        # Thuế suất chính (lấy từ breakdown đầu tiên nếu có)
        vat_rate: Optional[str] = None
        if vat_breakdown:
            vat_rate = vat_breakdown[0]["vat_rate"]
        else:
            # Fallback: lấy từ dòng đầu tiên của line items
            first_item = (ndhdon.find("DSHHDVu/HHDVu") if ndhdon else None)
            if first_item is not None:
                vat_rate = _text(first_item, "TSuat")

        # ── Mã CQT / QR ──────────────────────────────────────────────────────
        mccqt_el           = root.find("MCCQT")
        tax_authority_code = mccqt_el.text.strip() if mccqt_el is not None and mccqt_el.text else None

        qr_el   = root.find("DLQRCode")
        qr_data = qr_el.text.strip() if qr_el is not None and qr_el.text else None

        # ── Ngày ký (DSCKS) ──────────────────────────────────────────────────
        seller_signing_time: Optional[datetime] = None
        tax_signing_time:    Optional[datetime] = None

        dscks = root.find("DSCKS")
        if dscks is not None:
            # Người bán
            nban_sig = dscks.find("NBan")
            if nban_sig is not None:
                # Tìm SigningTime bên trong Object/SignatureProperties/SignatureProperty
                # Có thể có namespace hoặc không
                for obj in nban_sig.iter():
                    if _strip_ns(obj.tag) == "SigningTime":
                        seller_signing_time = _parse_signing_time(obj.text)
                        break

            # Cơ quan thuế
            cqt_sig = dscks.find("CQT")
            if cqt_sig is not None:
                for obj in cqt_sig.iter():
                    if _strip_ns(obj.tag) == "SigningTime":
                        tax_signing_time = _parse_signing_time(obj.text)
                        break

        # ── Issue date ───────────────────────────────────────────────────────
        issue_date: Optional[str] = issue_date_str  # giữ string, crawler_engine sẽ parse sang datetime

        return {
            # Hóa đơn
            "invoice_no":     invoice_no,
            "invoice_symbol": invoice_symbol,
            "invoice_form":   invoice_form,
            "invoice_type_gdt": invoice_type,   # THDon — loại HĐ theo chuẩn GDT, ≠ phân loại crawl
            "issue_date":     issue_date,
            "currency":       currency,
            "exchange_rate":  exchange_rate,
            "payment_method": payment_method,
            # XML meta
            "xml_version":       xml_version,
            "software_tax_code": software_tax_code,
            "is_adjustment":     is_adjustment,
            "portal_link":       portal_link,
            "fkey":              fkey,
            # Ngày ký
            "seller_signing_time": seller_signing_time,
            "tax_signing_time":    tax_signing_time,
            # Người bán
            "seller_name":      seller_name,
            "seller_tax_code":  seller_tax_code,
            "seller_address":   seller_address,
            "seller_phone":     seller_phone,
            "seller_email":     seller_email,
            "seller_bank":      seller_bank,
            "seller_bank_name": seller_bank_name,
            "seller_fax":       seller_fax,
            "seller_website":   seller_website,
            # Người mua
            "buyer_name":      buyer_name,
            "buyer_tax_code":  buyer_tax_code,
            "buyer_address":   buyer_address,
            "buyer_phone":     buyer_phone,
            "buyer_email":     buyer_email,
            "buyer_bank":      buyer_bank,
            "buyer_bank_name": buyer_bank_name,
            # Tổng tiền
            "amount":             amount,
            "vat_rate":           vat_rate,
            "vat_amount":         vat_amount,
            "total_amount":       total_amount,
            "total_in_words":     total_in_words,
            "discount_amount":    discount_amount,
            "non_taxable_amount": non_taxable_amount,
            "other_amount":       other_amount,
            # Thuế chi tiết
            "vat_breakdown": vat_breakdown,
            # Mã CQT / QR
            "tax_authority_code": tax_authority_code,
            "qr_data":            qr_data,
        }

    @staticmethod
    def parse_line_items(raw_bytes: bytes) -> List[Dict[str, Any]]:
        """
        Parse danh sách hàng hóa/dịch vụ từ <DSHHDVu>.

        Mỗi dict trả về:
          stt, ma_hhdvu, ten_hhdvu, don_vi_tinh, so_luong, don_gia,
          ty_le_ck, so_tien_ck, thanh_tien, thue_suat, tinh_chat,
          tt_hh_trung (thông tin hàng hóa trung gian)
        """
        try:
            root = ET.fromstring(raw_bytes)
        except ET.ParseError as exc:
            logger.error("XML parse error (line_items): {}", exc)
            return []

        dlhdon = root.find("DLHDon") or root
        ndhdon = dlhdon.find("NDHDon")
        if ndhdon is None:
            return []

        items: List[Dict[str, Any]] = []
        for hhdvu in ndhdon.findall("DSHHDVu/HHDVu"):
            # Lấy thêm VATAmount & Amount từ TTKhac nếu có
            # (một số phần mềm hóa đơn ghi ThTien=0 và để ở TTKhac)
            ttkhac_vat    = _get_ttkhac(hhdvu, "VATAmount")
            ttkhac_amount = _get_ttkhac(hhdvu, "Amount")

            thanh_tien_raw = _text(hhdvu, "ThTien")
            thanh_tien     = _parse_amount(thanh_tien_raw)
            # Nếu ThTien=0 nhưng TTKhac/Amount có giá trị thì dùng TTKhac
            if thanh_tien == 0 and ttkhac_amount:
                thanh_tien = _parse_amount(ttkhac_amount)

            item: Dict[str, Any] = {
                "stt":          _text(hhdvu, "STT"),
                "tinh_chat":    _text(hhdvu, "TChat"),       # 1=hàng hóa, 2=dịch vụ, 3=chiết khấu
                "ma_hhdvu":     _text(hhdvu, "MHHDVu"),      # Mã hàng hóa/dịch vụ ← MỚI
                "ten_hhdvu":    _text(hhdvu, "THHDVu"),      # Tên hàng hóa/dịch vụ ← MỚI (THHDVu)
                "don_vi_tinh":  _text(hhdvu, "DVTinh"),      # Đơn vị tính ← MỚI
                "so_luong":     _parse_amount(_text(hhdvu, "SLuong")),
                "don_gia":      _parse_amount(_text(hhdvu, "DGia")),
                "ty_le_ck":     _parse_amount(_text(hhdvu, "TLCKhau")),   # Tỷ lệ chiết khấu ← MỚI
                "so_tien_ck":   _parse_amount(_text(hhdvu, "STCKhau")),   # Số tiền chiết khấu ← MỚI
                "thanh_tien":   thanh_tien,                                # ThTien (trước thuế)
                "thue_suat":    _text(hhdvu, "TSuat"),                     # Thuế suất
                "tien_thue":    _parse_amount(ttkhac_vat) if ttkhac_vat else None,  # Tiền thuế VAT ← MỚI
                "tong_tien":    _parse_amount(ttkhac_amount) if ttkhac_amount else None,  # Tổng tiền sau thuế ← MỚI
                "tt_hh_trung":  _text(hhdvu, "TTHHDTrung"),  # Thông tin hàng hóa trung gian ← MỚI
            }
            items.append(item)

        return items