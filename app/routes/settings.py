"""Settings blueprint — configure application parameters at runtime."""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from app.db.repository import SettingsRepository

bp = Blueprint("settings", __name__, url_prefix="/settings")

_DEFAULTS = {
    "crawler_max_retries": "5",
    "crawler_page_size": "50",
    "crawler_delay_ms": "500",
    "playwright_headless": "true",
    "playwright_timeout": "30000",
    "playwright_slow_mo": "100",
}


@bp.get("/")
def index():
    current = SettingsRepository.get_all()
    # Merge with defaults for any missing keys
    merged = {**_DEFAULTS, **current}
    return render_template("settings.html", settings=merged)


@bp.post("/api/save")
def api_save():
    data = request.get_json(force=True, silent=True) or {}
    allowed_keys = set(_DEFAULTS.keys())
    saved = []
    for key, value in data.items():
        if key in allowed_keys:
            SettingsRepository.set(key, str(value))
            saved.append(key)
    return jsonify({"ok": True, "saved": saved})


@bp.get("/api/all")
def api_all():
    current = SettingsRepository.get_all()
    return jsonify({**_DEFAULTS, **current})