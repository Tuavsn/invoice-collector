"""
Snapshot blueprint — quản lý hóa đơn mới / chốt batch.

Routes:
  GET  /snapshot/                     → trang chính (HĐ mới + danh sách batch)
  GET  /snapshot/batch/<id>           → xem HĐ trong một batch cụ thể
  POST /snapshot/api/create           → tạo batch chốt từ danh sách invoice_ids
  DELETE /snapshot/api/batch/<id>     → xoá batch (giải phóng HĐ về "mới")
  GET  /snapshot/api/new              → JSON danh sách HĐ mới (AJAX/filter)
  GET  /snapshot/api/stats            → đếm HĐ mới + tổng batch
  POST /snapshot/api/export/new       → export Excel HĐ mới
  POST /snapshot/api/export/batch/<id>→ export Excel một batch
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from flask import Blueprint, abort, jsonify, render_template, request, send_file

from app.db.repository import SnapshotRepository, GdtAccountRepository
from app.services.excel_service import ExcelService

bp = Blueprint("snapshot", __name__, url_prefix="/snapshot")


# ─────────────────────────────────────────────────────────────── UI

@bp.get("/")
def index():
    """Trang chính: danh sách HĐ mới + sidebar danh sách batch đã chốt."""
    search        = request.args.get("search",           "").strip() or None
    # Loại hóa đơn — hỗ trợ chọn nhiều (checkbox), nhận dạng list từ query string
    category_list = [v.strip() for v in request.args.getlist("invoice_category") if v.strip()]
    start_date    = request.args.get("start_date",       "").strip() or None
    end_date      = request.args.get("end_date",         "").strip() or None
    account_id    = _parse_int(request.args.get("account_id", ""))

    start_dt = _parse_dt(start_date)
    end_dt   = _parse_dt(end_date)

    pagination = SnapshotRepository.get_new_invoices(
        page=1, per_page=5000,
        search=search, invoice_category=category_list or None,
        start_date=start_dt, end_date=end_dt,
        account_id=account_id,
    )
    batches    = SnapshotRepository.get_all(account_id=account_id)
    new_count  = SnapshotRepository.count_new(account_id=account_id)
    accounts   = GdtAccountRepository.get_all()

    return render_template(
        "snapshot.html",
        pagination=pagination,
        batches=batches,
        new_count=new_count,
        accounts=accounts,
        search=search or "",
        invoice_category_list=category_list,
        start_date=start_date or "",
        end_date=end_date or "",
        account_id=account_id or "",
    )


@bp.get("/batch/<int:batch_id>")
def batch_detail(batch_id: int):
    """Xem danh sách HĐ trong một batch đã chốt."""
    batch = SnapshotRepository.get_by_id(batch_id)
    if not batch:
        abort(404)

    search        = request.args.get("search",           "").strip() or None
    category_list = [v.strip() for v in request.args.getlist("invoice_category") if v.strip()]
    account_id    = _parse_int(request.args.get("account_id", ""))

    pagination = SnapshotRepository.get_batch_invoices(
        batch_id=batch_id, page=1, per_page=5000,
        search=search, invoice_category=category_list or None,
    )
    batches   = SnapshotRepository.get_all(account_id=account_id)
    new_count = SnapshotRepository.count_new(account_id=account_id)
    accounts  = GdtAccountRepository.get_all()

    return render_template(
        "snapshot.html",
        pagination=pagination,
        batches=batches,
        new_count=new_count,
        accounts=accounts,
        active_batch=batch.to_dict(),
        search=search or "",
        invoice_category_list=category_list,
        start_date="",
        end_date="",
        account_id=account_id or "",
    )


# ─────────────────────────────────────────────────────────────── JSON API

@bp.get("/api/stats")
def api_stats():
    account_id = _parse_int(request.args.get("account_id", ""))
    batches   = SnapshotRepository.get_all(account_id=account_id)
    new_count = SnapshotRepository.count_new(account_id=account_id)
    return jsonify({
        "new_count":   new_count,
        "batch_count": len(batches),
        "batches":     [b.to_dict() for b in batches],
    })


@bp.get("/api/new")
def api_new():
    """AJAX endpoint — trả JSON hóa đơn mới để render bảng phía client."""
    search        = request.args.get("search",           "").strip() or None
    category_list = [v.strip() for v in request.args.getlist("invoice_category") if v.strip()]
    start_dt = _parse_dt(request.args.get("start_date", ""))
    end_dt   = _parse_dt(request.args.get("end_date",   ""))
    account_id = _parse_int(request.args.get("account_id", ""))

    pg = SnapshotRepository.get_new_invoices(
        page=1, per_page=5000,
        search=search, invoice_category=category_list or None,
        start_date=start_dt, end_date=end_dt,
        account_id=account_id,
    )
    return jsonify({
        "items":    [_invoice_summary(i) for i in pg.items],
        "total":    pg.total,
        "pages":    pg.pages,
        "page":     pg.page,
    })


@bp.post("/api/create")
def api_create():
    """
    Body JSON:
      { "label": "...", "note": "...", "invoice_ids": [...] }
    """
    data = request.get_json(force=True, silent=True) or {}
    label       = (data.get("label") or "").strip()
    note        = (data.get("note")  or "").strip() or None
    invoice_ids = data.get("invoice_ids") or []
    account_id  = _parse_int(str(data.get("account_id") or ""))

    if not label:
        return jsonify(ok=False, error="Vui lòng đặt tên cho batch."), 400
    if not invoice_ids or not isinstance(invoice_ids, list):
        return jsonify(ok=False, error="Chưa chọn hóa đơn nào."), 400
    if not account_id:
        return jsonify(ok=False, error="Vui lòng chọn công ty trước khi chốt batch."), 400

    try:
        invoice_ids = [int(i) for i in invoice_ids]
    except (TypeError, ValueError):
        return jsonify(ok=False, error="invoice_ids không hợp lệ."), 400

    batch = SnapshotRepository.create(label=label, invoice_ids=invoice_ids, note=note, account_id=account_id)
    return jsonify(ok=True, batch=batch.to_dict())


@bp.post("/api/merge-or-create")
def api_merge_or_create():
    """
    Gộp hóa đơn vào batch cùng label nếu đã tồn tại, ngược lại tạo mới.
    Body JSON:
      { "label": "Tháng 3/2026", "note": "...", "invoice_ids": [...] }
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

    try:
        invoice_ids = [int(i) for i in invoice_ids]
    except (TypeError, ValueError):
        return jsonify(ok=False, error="invoice_ids không hợp lệ."), 400

    batch, merged = SnapshotRepository.merge_or_create(
        label=label, invoice_ids=invoice_ids, note=note, account_id=account_id
    )
    return jsonify(ok=True, batch=batch.to_dict(), merged=merged)


@bp.delete("/api/batch/<int:batch_id>")
def api_delete_batch(batch_id: int):
    """
    Xoá batch và trả các hóa đơn về trạng thái 'mới'.
    Dùng khi chốt nhầm hoặc muốn chốt lại.
    """
    batch = SnapshotRepository.get_by_id(batch_id)
    if not batch:
        return jsonify(ok=False, error="Batch không tồn tại."), 404

    released = SnapshotRepository.delete(batch_id)
    return jsonify(ok=True, released=released)


# ─────────────────────────────────────────────────────────────── Export

@bp.post("/api/export/new")
def api_export_new():
    """Export Excel toàn bộ HĐ mới (có thể kết hợp filter)."""
    data         = request.get_json(force=True, silent=True) or {}
    search       = (data.get("search")           or "").strip() or None
    category_raw = data.get("invoice_category")
    category_list = _normalize_category_list(category_raw)
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


@bp.get("/download/<filename>")
def download(filename: str):
    from app.config import Config
    from pathlib import Path
    safe = (Config.EXPORT_PATH / filename).resolve()
    if not str(safe).startswith(str(Config.EXPORT_PATH.resolve())):
        return "Forbidden", 403
    if not safe.exists():
        return "Not found", 404
    return send_file(safe, as_attachment=True, download_name=filename)


# ─────────────────────────────────────────────────────────────── Helpers

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


def _invoice_summary(inv) -> dict:
    """Trả về subset field cần thiết cho bảng danh sách (nhẹ hơn to_dict)."""
    return {
        "id":               inv.id,
        "invoice_no":       inv.invoice_no,
        "invoice_symbol":   inv.invoice_symbol,
        "invoice_form":     inv.invoice_form,
        "invoice_category": inv.invoice_category,
        "issue_date":       inv.issue_date.strftime("%d/%m/%Y") if inv.issue_date else "",
        "seller_name":      inv.seller_name or "",
        "buyer_name":       inv.buyer_name  or "",
        "seller_tax_code":  inv.seller_tax_code or "",
        "buyer_tax_code":   inv.buyer_tax_code  or "",
        "amount":           inv.amount      or 0,
        "vat_amount":       inv.vat_amount  or 0,
        "total_amount":     inv.total_amount or 0,
        "created_at":       inv.created_at.strftime("%d/%m/%Y %H:%M") if inv.created_at else "",
        "snapshot_batch_id": inv.snapshot_batch_id,
    }