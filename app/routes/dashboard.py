"""Dashboard blueprint — overview statistics and charts."""
from __future__ import annotations

from flask import Blueprint, render_template

from app.db.repository import CrawlJobRepository
from app.services.invoice_service import InvoiceService

bp = Blueprint("dashboard", __name__, url_prefix="/")


@bp.get("/")
def index():
    stats = InvoiceService.get_stats()
    account_stats = InvoiceService.get_account_stats()

    return render_template(
        "dashboard.html",
        stats=stats,
        account_stats=account_stats
    )