"""Invoice list and detail blueprints."""
from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request, send_file

from app.services.invoice_service import InvoiceService

bp = Blueprint("invoices", __name__, url_prefix="/invoices")


# ─────────────────────────────────────────────────────────────────────── UI

@bp.get("/")
def index():
    page           = request.args.get("page",          1,   type=int)
    per_page       = request.args.get("per_page",      20,  type=int)
    search         = request.args.get("search",        "").strip() or None
    start_date     = request.args.get("start_date",    "").strip() or None
    end_date       = request.args.get("end_date",      "").strip() or None
    ghi_chu        = request.args.get("ghi_chu",       "").strip() or None
    thang_ke_khai  = request.args.get("thang_ke_khai", "").strip() or None

    pagination = InvoiceService.get_paginated(
        page=page,
        per_page=per_page,
        search=search,
        start_date=start_date,
        end_date=end_date,
        ghi_chu=ghi_chu,
        thang_ke_khai=thang_ke_khai,
    )

    return render_template(
        "invoices.html",
        pagination=pagination,
        search=search             or "",
        start_date=start_date     or "",
        end_date=end_date         or "",
        ghi_chu=ghi_chu           or "",
        thang_ke_khai=thang_ke_khai or "",
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


# ─────────────────────────────────────────────────────────────────── JSON API

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


# ──────────────────────────────────────────── Extra fields PATCH endpoint

_EXTRA_FIELDS = {
    "mat_hang", "thang_ke_khai", "payment_note", "bank_name",
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


# ──────────────────────────────────────────────────────────────── File download

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


# ─────────────────────────────────────────────────────────────── PDF generation

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