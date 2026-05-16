"""Invoice business logic service."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.db.repository import InvoiceRepository
from app.db.models import Invoice
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
    ):
        start_dt = _parse_dt(start_date) if start_date else None
        end_dt = _parse_dt(end_date) if end_date else None

        return InvoiceRepository.get_all(
            page=page,
            per_page=per_page,
            search=search,
            start_date=start_dt,
            end_date=end_dt,
            invoice_type=invoice_type,
        )

    @staticmethod
    def get_detail(invoice_id: int) -> Optional[Dict[str, Any]]:
        invoice = InvoiceRepository.get_by_id(invoice_id)
        if not invoice:
            return None

        detail = invoice.to_dict()

        # Attach XML line items if XML exists
        if invoice.xml_path:
            xml_file = Path(invoice.xml_path)
            if xml_file.exists():
                detail["line_items"] = XmlService.extract_line_items(xml_file)
                detail["xml_content"] = xml_file.read_text(encoding="utf-8", errors="replace")[:5000]

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