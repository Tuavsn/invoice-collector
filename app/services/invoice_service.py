"""Invoice business logic service."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.db.repository import InvoiceRepository
from app.services.xml_service import XmlService


class InvoiceService:

    @staticmethod
    def get_paginated(
        page: int = 1,
        per_page: int = 20,
        search: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        invoice_type: Optional[str] = None,
        ghi_chu: Optional[str] = None,
        thang_ke_khai: Optional[str] = None,
    ):
        start_dt = _parse_dt(start_date) if start_date else None
        end_dt   = _parse_dt(end_date)   if end_date   else None

        return InvoiceRepository.get_all(
            page=page,
            per_page=per_page,
            search=search,
            start_date=start_dt,
            end_date=end_dt,
            invoice_type=invoice_type,
            ghi_chu=ghi_chu,
            thang_ke_khai=thang_ke_khai,
        )

    @staticmethod
    def get_detail(invoice_id: int) -> Optional[Dict[str, Any]]:
        invoice = InvoiceRepository.get_by_id(invoice_id)
        if not invoice:
            return None

        detail = invoice.to_dict()

        xml_data_path = detail.get("xml_data_path")
        if xml_data_path:
            xml_file = Path(xml_data_path)
            if xml_file.exists():
                raw_bytes = xml_file.read_bytes()
                if not detail.get("line_items"):
                    detail["line_items"] = XmlService.parse_line_items(raw_bytes)
                detail["xml_data_content"] = raw_bytes.decode("utf-8", errors="replace")[:5000]
            else:
                logger.warning("xml_data_path trỏ tới file không tồn tại: {}", xml_data_path)

        for path_field, flag_field in [
            ("zip_path",       "has_zip"),
            ("xml_data_path",  "has_xml"),
            ("view_html_path", "has_html"),
            ("pdf_path",       "has_pdf"),
        ]:
            p = detail.get(path_field)
            if not detail.get(flag_field):
                detail[flag_field] = bool(p and Path(p).exists())

        return detail

    @staticmethod
    def get_stats() -> Dict[str, Any]:
        return InvoiceRepository.stats()

    @staticmethod
    def get_monthly_summary() -> List[Dict[str, Any]]:
        return InvoiceRepository.monthly_summary()


def _parse_dt(value: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None