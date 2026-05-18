"""
Excel export service — generates VAT summary reports.
Output format mirrors BẢNG KÊ HÓA ĐƠN CHỨNG TỪ HÀNG HÓA, DỊCH VỤ MUA VÀO / BÁN RA.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from loguru import logger
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.config import Config
from app.db.models import Invoice
from app.db.repository import InvoiceRepository

# ── Style constants ────────────────────────────────────────────────────────
_THIN   = Side(style="thin")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

_BLUE_FILL   = PatternFill("solid", start_color="1F4E79", end_color="1F4E79")   # dark blue header
_LIGHT_FILL  = PatternFill("solid", start_color="D6E4F0", end_color="D6E4F0")   # light blue sub-header
_TOTAL_FILL  = PatternFill("solid", start_color="FFEB9C", end_color="FFEB9C")   # yellow total
_ALT_FILL    = PatternFill("solid", start_color="F2F2F2", end_color="F2F2F2")   # grey alt row

_WHITE_BOLD  = Font(name="Arial", bold=True, color="FFFFFF", size=10)
_BLACK_BOLD  = Font(name="Arial", bold=True, color="000000", size=10)
_BLACK_NORM  = Font(name="Arial", size=10)
_TITLE_FONT  = Font(name="Arial", bold=True, size=12)
_SUB_FONT    = Font(name="Arial", bold=True, size=10, color="1F4E79")

_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_LEFT   = Alignment(horizontal="left",   vertical="center", wrap_text=True)
_RIGHT  = Alignment(horizontal="right",  vertical="center")

# ── Column definitions for MUA VÀO ────────────────────────────────────────
# (header, width, attr_or_callable)
_MUA_VAO_COLS = [
    ("STT",                          6,   "stt"),
    ("Ký hiệu mẫu số",              10,   "invoice_form"),
    ("Ký hiệu hóa đơn",            14,   "invoice_symbol"),
    ("Số hóa đơn",                  14,   "invoice_no"),
    ("Ngày lập",                    13,   "issue_date_fmt"),
    ("Tên người bán",               36,   "seller_name"),
    ("MST người bán",               18,   "seller_tax_code"),
    ("Mặt hàng",                    36,   "mat_hang"),
    ("Doanh số mua vào\nchưa có thuế", 18, "amount"),
    ("Thuế suất %",                 10,   "vat_rate"),
    ("Thuế GTGT",                   15,   "vat_amount"),
    ("Thành tiền VND",              16,   "total_amount"),
    ("Tháng kê khai",               14,   "thang_ke_khai"),
    ("Chứng từ thanh toán\n(TM/CK)", 22, "payment_note"),
    ("Ngân hàng",                   12,   "bank_name"),
    ("Điều chỉnh",                  12,   "adj"),
    ("Bị điều chỉnh",               12,   "adj2"),
    ("Ghi chú",                     22,   "ghi_chu"),
    ("Mã công trình",               16,   "ma_cong_trinh"),
    ("Số hợp đồng",                 24,   "so_hop_dong"),
    ("Ngày hợp đồng",               14,   "ngay_hop_dong"),
    ("HĐ bán ra\ntương ứng",        20,   "hd_ban_ra_tuong_ung"),
]

# ── Column definitions for BÁN RA ────────────────────────────────────────
_BAN_RA_COLS = [
    ("STT",                          6,   "stt"),
    ("Ký hiệu hóa đơn",            14,   "invoice_symbol"),
    ("Số HĐ",                       14,   "invoice_no"),
    ("Ngày HĐ",                     13,   "issue_date_fmt"),
    ("Tên người mua",               36,   "buyer_name"),
    ("Mã số thuế",                  18,   "buyer_tax_code"),
    ("Mặt hàng",                    36,   "mat_hang"),
    ("Doanh số bán\nchưa thuế",     18,   "amount"),
    ("Thuế suất",                   10,   "vat_rate"),
    ("Thuế GTGT",                   15,   "vat_amount"),
    ("Tổng thanh toán",             16,   "total_amount"),
    ("Điều chỉnh",                  12,   "adj"),
    ("Bị điều chỉnh",               12,   "adj2"),
    ("Mã công trình",               16,   "ma_cong_trinh"),
    ("Số hợp đồng",                 24,   "so_hop_dong"),
    ("Ngày hợp đồng",               14,   "ngay_hop_dong"),
    ("Thanh tiền (TM/CK)",          22,   "payment_note"),
    ("Tháng",                       12,   "thang_ke_khai"),
]


class ExcelService:

    @staticmethod
    def export_vat_summary(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        ghi_chu: Optional[str] = None,
        thang_ke_khai: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Path:
        Config.EXPORT_PATH.mkdir(parents=True, exist_ok=True)

        start_dt = _parse_date(start_date)
        end_dt   = _parse_date(end_date)

        invoices = InvoiceRepository.get_for_export(
            start_date=start_dt,
            end_date=end_dt,
            ghi_chu=ghi_chu,
            thang_ke_khai=thang_ke_khai,
            search=search,
        )

        # Separate purchase vs sale; if invoice_type not set, treat all as purchase
        mua_vao = [i for i in invoices if (i.invoice_type or "").upper() != "SALE"]
        ban_ra  = [i for i in invoices if (i.invoice_type or "").upper() == "SALE"]

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename  = f"bang_ke_hoa_don_{timestamp}.xlsx"
        dest      = Config.EXPORT_PATH / filename

        wb = Workbook()

        ws_mua = wb.active
        _build_sheet(ws_mua, "MUA VÀO", mua_vao, _MUA_VAO_COLS, is_purchase=True)

        ws_ban = wb.create_sheet("BÁN RA")
        _build_sheet(ws_ban, "BÁN RA", ban_ra, _BAN_RA_COLS, is_purchase=False)

        wb.save(str(dest))
        logger.info("Excel report saved: {}", dest)
        return dest


# ── Sheet builder ─────────────────────────────────────────────────────────

def _build_sheet(ws, sheet_label: str, invoices: List[Invoice], col_defs, is_purchase: bool) -> None:
    num_cols = len(col_defs)
    last_col = get_column_letter(num_cols)

    # Row 1: company header block (mirrors the sample file)
    ws.merge_cells(f"A1:{last_col}1")
    c = ws["A1"]
    c.value     = "BẢNG KÊ HÓA ĐƠN CHỨNG TỪ HÀNG HÓA, DỊCH VỤ " + ("MUA VÀO" if is_purchase else "BÁN RA")
    c.font      = _TITLE_FONT
    c.alignment = _CENTER
    ws.row_dimensions[1].height = 24

    ws.merge_cells(f"A2:{last_col}2")
    ws["A2"].value     = f"Xuất ngày: {datetime.now().strftime('%d/%m/%Y %H:%M')}   —   Tổng: {len(invoices)} hóa đơn"
    ws["A2"].font      = _BLACK_NORM
    ws["A2"].alignment = _CENTER
    ws.row_dimensions[2].height = 16

    # Row 3: column headers
    for col_idx, (hdr, width, _) in enumerate(col_defs, 1):
        cell = ws.cell(row=3, column=col_idx, value=hdr)
        cell.font      = _WHITE_BOLD
        cell.fill      = _BLUE_FILL
        cell.border    = _BORDER
        cell.alignment = _CENTER
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.row_dimensions[3].height = 36

    ws.freeze_panes = "A4"

    # Data rows — group by thang_ke_khai for subtotals
    groups = _group_by_ky(invoices)
    current_row = 4
    grand_amount  = grand_vat  = grand_total = 0.0

    for ky_label, ky_invoices in groups:
        stt = 1
        ky_amount = ky_vat = ky_total = 0.0

        for inv in ky_invoices:
            row_data = _invoice_to_row(inv, stt, col_defs)
            alt = stt % 2 == 0

            for col_idx, val in enumerate(row_data, 1):
                cell        = ws.cell(row=current_row, column=col_idx, value=val)
                cell.border = _BORDER
                cell.font   = _BLACK_NORM
                if alt:
                    cell.fill = _ALT_FILL

                # Right-align numbers
                _, _, field = col_defs[col_idx - 1]
                if field in ("amount", "vat_amount", "total_amount"):
                    cell.alignment    = _RIGHT
                    cell.number_format = '#,##0'
                elif field == "stt":
                    cell.alignment = _CENTER
                else:
                    cell.alignment = _LEFT

            ky_amount += inv.amount       or 0
            ky_vat    += inv.vat_amount   or 0
            ky_total  += inv.total_amount or 0
            stt           += 1
            current_row   += 1

        # Subtotal row per quarter/period
        if ky_label:
            _write_subtotal_row(ws, current_row, num_cols, ky_label, ky_amount, ky_vat, ky_total, col_defs)
            current_row += 1

        grand_amount += ky_amount
        grand_vat    += ky_vat
        grand_total  += ky_total

    # Grand total
    _write_subtotal_row(ws, current_row, num_cols, "TỔNG CỘNG", grand_amount, grand_vat, grand_total, col_defs, is_grand=True)


def _write_subtotal_row(ws, row, num_cols, label, amount, vat, total, col_defs, is_grand=False):
    fill = _TOTAL_FILL
    font = Font(name="Arial", bold=True, size=10)

    # Find column indices for amount fields
    amount_col = vat_col = total_col = None
    for i, (_, _, field) in enumerate(col_defs, 1):
        if field == "amount":      amount_col = i
        if field == "vat_amount":  vat_col    = i
        if field == "total_amount": total_col = i

    # Label spanning first columns up to amount-1
    span_end = (amount_col - 1) if amount_col else num_cols
    if span_end >= 1:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=span_end)
    label_cell            = ws.cell(row=row, column=1, value=("TỔNG " + label) if not is_grand else label)
    label_cell.font       = font
    label_cell.fill       = fill
    label_cell.border     = _BORDER
    label_cell.alignment  = Alignment(horizontal="right", vertical="center")

    for col_idx in range(1, num_cols + 1):
        cell        = ws.cell(row=row, column=col_idx)
        cell.fill   = fill
        cell.border = _BORDER
        cell.font   = font

    if amount_col:
        c = ws.cell(row=row, column=amount_col, value=amount)
        c.number_format = '#,##0'
        c.fill = fill; c.border = _BORDER; c.font = font; c.alignment = _RIGHT
    if vat_col:
        c = ws.cell(row=row, column=vat_col, value=vat)
        c.number_format = '#,##0'
        c.fill = fill; c.border = _BORDER; c.font = font; c.alignment = _RIGHT
    if total_col:
        c = ws.cell(row=row, column=total_col, value=total)
        c.number_format = '#,##0'
        c.fill = fill; c.border = _BORDER; c.font = font; c.alignment = _RIGHT

    ws.row_dimensions[row].height = 18


def _invoice_to_row(inv: Invoice, stt: int, col_defs) -> list:
    """Map an Invoice ORM object to a list of cell values matching col_defs."""
    date_str = inv.issue_date.strftime("%d/%m/%Y") if inv.issue_date else ""
    row = []
    for _, _, field in col_defs:
        if field == "stt":               row.append(stt)
        elif field == "issue_date_fmt":  row.append(date_str)
        elif field == "amount":          row.append(inv.amount       or 0)
        elif field == "vat_amount":      row.append(inv.vat_amount   or 0)
        elif field == "total_amount":    row.append(inv.total_amount or 0)
        elif field in ("adj", "adj2"):   row.append("")
        else:
            row.append(getattr(inv, field, "") or "")
    return row


def _group_by_ky(invoices: List[Invoice]):
    """
    Group invoices by thang_ke_khai (e.g. QUÝ I/2026).
    Returns list of (label, [Invoice]) in order encountered.
    Invoices with no thang_ke_khai go into a single unlabelled group at the end.
    """
    ordered_keys = []
    groups: dict = defaultdict(list)
    for inv in invoices:
        key = inv.thang_ke_khai or ""
        if key not in ordered_keys:
            ordered_keys.append(key)
        groups[key].append(inv)

    result = []
    for key in ordered_keys:
        # Only emit subtotal label for named quarters
        label = key if key else ""
        result.append((label, groups[key]))
    return result


# ── Helpers ───────────────────────────────────────────────────────────────

def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None