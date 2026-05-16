"""Invoice list and detail blueprints."""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, render_template, request, send_file

from app.services.invoice_service import InvoiceService

bp = Blueprint("invoices", __name__, url_prefix="/invoices")


@bp.get("/")
def index():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    search = request.args.get("search", "").strip() or None
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None
    invoice_type = request.args.get("type", "").strip() or None

    pagination = InvoiceService.get_paginated(
        page=page,
        per_page=per_page,
        search=search,
        start_date=start_date,
        end_date=end_date,
        invoice_type=invoice_type,
    )

    return render_template(
        "invoices.html",
        pagination=pagination,
        search=search or "",
        start_date=start_date or "",
        end_date=end_date or "",
        invoice_type=invoice_type or "",
    )


@bp.get("/<int:invoice_id>")
def detail(invoice_id: int):
    detail = InvoiceService.get_detail(invoice_id)
    if not detail:
        abort(404)
    return render_template("invoice_detail.html", invoice=detail)


@bp.get("/api/list")
def api_list():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    search = request.args.get("search", "").strip() or None
    pagination = InvoiceService.get_paginated(page=page, per_page=per_page, search=search)
    return jsonify(
        {
            "items": [i.to_dict() for i in pagination.items],
            "total": pagination.total,
            "pages": pagination.pages,
            "page": pagination.page,
        }
    )


@bp.get("/api/<int:invoice_id>/download/<file_type>")
def api_download(invoice_id: int, file_type: str):
    """Serve invoice file (xml/pdf) for direct browser download."""
    from pathlib import Path

    detail = InvoiceService.get_detail(invoice_id)
    if not detail:
        abort(404)

    path_key = f"{file_type}_path"
    file_path = detail.get(path_key)
    if not file_path:
        abort(404)

    p = Path(file_path)
    if not p.exists():
        abort(404)

    return send_file(
        p,
        as_attachment=True,
        download_name=p.name,
    )