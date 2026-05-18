"""Export blueprint — generate and download Excel reports."""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request, send_file

from app.services.excel_service import ExcelService

bp = Blueprint("export", __name__, url_prefix="/export")


@bp.get("/")
def index():
    return render_template("export.html")


@bp.post("/api/excel")
def api_excel():
    data = request.get_json(force=True, silent=True) or {}

    start_date    = (data.get("start_date")    or "").strip() or None
    end_date      = (data.get("end_date")      or "").strip() or None
    ghi_chu       = (data.get("ghi_chu")       or "").strip() or None
    thang_ke_khai = (data.get("thang_ke_khai") or "").strip() or None
    search        = (data.get("search")        or "").strip() or None

    try:
        path = ExcelService.export_vat_summary(
            start_date=start_date,
            end_date=end_date,
            ghi_chu=ghi_chu,
            thang_ke_khai=thang_ke_khai,
            search=search,
        )
        return jsonify({"ok": True, "file": path.name, "path": str(path)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.get("/download/<filename>")
def download(filename: str):
    from app.config import Config
    from pathlib import Path

    safe_path = (Config.EXPORT_PATH / filename).resolve()
    if not str(safe_path).startswith(str(Config.EXPORT_PATH.resolve())):
        return "Forbidden", 403
    if not safe_path.exists():
        return "Not found", 404

    return send_file(safe_path, as_attachment=True, download_name=filename)