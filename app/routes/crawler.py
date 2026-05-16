"""Crawler blueprint — UI and API for starting/stopping crawls."""
from __future__ import annotations

import threading

from flask import Blueprint, current_app, jsonify, render_template, request
from loguru import logger

from app.db.repository import CrawlJobRepository
from app.extensions import socketio
from app.services.crawler_service import get_crawl_status, start_crawl, stop_crawl

bp = Blueprint("crawler", __name__, url_prefix="/crawler")

# ── Captcha handshake (thread-safe) ──────────────────────────────────────────
# Dùng threading.Event vì Flask route là sync, còn crawler chạy trong thread
# riêng với asyncio event loop — chúng ta bridge qua threading.Event.
_captcha_lock   = threading.Lock()
_captcha_event  = threading.Event()
_captcha_answer: str = ""


def get_captcha_callbacks():
    """
    Trả về cặp (event, getter) để truyền vào CrawlerEngine.
    Gọi hàm này mỗi lần start crawl để reset state.
    """
    with _captcha_lock:
        _captcha_event.clear()
    return _captcha_event, lambda: _captcha_answer


# ─────────────────────────────────────────────────── UI

@bp.get("/")
def index():
    status = get_crawl_status()
    recent_jobs = CrawlJobRepository.get_recent(10)
    return render_template("crawler.html", status=status, recent_jobs=recent_jobs)


# ─────────────────────────────────────────────────── API

@bp.post("/api/start")
def api_start():
    data = request.get_json(force=True, silent=True) or {}

    username   = data.get("username",   "").strip()
    password   = data.get("password",   "").strip()
    start_date = data.get("start_date", "").strip()
    end_date   = data.get("end_date",   "").strip()

    if not all([username, password, start_date, end_date]):
        return jsonify({"ok": False, "error": "All fields are required."}), 400

    job = CrawlJobRepository.create(start_date=start_date, end_date=end_date)

    app = current_app._get_current_object()

    def emit_fn(msg: str) -> None:
        socketio.emit("crawler_log", {"message": msg, "job_id": job.id})

    def emit_captcha_fn(b64: str) -> None:
        socketio.emit("crawler_captcha", {"image": b64, "job_id": job.id})

    captcha_event, get_captcha_answer = get_captcha_callbacks()

    ok = start_crawl(
        job_id=job.id,
        username=username,
        password=password,
        start_date=start_date,
        end_date=end_date,
        emit_fn=emit_fn,
        emit_captcha_fn=emit_captcha_fn,
        captcha_event=captcha_event,
        get_captcha_answer=get_captcha_answer,
        app=app,
    )

    if not ok:
        CrawlJobRepository.update_status(
            job.id, "failed", error_message="Another crawl is running."
        )
        return jsonify({"ok": False, "error": "A crawl is already running."}), 409

    return jsonify({"ok": True, "job_id": job.id})


@bp.post("/api/stop")
def api_stop():
    stopped = stop_crawl()
    return jsonify({
        "ok": stopped,
        "message": "Stop requested." if stopped else "No active crawl.",
    })


@bp.get("/api/status")
def api_status():
    return jsonify(get_crawl_status())


@bp.get("/api/job/<int:job_id>")
def api_job_detail(job_id: int):
    job = CrawlJobRepository.get_by_id(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job.to_dict())


@bp.post("/api/captcha")
def api_captcha_submit():
    """Nhận captcha text từ user và giải phóng crawler đang chờ."""
    global _captcha_answer
    answer = (request.json or {}).get("answer", "").strip().upper()
    if not answer:
        return jsonify({"ok": False, "error": "Empty answer"}), 400

    with _captcha_lock:
        _captcha_answer = answer
        _captcha_event.set()

    logger.info("Captcha answer received from user: '{}'", answer)
    return jsonify({"ok": True})


@bp.post("/api/captcha/refresh")
def api_captcha_refresh():
    """Yêu cầu crawler chụp lại captcha mới và gửi lên UI."""
    socketio.emit("crawler_captcha_refresh", {})
    return jsonify({"ok": True})