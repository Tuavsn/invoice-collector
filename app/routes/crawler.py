"""Crawler blueprint — UI and API for starting/stopping crawls."""
from __future__ import annotations

import os
import threading
from calendar import monthrange
from datetime import date, timedelta
from typing import List, Tuple

from flask import Blueprint, current_app, jsonify, render_template, request
from loguru import logger

from app.db.repository import CrawlJobRepository
from app.extensions import socketio
from app.services.crawler_service import get_crawl_status, start_crawl, stop_crawl

bp = Blueprint("crawler", __name__, url_prefix="/crawler")

# ── Captcha handshake (thread-safe) ──────────────────────────────────────────
_captcha_lock   = threading.Lock()
_captcha_event  = threading.Event()
_captcha_answer: str = ""

# ── Auto-sync state ───────────────────────────────────────────────────────────
_auto_sync_lock:   threading.Lock  = threading.Lock()
_auto_sync_active: bool            = False
_auto_sync_stop:   threading.Event = threading.Event()


def _set_auto_sync(active: bool) -> None:
    global _auto_sync_active
    with _auto_sync_lock:
        _auto_sync_active = active
        if not active:
            _auto_sync_stop.clear()
    socketio.emit("auto_sync_state", {"active": active})
    logger.info("Auto-sync state → {}", active)


def get_auto_sync_state() -> bool:
    with _auto_sync_lock:
        return _auto_sync_active


def get_captcha_callbacks():
    with _captcha_lock:
        _captcha_event.clear()
    return _captcha_event, lambda: _captcha_answer


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_credentials() -> Tuple[str, str]:
    username = os.environ.get("GDT_USERNAME", "").strip()
    password = os.environ.get("GDT_PASSWORD", "").strip()
    return username, password


def _split_into_chunks(
    start_month: int, start_year: int,
    end_month: int,   end_year: int,
    max_days: int = 31,
) -> List[Tuple[str, str]]:
    range_start = date(start_year, start_month, 1)
    last_day    = monthrange(end_year, end_month)[1]
    range_end   = date(end_year, end_month, last_day)
    chunks: List[Tuple[str, str]] = []
    cursor = range_start
    while cursor <= range_end:
        chunk_end = min(cursor + timedelta(days=max_days - 1), range_end)
        chunks.append((cursor.strftime("%d/%m/%Y"), chunk_end.strftime("%d/%m/%Y")))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _compute_auto_sync_chunks() -> List[Tuple[str, str]]:
    today   = date.today()
    first_m = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    return _split_into_chunks(first_m.month, first_m.year, today.month, today.year)


# ─────────────────────────────────────────────────────────────────────── UI

@bp.get("/")
def index():
    status      = get_crawl_status()
    recent_jobs = CrawlJobRepository.get_recent(10)
    has_creds   = all(_get_credentials())
    return render_template(
        "crawler.html",
        status=status,
        recent_jobs=recent_jobs,
        has_creds=has_creds,
        auto_sync_active=get_auto_sync_state(),
    )


# ─────────────────────────────────────────────────────────────────────── API

def _launch_chunks(chunks: List[Tuple[str, str]], app, *, is_auto_sync: bool = False) -> dict:
    username, password = _get_credentials()
    if not username or not password:
        return {"ok": False, "error": "GDT credentials not set in environment (GDT_USERNAME / GDT_PASSWORD)."}
    if not chunks:
        return {"ok": False, "error": "No date chunks to process."}

    def _run_chain():
        import time
        if is_auto_sync:
            _set_auto_sync(True)
        try:
            for i, (sd, ed) in enumerate(chunks):
                if is_auto_sync and _auto_sync_stop.is_set():
                    logger.info("Auto-sync cancelled before chunk {}", i + 1)
                    break

                # Wait for free slot
                while True:
                    if is_auto_sync and _auto_sync_stop.is_set():
                        break
                    with app.app_context():
                        status = get_crawl_status()
                    if not status["is_running"]:
                        break
                    time.sleep(3)

                if is_auto_sync and _auto_sync_stop.is_set():
                    logger.info("Auto-sync cancelled while waiting for slot.")
                    break

                with app.app_context():
                    job = CrawlJobRepository.create(start_date=sd, end_date=ed)

                def emit_fn(msg, _jid=job.id):
                    socketio.emit("crawler_log", {"message": msg, "job_id": _jid})

                def emit_captcha_fn(b64, _jid=job.id):
                    socketio.emit("crawler_captcha", {"image": b64, "job_id": _jid})

                captcha_event, get_captcha_answer = get_captcha_callbacks()

                ok = start_crawl(
                    job_id=job.id,
                    username=username,
                    password=password,
                    start_date=sd,
                    end_date=ed,
                    emit_fn=emit_fn,
                    emit_captcha_fn=emit_captcha_fn,
                    captcha_event=captcha_event,
                    get_captcha_answer=get_captcha_answer,
                    app=app,
                )
                if not ok:
                    logger.warning("Could not start chunk {}/{}: {} → {}", i + 1, len(chunks), sd, ed)
                else:
                    logger.info("Chunk {}/{} started: {} → {}", i + 1, len(chunks), sd, ed)

                time.sleep(5)
        finally:
            if is_auto_sync:
                _set_auto_sync(False)

    t = threading.Thread(
        target=_run_chain,
        daemon=True,
        name="auto-sync-chain" if is_auto_sync else "chunk-chain",
    )
    t.start()
    return {"ok": True, "chunks": [{"start": s, "end": e} for s, e in chunks]}


@bp.post("/api/start")
def api_start():
    data = request.get_json(force=True, silent=True) or {}
    app  = current_app._get_current_object()
    mode = data.get("mode", "range")

    if mode == "auto_sync":
        if get_auto_sync_state():
            return jsonify({"ok": False, "error": "Auto-sync is already running."}), 409
        chunks = _compute_auto_sync_chunks()
        result = _launch_chunks(chunks, app, is_auto_sync=True)

    else:
        # Manual range — cancel any running auto-sync first
        if get_auto_sync_state():
            _auto_sync_stop.set()
            stop_crawl()
            logger.info("Auto-sync cancelled by manual range crawl.")

        try:
            start_month = int(data.get("start_month", 0))
            start_year  = int(data.get("start_year",  0))
            end_month   = int(data.get("end_month",   0))
            end_year    = int(data.get("end_year",    0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid month/year values."}), 400

        if not all([start_month, start_year, end_month, end_year]):
            return jsonify({"ok": False, "error": "All month/year fields are required."}), 400

        if date(start_year, start_month, 1) > date(end_year, end_month, 1):
            return jsonify({"ok": False, "error": "Start must be before or equal to end."}), 400

        chunks = _split_into_chunks(start_month, start_year, end_month, end_year)
        result = _launch_chunks(chunks, app, is_auto_sync=False)

    if not result["ok"]:
        return jsonify(result), 400

    return jsonify({**result, "total_chunks": len(result["chunks"]), "mode": mode})


@bp.post("/api/stop")
def api_stop():
    auto_was_active = get_auto_sync_state()
    if auto_was_active:
        _auto_sync_stop.set()
        _set_auto_sync(False)

    stopped = stop_crawl()
    return jsonify({
        "ok":                  stopped or auto_was_active,
        "message":             "Auto-sync cancelled." if auto_was_active else ("Stop requested." if stopped else "No active crawl."),
        "auto_sync_cancelled": auto_was_active,
    })


@bp.get("/api/status")
def api_status():
    status = get_crawl_status()
    status["auto_sync_active"] = get_auto_sync_state()
    return jsonify(status)


@bp.get("/api/job/<int:job_id>")
def api_job_detail(job_id: int):
    job = CrawlJobRepository.get_by_id(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job.to_dict())


@bp.post("/api/captcha")
def api_captcha_submit():
    global _captcha_answer
    answer = (request.json or {}).get("answer", "").strip().upper()
    if not answer:
        return jsonify({"ok": False, "error": "Empty answer"}), 400
    with _captcha_lock:
        _captcha_answer = answer
        _captcha_event.set()
    logger.info("Captcha answer received: '{}'", answer)
    return jsonify({"ok": True})


@bp.post("/api/captcha/refresh")
def api_captcha_refresh():
    socketio.emit("crawler_captcha_refresh", {})
    return jsonify({"ok": True})


@bp.get("/api/chunks-preview")
def api_chunks_preview():
    try:
        start_month = int(request.args.get("start_month", 0))
        start_year  = int(request.args.get("start_year",  0))
        end_month   = int(request.args.get("end_month",   0))
        end_year    = int(request.args.get("end_year",    0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid params"}), 400

    if not all([start_month, start_year, end_month, end_year]):
        return jsonify({"chunks": []})

    chunks = _split_into_chunks(start_month, start_year, end_month, end_year)
    return jsonify({"chunks": [{"start": s, "end": e} for s, e in chunks]})