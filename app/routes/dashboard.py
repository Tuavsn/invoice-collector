"""Dashboard blueprint — overview statistics and charts."""
from __future__ import annotations

from flask import Blueprint, render_template

from app.db.repository import CrawlJobRepository
from app.services.file_service import FileService
from app.services.invoice_service import InvoiceService

bp = Blueprint("dashboard", __name__, url_prefix="/")


@bp.get("/")
def index():
    stats = InvoiceService.get_stats()
    monthly = InvoiceService.get_monthly_summary()
    recent_jobs = CrawlJobRepository.get_recent(5)
    disk = FileService.disk_usage()
    dl_stats = FileService.get_download_stats()

    return render_template(
        "dashboard.html",
        stats=stats,
        monthly=monthly,
        recent_jobs=recent_jobs,
        disk=disk,
        dl_stats=dl_stats,
    )