"""Settings blueprint — configure application parameters at runtime."""
from __future__ import annotations

import os
from datetime import date

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
    merged  = {**_DEFAULTS, **current}

    # Credential status (never expose the actual values to the template)
    gdt_username = os.environ.get("GDT_USERNAME", "").strip()
    gdt_password = os.environ.get("GDT_PASSWORD", "").strip()
    cred_status  = {
        "username_set": bool(gdt_username),
        "password_set": bool(gdt_password),
        "username_hint": (gdt_username[:2] + "***") if gdt_username else "",
    }

    today = date.today()
    return render_template(
        "settings.html",
        settings=merged,
        cred_status=cred_status,
        now_year=today.year,
        now_month=today.month,
    )


@bp.post("/api/save")
def api_save():
    data        = request.get_json(force=True, silent=True) or {}
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