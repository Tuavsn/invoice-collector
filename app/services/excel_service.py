"""
Excel export service — generates VAT summary reports.
Output format mirrors common Vietnamese accounting/VAT summary files.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger
from openpyxl import Workbook
from openpyxl.styles import (
    Alignment,
    Border,
    Font,
    PatternFill,
    Side,
)
from openpyxl.utils import get_column_letter

from app.config import Config
from app.db.models import Invoice
from app.db.repository import InvoiceRepository


_THIN = Side(style="thin")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_HEADER_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
_SUB_FILL = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
_TOTAL_FILL = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")


class ExcelService:

    @staticmethod
    def export_vat_summary(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Path:
        """
        Generate a comprehensive VAT summary Excel workbook.
        Saves to exports/ directory and returns the file path.
        """
        Config.EXPORT_PATH.mkdir(parents=True, exist_ok=True)

        # Parse date filters
        start_dt: Optional[datetime] = None
        end_dt: Optional[datetime] = None
        if start_date:
            try:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                pass
        if end_date:
            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                pass

        invoices = InvoiceRepository.get_for_export(start_dt, end_dt)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"bao_cao_hoa_don_{timestamp}.xlsx"
        dest = Config.EXPORT_PATH / filename

        wb = Workbook()

        # Sheet 1: All invoices
        _write_invoice_sheet(wb.active, invoices, "Danh sách hóa đơn")

        # Sheet 2: VAT summary by month
        ws2 = wb.create_sheet("Tổng hợp theo tháng")
        _write_monthly_summary(ws2, invoices)

        # Sheet 3: Raw data for pivot tables
        ws3 = wb.create_sheet("Dữ liệu thô")
        _write_raw_data(ws3, invoices)

        wb.save(str(dest))
        logger.info("Excel report saved: {}", dest)
        return dest


def _header_cell(ws, row: int, col: int, value: str, bold: bool = True, fill=None) -> None:
    cell = ws.cell(row=row, column=col, value=value)
    cell.font = Font(bold=bold, color="FFFFFF" if fill == _HEADER_FILL else "000000", size=11)
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell.border = _BORDER
    if fill:
        cell.fill = fill


def _data_cell(ws, row: int, col: int, value, number_format: Optional[str] = None) -> None:
    cell = ws.cell(row=row, column=col, value=value)
    cell.border = _BORDER
    cell.alignment = Alignment(vertical="center")
    if number_format:
        cell.number_format = number_format


def _write_invoice_sheet(ws, invoices: List[Invoice], title: str) -> None:
    ws.title = title

    # Title row
    ws.merge_cells("A1:L1")
    title_cell = ws["A1"]
    title_cell.value = "BẢNG KÊ HÓA ĐƠN MUA VÀO / BÁN RA"
    title_cell.font = Font(bold=True, size=14, color="1F4E79")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    # Sub-title with date info
    ws.merge_cells("A2:L2")
    ws["A2"].value = f"Xuất ngày: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
    ws["A2"].alignment = Alignment(horizontal="center")
    ws.row_dimensions[2].height = 18

    # Column headers
    headers = [
        "STT", "Số hóa đơn", "Ký hiệu", "Ngày lập",
        "Tên người bán", "MST người bán",
        "Tên người mua", "MST người mua",
        "Tiền hàng (chưa thuế)", "Thuế GTGT", "Tổng tiền TT",
        "Trạng thái",
    ]
    for col_idx, h in enumerate(headers, 1):
        _header_cell(ws, 3, col_idx, h, fill=_HEADER_FILL)
    ws.row_dimensions[3].height = 40

    # Data rows
    for row_idx, inv in enumerate(invoices, 1):
        r = row_idx + 3
        _data_cell(ws, r, 1, row_idx)
        _data_cell(ws, r, 2, inv.invoice_no)
        _data_cell(ws, r, 3, inv.invoice_symbol)
        _data_cell(ws, r, 4, inv.issue_date.strftime("%d/%m/%Y") if inv.issue_date else "")
        _data_cell(ws, r, 5, inv.seller_name)
        _data_cell(ws, r, 6, inv.seller_tax_code)
        _data_cell(ws, r, 7, inv.buyer_name)
        _data_cell(ws, r, 8, inv.buyer_tax_code)
        _data_cell(ws, r, 9, inv.amount, number_format='#,##0')
        _data_cell(ws, r, 10, inv.vat_amount, number_format='#,##0')
        _data_cell(ws, r, 11, inv.total_amount, number_format='#,##0')
        _data_cell(ws, r, 12, inv.status or "")
        if row_idx % 2 == 0:
            for c in range(1, 13):
                ws.cell(row=r, column=c).fill = PatternFill(
                    start_color="F2F2F2", end_color="F2F2F2", fill_type="solid"
                )

    # Totals row
    total_row = len(invoices) + 4
    ws.merge_cells(f"A{total_row}:H{total_row}")
    total_label = ws.cell(row=total_row, column=1, value="TỔNG CỘNG")
    total_label.font = Font(bold=True, size=11)
    total_label.alignment = Alignment(horizontal="right")
    total_label.fill = _TOTAL_FILL

    total_amount = sum(i.amount for i in invoices)
    total_vat = sum(i.vat_amount for i in invoices)
    total_total = sum(i.total_amount for i in invoices)

    for col, val in [(9, total_amount), (10, total_vat), (11, total_total)]:
        c = ws.cell(row=total_row, column=col, value=val)
        c.number_format = '#,##0'
        c.font = Font(bold=True)
        c.fill = _TOTAL_FILL
        c.border = _BORDER

    # Column widths
    col_widths = [6, 18, 12, 14, 40, 18, 40, 18, 20, 18, 20, 15]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A4"


def _write_monthly_summary(ws, invoices: List[Invoice]) -> None:
    ws.merge_cells("A1:F1")
    ws["A1"].value = "TỔNG HỢP HÓA ĐƠN THEO THÁNG"
    ws["A1"].font = Font(bold=True, size=13, color="1F4E79")
    ws["A1"].alignment = Alignment(horizontal="center")

    headers = ["Tháng", "Số hóa đơn", "Tổng tiền hàng", "Tổng thuế GTGT", "Tổng tiền TT", "TB / hóa đơn"]
    for col_idx, h in enumerate(headers, 1):
        _header_cell(ws, 2, col_idx, h, fill=_HEADER_FILL)

    # Group by month
    from collections import defaultdict
    monthly: dict = defaultdict(lambda: {"count": 0, "amount": 0.0, "vat": 0.0, "total": 0.0})
    for inv in invoices:
        if inv.issue_date:
            key = inv.issue_date.strftime("%Y-%m")
            monthly[key]["count"] += 1
            monthly[key]["amount"] += inv.amount
            monthly[key]["vat"] += inv.vat_amount
            monthly[key]["total"] += inv.total_amount

    for row_idx, (month, data) in enumerate(sorted(monthly.items()), 1):
        r = row_idx + 2
        avg = data["total"] / data["count"] if data["count"] else 0
        _data_cell(ws, r, 1, month)
        _data_cell(ws, r, 2, data["count"])
        _data_cell(ws, r, 3, data["amount"], '#,##0')
        _data_cell(ws, r, 4, data["vat"], '#,##0')
        _data_cell(ws, r, 5, data["total"], '#,##0')
        _data_cell(ws, r, 6, avg, '#,##0')

    for i, w in enumerate([14, 14, 22, 22, 22, 22], 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _write_raw_data(ws, invoices: List[Invoice]) -> None:
    ws["A1"].value = "invoice_no"
    ws["B1"].value = "invoice_symbol"
    ws["C1"].value = "issue_date"
    ws["D1"].value = "seller_name"
    ws["E1"].value = "seller_tax_code"
    ws["F1"].value = "buyer_name"
    ws["G1"].value = "buyer_tax_code"
    ws["H1"].value = "amount"
    ws["I1"].value = "vat_amount"
    ws["J1"].value = "total_amount"
    ws["K1"].value = "status"

    for row_idx, inv in enumerate(invoices, 2):
        ws.cell(row_idx, 1, inv.invoice_no)
        ws.cell(row_idx, 2, inv.invoice_symbol)
        ws.cell(row_idx, 3, inv.issue_date.strftime("%Y-%m-%d") if inv.issue_date else "")
        ws.cell(row_idx, 4, inv.seller_name)
        ws.cell(row_idx, 5, inv.seller_tax_code)
        ws.cell(row_idx, 6, inv.buyer_name)
        ws.cell(row_idx, 7, inv.buyer_tax_code)
        ws.cell(row_idx, 8, inv.amount)
        ws.cell(row_idx, 9, inv.vat_amount)
        ws.cell(row_idx, 10, inv.total_amount)
        ws.cell(row_idx, 11, inv.status)