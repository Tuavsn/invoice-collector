"""
XML parsing service for Vietnam e-invoice (hóa đơn điện tử) XML format.
Supports both TCVN and ETAX XML schemas.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from xml.etree import ElementTree as ET

from loguru import logger


# Common namespace prefixes used in GDT invoice XML
_NAMESPACES = {
    "inv": "http://laphoadon.gdt.gov.vn/2014/09/invoicexml/v1",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}


class XmlService:
    @staticmethod
    def parse_invoice_xml(xml_path: Path) -> Optional[Dict[str, Any]]:
        """
        Parse a GDT invoice XML file and extract key metadata fields.
        Returns a dict suitable for merging into the invoice DB record.
        """
        if not xml_path.exists():
            logger.warning("XML file not found: {}", xml_path)
            return None

        try:
            tree = ET.parse(str(xml_path))
            root = tree.getroot()

            # Strip namespace from tag for easier matching
            def tag(el) -> str:
                return el.tag.split("}")[-1] if "}" in el.tag else el.tag

            def find_text(root_el, *paths: str) -> Optional[str]:
                """Try multiple XPath-like paths and return first non-empty result."""
                for path in paths:
                    try:
                        el = root_el.find(path, _NAMESPACES)
                        if el is not None and el.text:
                            return el.text.strip()
                    except Exception:
                        pass
                return None

            def find_text_ns_free(root_el, tag_name: str) -> Optional[str]:
                """Walk tree and find first element matching tag name (namespace-free)."""
                for el in root_el.iter():
                    if tag(el) == tag_name and el.text:
                        return el.text.strip()
                return None

            # ── Invoice header
            invoice_no = (
                find_text_ns_free(root, "SHDon")
                or find_text_ns_free(root, "So")
                or find_text_ns_free(root, "invoiceNumber")
            )
            invoice_symbol = (
                find_text_ns_free(root, "KHHDon")
                or find_text_ns_free(root, "KyHieu")
                or find_text_ns_free(root, "invoiceSeries")
            )
            issue_date_raw = (
                find_text_ns_free(root, "NLap")
                or find_text_ns_free(root, "NgayLap")
                or find_text_ns_free(root, "invoiceIssuedDate")
            )

            # ── Seller
            seller_name = (
                find_text_ns_free(root, "TNNBan")
                or find_text_ns_free(root, "TenNguoiBan")
                or find_text_ns_free(root, "sellerLegalName")
            )
            seller_tax_code = (
                find_text_ns_free(root, "MST")
                or find_text_ns_free(root, "MaSoThue")
                or find_text_ns_free(root, "sellerTaxCode")
            )

            # ── Buyer
            buyer_name = (
                find_text_ns_free(root, "TNNMua")
                or find_text_ns_free(root, "TenNguoiMua")
                or find_text_ns_free(root, "buyerLegalName")
            )
            buyer_tax_code = (
                find_text_ns_free(root, "MSTNMua")
                or find_text_ns_free(root, "MaSoThueNguoiMua")
                or find_text_ns_free(root, "buyerTaxCode")
            )

            # ── Amounts
            def parse_amount(raw: Optional[str]) -> float:
                if not raw:
                    return 0.0
                cleaned = raw.replace(",", "").replace(".", "").strip()
                try:
                    return float(cleaned)
                except ValueError:
                    try:
                        return float(raw.replace(",", "."))
                    except ValueError:
                        return 0.0

            amount_raw = (
                find_text_ns_free(root, "TgTCThue")
                or find_text_ns_free(root, "TongTienChuaThue")
                or find_text_ns_free(root, "totalAmountWithoutVat")
            )
            vat_raw = (
                find_text_ns_free(root, "TgTThue")
                or find_text_ns_free(root, "TongTienThue")
                or find_text_ns_free(root, "totalVatAmount")
            )
            total_raw = (
                find_text_ns_free(root, "TgTTTBSo")
                or find_text_ns_free(root, "TongTienThanhToan")
                or find_text_ns_free(root, "totalAmount")
            )

            # ── Parse issue date
            issue_date = None
            if issue_date_raw:
                from app.utils.dates import parse_date
                # GDT XML may use yyyy-MM-dd or dd/MM/yyyy
                for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
                    from datetime import datetime
                    try:
                        issue_date = datetime.strptime(issue_date_raw[:10], fmt[:8]).date()
                        break
                    except ValueError:
                        continue

            result: Dict[str, Any] = {
                "invoice_no": invoice_no,
                "invoice_symbol": invoice_symbol,
                "seller_name": seller_name,
                "seller_tax_code": seller_tax_code,
                "buyer_name": buyer_name,
                "buyer_tax_code": buyer_tax_code,
                "amount": parse_amount(amount_raw),
                "vat_amount": parse_amount(vat_raw),
                "total_amount": parse_amount(total_raw),
            }

            if issue_date:
                result["issue_date"] = issue_date

            logger.debug(
                "Parsed XML: #{} symbol={} total={}",
                invoice_no,
                invoice_symbol,
                result["total_amount"],
            )
            return result

        except ET.ParseError as exc:
            logger.error("XML parse error in {}: {}", xml_path.name, exc)
            return None
        except Exception as exc:
            logger.exception("Unexpected XML parsing error {}: {}", xml_path.name, exc)
            return None

    @staticmethod
    def extract_line_items(xml_path: Path) -> list:
        """Extract invoice line items (hàng hóa) from XML."""
        items = []
        if not xml_path.exists():
            return items

        try:
            tree = ET.parse(str(xml_path))
            root = tree.getroot()

            def tag(el) -> str:
                return el.tag.split("}")[-1] if "}" in el.tag else el.tag

            for el in root.iter():
                if tag(el) in ("HHDVu", "HangHoa", "lineItem", "Item"):
                    item: Dict[str, Any] = {}
                    for child in el:
                        item[tag(child)] = child.text
                    if item:
                        items.append(item)

        except Exception as exc:
            logger.warning("Line items extraction failed: {}", exc)

        return items