"""Invoice list / detail / snapshot-batch blueprint (gộp từ invoices.py + snapshot.py)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from flask import Blueprint, abort, jsonify, render_template, request, send_file

from app.services.invoice_service import InvoiceService
from app.services.excel_service import ExcelService, _parse_batch_month_year
from app.db.repository import GdtAccountRepository, SnapshotRepository

bp = Blueprint("invoices", __name__, url_prefix="/invoices")


# ═══════════════════════════════════════════════════════════════════ UI

@bp.get("/")
def index():
    """
    Trang hóa đơn: hỗ trợ 2 chế độ xem qua query param `batch_id`:
      - không có batch_id → "Hóa đơn mới" (chưa chốt) — có checkbox chọn + nút Chốt batch
      - có batch_id       → xem hóa đơn trong batch đã chốt đó — có nút edit + export riêng
    """
    per_page       = 2000  # không phân trang thật — giới hạn để tránh quá tải
    search         = request.args.get("search",           "").strip() or None
    start_date     = request.args.get("start_date",       "").strip() or None
    end_date       = request.args.get("end_date",         "").strip() or None
    ghi_chu        = request.args.get("ghi_chu",          "").strip() or None
    thang_ke_khai  = request.args.get("thang_ke_khai",    "").strip() or None
    invoice_category_list = [v.strip() for v in request.args.getlist("invoice_category") if v.strip()]
    account_id_str = request.args.get("account_id",       "").strip() or None
    account_id     = int(account_id_str) if account_id_str and account_id_str.isdigit() else None
    batch_id_str   = request.args.get("batch_id",         "").strip() or None
    batch_id       = int(batch_id_str) if batch_id_str and batch_id_str.isdigit() else None

    accounts     = GdtAccountRepository.get_all()
    batches      = SnapshotRepository.get_all(account_id=account_id)
    new_count    = SnapshotRepository.count_new(account_id=account_id)
    active_batch = SnapshotRepository.get_by_id(batch_id) if batch_id else None

    if active_batch:
        pagination = SnapshotRepository.get_batch_invoices(
            batch_id=active_batch.id,
            page=1, per_page=per_page,
            search=search,
            invoice_category=invoice_category_list or None,
        )
    else:
        start_dt = _parse_dt(start_date)
        end_dt   = _parse_dt(end_date)
        pagination = SnapshotRepository.get_new_invoices(
            page=1, per_page=per_page,
            search=search,
            invoice_category=invoice_category_list or None,
            start_date=start_dt, end_date=end_dt,
            account_id=account_id,
            ghi_chu=ghi_chu,
            thang_ke_khai=thang_ke_khai,
        )

    return render_template(
        "invoices.html",
        pagination=pagination,
        accounts=accounts,
        batches=batches,
        new_count=new_count,
        active_batch=active_batch.to_dict() if active_batch else None,
        search=search               or "",
        start_date=start_date       or "",
        end_date=end_date           or "",
        ghi_chu=ghi_chu             or "",
        thang_ke_khai=thang_ke_khai or "",
        invoice_category_list=invoice_category_list,
        account_id=account_id_str or "",
        batch_id=batch_id_str or "",
    )


@bp.get("/<int:invoice_id>")
def detail(invoice_id: int):
    detail = InvoiceService.get_detail(invoice_id)
    if not detail:
        abort(404)
    view_html_content = ""
    if detail.get("view_html_path"):
        html_path = detail["view_html_path"]
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8", errors="replace") as f:
                view_html_content = f.read()
    detail["view_html_content"] = view_html_content
    return render_template("invoice_detail.html", invoice=detail)


# ═══════════════════════════════════════════════════════════════ JSON API — list

@bp.get("/api/list")
def api_list():
    page     = request.args.get("page",     1,  type=int)
    per_page = request.args.get("per_page", 20, type=int)
    search   = request.args.get("search",   "").strip() or None
    pagination = InvoiceService.get_paginated(page=page, per_page=per_page, search=search)
    return jsonify({
        "items": [i.to_dict() for i in pagination.items],
        "total": pagination.total,
        "pages": pagination.pages,
        "page":  pagination.page,
    })


# ═══════════════════════════════════════════════════════════════ JSON API — extra fields

_EXTRA_FIELDS = {
    "thang_ke_khai", "payment_note", "bank_name",
    "ghi_chu", "ma_cong_trinh", "so_hop_dong", "ngay_hop_dong",
    "hd_ban_ra_tuong_ung",
}

@bp.patch("/api/<int:invoice_id>/extra")
def api_update_extra(invoice_id: int):
    """Cập nhật các field bổ sung (không lấy từ XML) cho một hóa đơn."""
    from app.db.models import Invoice
    from app.extensions import db

    inv = db.session.get(Invoice, invoice_id)
    if not inv:
        return jsonify(ok=False, error="Invoice not found"), 404

    data = request.get_json(force=True, silent=True) or {}
    updated = {}
    for field in _EXTRA_FIELDS:
        if field in data:
            value = data[field].strip() if isinstance(data[field], str) else data[field]
            setattr(inv, field, value or None)
            updated[field] = value or None

    if not updated:
        return jsonify(ok=False, error="No valid fields provided"), 400

    db.session.commit()
    return jsonify(ok=True, updated=updated)


# ═══════════════════════════════════════════════════════════════ JSON API — snapshot batch

@bp.get("/api/batch/stats")
def api_batch_stats():
    account_id = _parse_int(request.args.get("account_id", ""))
    batches   = SnapshotRepository.get_all(account_id=account_id)
    new_count = SnapshotRepository.count_new(account_id=account_id)
    return jsonify({
        "new_count":   new_count,
        "batch_count": len(batches),
        "batches":     [b.to_dict() for b in batches],
    })


@bp.get("/api/batch/years")
def api_batch_years():
    """Danh sách các năm có batch (đã chốt) — dùng cho dropdown 'Xuất theo năm'."""
    account_id = _parse_int(request.args.get("account_id", ""))
    if not account_id:
        return jsonify(ok=False, error="Thiếu account_id."), 400

    batches = SnapshotRepository.get_all(account_id=account_id)
    years = sorted(
        {y for b in batches for _, y in [_parse_batch_month_year(b.label)] if y},
        reverse=True,
    )
    return jsonify(ok=True, years=years)


@bp.post("/api/batch/merge-or-create")
def api_batch_merge_or_create():
    """
    Gộp hóa đơn vào batch cùng label nếu đã tồn tại, ngược lại tạo mới.
    Body JSON: { "label": "Tháng 3/2026", "note": "...", "invoice_ids": [...], "account_id": 1 }
    """
    data = request.get_json(force=True, silent=True) or {}
    label       = (data.get("label") or "").strip()
    note        = (data.get("note")  or "").strip() or None
    invoice_ids = data.get("invoice_ids") or []
    account_id  = _parse_int(str(data.get("account_id") or ""))

    if not label:
        return jsonify(ok=False, error="Vui lòng chọn tháng."), 400
    if not invoice_ids or not isinstance(invoice_ids, list):
        return jsonify(ok=False, error="Chưa chọn hóa đơn nào."), 400
    if not account_id:
        return jsonify(ok=False, error="Vui lòng chọn công ty trước khi chốt batch."), 400

    try:
        invoice_ids = [int(i) for i in invoice_ids]
    except (TypeError, ValueError):
        return jsonify(ok=False, error="invoice_ids không hợp lệ."), 400

    batch, merged = SnapshotRepository.merge_or_create(
        label=label, invoice_ids=invoice_ids, note=note, account_id=account_id
    )
    return jsonify(ok=True, batch=batch.to_dict(), merged=merged)


@bp.delete("/api/batch/<int:batch_id>")
def api_batch_delete(batch_id: int):
    """Xoá batch và trả các hóa đơn về trạng thái 'mới'."""
    batch = SnapshotRepository.get_by_id(batch_id)
    if not batch:
        return jsonify(ok=False, error="Batch không tồn tại."), 404

    released = SnapshotRepository.delete(batch_id)
    return jsonify(ok=True, released=released)


# ═══════════════════════════════════════════════════════════════ Export

@bp.post("/api/export/new")
def api_export_new():
    """Export Excel toàn bộ HĐ mới (có thể kết hợp filter)."""
    data         = request.get_json(force=True, silent=True) or {}
    search       = (data.get("search") or "").strip() or None
    category_list = _normalize_category_list(data.get("invoice_category"))
    start_dt = _parse_dt(data.get("start_date", ""))
    end_dt   = _parse_dt(data.get("end_date",   ""))
    account_id = _parse_int(str(data.get("account_id") or ""))

    invoices = SnapshotRepository.get_new_for_export(
        search=search, invoice_category=category_list or None,
        start_date=start_dt, end_date=end_dt,
        account_id=account_id,
    )
    if not invoices:
        return jsonify(ok=False, error="Không có hóa đơn nào để xuất."), 404

    try:
        path = ExcelService.export_from_list(invoices, label="HĐ mới chưa chốt", account_id=account_id)
        return jsonify(ok=True, file=path.name, path=str(path))
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@bp.post("/api/export/batch/<int:batch_id>")
def api_export_batch(batch_id: int):
    """Export Excel một batch đã chốt."""
    batch = SnapshotRepository.get_by_id(batch_id)
    if not batch:
        return jsonify(ok=False, error="Batch không tồn tại."), 404

    invoices = SnapshotRepository.get_batch_for_export(batch_id)
    if not invoices:
        return jsonify(ok=False, error="Batch không có hóa đơn."), 404

    try:
        path = ExcelService.export_from_list(invoices, label=batch.label, account_id=batch.account_id)
        return jsonify(ok=True, file=path.name, path=str(path))
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@bp.post("/api/export/year")
def api_export_year():
    """
    Export Excel gộp toàn bộ các batch (tháng) đã chốt trong một năm, cho một tài khoản.
    Body JSON: { "year": 2026, "account_id": 1 }
    """
    data       = request.get_json(force=True, silent=True) or {}
    year       = _parse_int(str(data.get("year") or ""))
    account_id = _parse_int(str(data.get("account_id") or ""))

    if not year:
        return jsonify(ok=False, error="Vui lòng chọn năm."), 400
    if not account_id:
        return jsonify(ok=False, error="Vui lòng chọn công ty."), 400

    try:
        path = ExcelService.export_by_year(year=year, account_id=account_id)
        return jsonify(ok=True, file=path.name, path=str(path))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@bp.get("/download/<filename>")
def download_export(filename: str):
    """Tải file Excel export (chung dùng cho cả export new/batch/year)."""
    from app.config import Config
    safe = (Config.EXPORT_PATH / filename).resolve()
    if not str(safe).startswith(str(Config.EXPORT_PATH.resolve())):
        return "Forbidden", 403
    if not safe.exists():
        return "Not found", 404
    return send_file(safe, as_attachment=True, download_name=filename)


# ═══════════════════════════════════════════════════════════════ File download (zip/xml/html/pdf)

_FILE_TYPE_FIELD = {
    "zip":  "zip_path",
    "xml":  "xml_data_path",
    "html": "view_html_path",
    "pdf":  "pdf_path",
}

_FILE_TYPE_MIME = {
    "zip":  "application/zip",
    "xml":  "application/xml",
    "html": "text/html",
    "pdf":  "application/pdf",
}


@bp.get("/api/<int:invoice_id>/download/<file_type>")
def api_download(invoice_id: int, file_type: str):
    """Serve invoice file (zip / xml / html / pdf) for direct browser download."""
    if file_type not in _FILE_TYPE_FIELD:
        abort(400)

    detail = InvoiceService.get_detail(invoice_id)
    if not detail:
        abort(404)

    file_path = detail.get(_FILE_TYPE_FIELD[file_type])
    if not file_path:
        abort(404)

    p = Path(file_path)
    if not p.exists():
        abort(404)

    return send_file(
        p,
        mimetype=_FILE_TYPE_MIME[file_type],
        as_attachment=True,
        download_name=p.name,
    )


# ═══════════════════════════════════════════════════════════════ PDF generation

@bp.post("/<int:invoice_id>/generate-pdf")
def api_generate_pdf(invoice_id: int):
    from app.db.models import Invoice
    from app.automation.invoice_export import generate_invoice_pdf
    from app.extensions import db

    inv = Invoice.query.get_or_404(invoice_id)
    if not inv.invoice_dir:
        return jsonify(ok=False, error="Invoice directory not found")

    pdf_path = generate_invoice_pdf(inv.invoice_dir)
    if pdf_path and pdf_path.exists():
        inv.has_pdf  = True
        inv.pdf_path = str(pdf_path)
        db.session.commit()
        return jsonify(ok=True, file=pdf_path.name)
    else:
        return jsonify(ok=False, error="PDF generation failed. Ensure Playwright is installed.")


@bp.get("/<int:invoice_id>/download/pdf")
def api_download_pdf(invoice_id: int):
    from app.db.models import Invoice

    inv = Invoice.query.get_or_404(invoice_id)
    if not inv.pdf_path or not Path(inv.pdf_path).exists():
        abort(404)
    return send_file(
        inv.pdf_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"invoice_{inv.invoice_no}.pdf",
    )


# ═══════════════════════════════════════════════════════════════ Helpers

def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _normalize_category_list(value) -> List[str]:
    """Chuẩn hoá invoice_category nhận từ JSON body — hỗ trợ cả list và string đơn (tương thích cũ)."""
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []