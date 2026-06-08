"""Settings blueprint — configure application parameters at runtime."""
from __future__ import annotations

import os
from datetime import date

from flask import Blueprint, jsonify, render_template, request

from app.db.repository import SettingsRepository

bp = Blueprint("settings", __name__, url_prefix="/settings")

_DEFAULTS = {
    # ── Crawler
    "crawler_max_retries": "5",
    "crawler_page_size":   "50",
    "crawler_delay_ms":    "500",
    # ── Playwright
    "playwright_headless": "true",
    "playwright_timeout":  "30000",
    "playwright_slow_mo":  "100",
}

_ALLOWED_KEYS = set(_DEFAULTS.keys())


@bp.get("/")
def index():
    current = SettingsRepository.get_all()
    merged  = {**_DEFAULTS, **current}

    gdt_username = os.environ.get("GDT_USERNAME", "").strip()
    gdt_password = os.environ.get("GDT_PASSWORD", "").strip()
    cred_status  = {
        "username_set": bool(gdt_username),
        "password_set": bool(gdt_password),
        "username_hint": (gdt_username[:2] + "***") if gdt_username else "",
    }

    today = date.today()
    from app.db.repository import GdtAccountRepository
    accounts = GdtAccountRepository.get_all()
    return render_template(
        "settings.html",
        settings=merged,
        cred_status=cred_status,
        accounts=accounts,
        now_year=today.year,
        now_month=today.month,
    )


@bp.post("/api/save")
def api_save():
    data  = request.get_json(force=True, silent=True) or {}
    saved = []
    for key, value in data.items():
        if key in _ALLOWED_KEYS:
            SettingsRepository.set(key, str(value))
            saved.append(key)
    return jsonify({"ok": True, "saved": saved})


@bp.get("/api/all")
def api_all():
    current = SettingsRepository.get_all()
    return jsonify({**_DEFAULTS, **current})

# ── GDT Accounts API (also on settings page for convenience) ──────────────

@bp.get("/api/accounts")
def api_settings_accounts():
    from app.db.repository import GdtAccountRepository
    return __import__('flask').jsonify([a.to_dict() for a in GdtAccountRepository.get_all()])