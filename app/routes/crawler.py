"""Crawler blueprint — UI and API for starting/stopping crawls."""
from __future__ import annotations

import json
import os
import threading
from calendar import monthrange
from datetime import date
from typing import List, Optional, Tuple

from flask import Blueprint, current_app, jsonify, render_template, request
from loguru import logger

from app.db.repository import CrawlJobRepository, GdtAccountRepository
from app.extensions import socketio
from app.services.crawler_service import (
    disable_auto_sync,
    enable_auto_sync,
    get_crawl_status,
    load_auto_sync_config,
    start_crawl,
    stop_crawl,
    _resolve_date_range,
)

bp = Blueprint("crawler", __name__, url_prefix="/crawler")

# ── Captcha state — keyed by account_id (None = legacy single-account) ───────
_captcha_states: dict = {}
_captcha_states_lock = threading.Lock()

# ── In-process auto-sync state — per account ──────────────────────────────────
_auto_sync_lock:        threading.Lock = threading.Lock()
_auto_sync_active:      dict = {}
_auto_sync_stop_events: dict = {}


# ── State helpers ─────────────────────────────────────────────────────────────

def _state_key(account_id: Optional[int]):
    return account_id if account_id is not None else "_legacy"


def _get_stop_event(account_id: Optional[int]) -> threading.Event:
    key = _state_key(account_id)
    with _auto_sync_lock:
        ev = _auto_sync_stop_events.get(key)
        if ev is None:
            ev = threading.Event()
            _auto_sync_stop_events[key] = ev
        return ev


def _set_auto_sync(active: bool, cfg: dict | None = None, account_id: Optional[int] = None) -> None:
    key = _state_key(account_id)
    with _auto_sync_lock:
        _auto_sync_active[key] = active
        if active:
            _auto_sync_stop_events.setdefault(key, threading.Event()).clear()
    socketio.emit("auto_sync_state", {
        "active":     active,
        "config":     cfg or {},
        "account_id": account_id,
    })
    logger.info("Auto-sync state (account_id={}) → {}", account_id, active)


def get_auto_sync_state(account_id: Optional[int] = None) -> bool:
    with _auto_sync_lock:
        if account_id is not None:
            return _auto_sync_active.get(_state_key(account_id), False)
        return any(_auto_sync_active.values())


def _stop_all_auto_sync_chains() -> None:
    with _auto_sync_lock:
        events = list(_auto_sync_stop_events.values())
    for ev in events:
        ev.set()


def _get_or_create_captcha_state(account_id, account_name: str = "") -> dict:
    with _captcha_states_lock:
        if account_id not in _captcha_states:
            _captcha_states[account_id] = {
                "lock":         threading.Lock(),
                "event":        threading.Event(),
                "answer":       "",
                "account_name": account_name,
            }
        return _captcha_states[account_id]


def get_captcha_callbacks(account_id=None, account_name: str = ""):
    state = _get_or_create_captcha_state(account_id, account_name)
    with state["lock"]:
        state["event"].clear()
    return state["event"], lambda: state["answer"]


# ── emit_fn factory ───────────────────────────────────────────────────────────

def _make_emit_fn(job_id: int, account_id: Optional[int]):
    """
    Trả về emit_fn thông minh:
      - Nếu message là JSON marker ``{"__progress__": true, ...}``
        → emit socket event ``crawler_progress`` (frontend dùng để tính %)
      - Còn lại → emit ``crawler_log`` như cũ
    """
    def _emit(msg: str) -> None:
        if msg and msg.startswith('{"__progress__"'):
            try:
                data = json.loads(msg)
                if data.get("__progress__"):
                    socketio.emit("crawler_progress", {
                        "total":      data["total"],
                        "done":       data["done"],
                        "failed":     data["failed"],
                        "skipped":    data.get("skipped", 0),
                        "job_id":     job_id,
                        "account_id": account_id,
                    })
                    return
            except (json.JSONDecodeError, KeyError):
                pass
        socketio.emit("crawler_log", {
            "message":    msg,
            "job_id":     job_id,
            "account_id": account_id,
        })
    return _emit


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_credentials() -> Tuple[str, str]:
    username = os.environ.get("GDT_USERNAME", "").strip()
    password = os.environ.get("GDT_PASSWORD", "").strip()
    return username, password


def _split_into_chunks(start_date: date, end_date: date) -> List[Tuple[str, str]]:
    chunks: List[Tuple[str, str]] = []
    cursor = start_date
    while cursor <= end_date:
        month_end = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
        chunk_end = min(month_end, end_date)
        chunks.append((cursor.strftime("%d/%m/%Y"), chunk_end.strftime("%d/%m/%Y")))
        if chunk_end.month == 12:
            cursor = date(chunk_end.year + 1, 1, 1)
        else:
            cursor = date(chunk_end.year, chunk_end.month + 1, 1)
    return chunks


def _range_from_months_back(months_back: int) -> Tuple[date, date]:
    today = date.today()
    y, m = today.year, today.month
    for _ in range(months_back):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return date(y, m, 1), today


def _compute_auto_sync_chunks(months_back: int = 2) -> List[Tuple[str, str]]:
    start, end = _range_from_months_back(months_back)
    return _split_into_chunks(start, end)


def _parse_date_param(s: str) -> date:
    return date.fromisoformat(s)


def _chunks_from_iso_range(start_iso: str, end_iso: str) -> List[Tuple[str, str]]:
    return _split_into_chunks(
        date.fromisoformat(start_iso),
        date.fromisoformat(end_iso),
    )


# ─────────────────────────────────────────────────────────────────────── UI ──

@bp.get("/")
def index():
    status        = get_crawl_status()
    recent_jobs   = CrawlJobRepository.get_recent(10)
    has_creds     = all(_get_credentials())
    auto_sync_cfg = load_auto_sync_config()
    accounts      = GdtAccountRepository.get_all(active_only=False)
    return render_template(
        "crawler.html",
        status=status,
        recent_jobs=recent_jobs,
        has_creds=has_creds,
        accounts=accounts,
        auto_sync_active=auto_sync_cfg.get("enabled", False),
        auto_sync_config=auto_sync_cfg if auto_sync_cfg.get("enabled") else None,
    )


# ─────────────────────────────────────────────────────────────────────── API ──

def _launch_chunks(
    chunks: List[Tuple[str, str]],
    app,
    *,
    account_id: Optional[int] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    is_auto_sync: bool = False,
    auto_sync_cfg: dict | None = None,
) -> dict:
    # Resolve credentials
    _username     = username
    _password     = password
    _account_name = ""
    if not _username and account_id is not None:
        try:
            with app.app_context():
                from app.db.repository import GdtAccountRepository as _GAR
                _acct = _GAR.get_by_id(account_id)
                if _acct:
                    _username     = _acct.username
                    _password     = _acct.password
                    _account_name = _acct.name
        except Exception as _e:
            logger.warning("Could not resolve credentials for account_id={}: {}", account_id, _e)
    if not _username:
        _username, _password = _get_credentials()

    if not _username or not _password:
        return {
            "ok": False,
            "error": "GDT credentials not set (check account config or GDT_USERNAME/GDT_PASSWORD env).",
        }
    if not chunks:
        return {"ok": False, "error": "No date chunks to process."}

    def _run_chain():
        import time

        stop_event = _get_stop_event(account_id) if is_auto_sync else None

        if is_auto_sync:
            _set_auto_sync(True, cfg=auto_sync_cfg, account_id=account_id)

        try:
            # Wait for the engine to be free
            while True:
                if is_auto_sync and stop_event.is_set():
                    break

                with app.app_context():
                    status = get_crawl_status()

                if not status["is_running"]:
                    break

                time.sleep(3)

            if is_auto_sync and stop_event.is_set():
                logger.info(
                    "Auto-sync (account_id={}) cancelled while waiting for slot.",
                    account_id,
                )
                return

            with app.app_context():
                job = CrawlJobRepository.create(
                    start_date=chunks[0][0],
                    end_date=chunks[-1][1],
                    account_id=account_id,
                )

            emit_fn = _make_emit_fn(job.id, account_id)

            def emit_captcha_fn(
                b64,
                _jid=job.id,
                _aid=account_id,
                _aname=_account_name,
            ):
                socketio.emit(
                    "crawler_captcha",
                    {
                        "image": b64,
                        "job_id": _jid,
                        "account_id": _aid,
                        "account_name": _aname,
                    },
                )

            captcha_event, get_captcha_answer = get_captcha_callbacks(
                account_id=account_id,
                account_name=_account_name,
            )

            ok = start_crawl(
                job_id=job.id,
                username=_username,
                password=_password,
                chunks=chunks,      # truyền toàn bộ 6 tháng 1 lần
                account_id=account_id,
                emit_fn=emit_fn,
                emit_captcha_fn=emit_captcha_fn,
                captcha_event=captcha_event,
                get_captcha_answer=get_captcha_answer,
                app=app,
            )

            if not ok:
                logger.warning(
                    "Could not start crawl: {} → {}",
                    chunks[0][0],
                    chunks[-1][1],
                )
            else:
                logger.info(
                    "Started crawl: {} → {}",
                    chunks[0][0],
                    chunks[-1][1],
                )

        finally:
            if is_auto_sync:
                _set_auto_sync(False, cfg=load_auto_sync_config(), account_id=account_id)

    t = threading.Thread(
        target=_run_chain,
        daemon=True,
        name=f"auto-sync-chain-{account_id}" if is_auto_sync else "chunk-chain",
    )
    t.start()
    return {"ok": True, "chunks": [{"start": s, "end": e} for s, e in chunks]}


# ── POST /crawler/api/start ───────────────────────────────────────────────────

@bp.post("/api/start")
def api_start():
    data = request.get_json(force=True, silent=True) or {}
    app  = current_app._get_current_object()
    mode = data.get("mode", "range")

    AUTO_SYNC_MODES = {"2m", "6m", "1y", "custom_date"}

    # ── Auto-sync modes ───────────────────────────────────────────────────────
    if mode in AUTO_SYNC_MODES:
        if mode == "custom_date":
            try:
                start_d = _parse_date_param(data["start_date"])
                end_d   = _parse_date_param(data["end_date"])
            except (KeyError, ValueError):
                return jsonify(
                    {"ok": False, "error": "Invalid start_date / end_date (YYYY-MM-DD)."}
                ), 400
            if start_d > end_d:
                return jsonify(
                    {"ok": False, "error": "start_date must be ≤ end_date."}
                ), 400
            start_iso, end_iso = data["start_date"], data["end_date"]
        else:
            start_iso, end_iso = _resolve_date_range({"preset": mode})

        try:
            run_hour   = max(0, min(23, int(data.get("run_hour",   19))))
            run_minute = max(0, min(59, int(data.get("run_minute",  0))))
        except (TypeError, ValueError):
            run_hour, run_minute = 19, 0

        account_schedules = data.get("account_schedules")
        if not account_schedules:
            account_ids = data.get("account_ids") or []
            if account_ids:
                account_schedules = [
                    {"account_id": aid, "run_hour": run_hour, "run_minute": run_minute}
                    for aid in account_ids
                ]
            else:
                account_schedules = [
                    {"account_id": None, "run_hour": run_hour, "run_minute": run_minute}
                ]

        busy = [
            s.get("account_id") for s in account_schedules
            if get_auto_sync_state(s.get("account_id"))
        ]
        if busy:
            return jsonify({
                "ok": False,
                "error": f"Auto-sync đang chạy cho account_id(s): {busy}",
            }), 409

        saved_cfg = enable_auto_sync(
            preset=mode,
            start_date=start_iso if mode == "custom_date" else None,
            end_date=end_iso     if mode == "custom_date" else None,
            account_schedules=account_schedules,
            app=app,
        )

        socketio.emit("auto_sync_state", {"active": False, "config": saved_cfg})
        result = {"ok": True, "chunks": [], "scheduled_only": True}

    # ── Manual range mode ─────────────────────────────────────────────────────
    else:
        if get_auto_sync_state():
            _stop_all_auto_sync_chains()
            stop_crawl()
            logger.info("Auto-sync cancelled by manual range crawl.")

        disable_auto_sync()
        socketio.emit("auto_sync_state", {"active": False, "config": None})

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

        start_d = date(start_year, start_month, 1)
        last    = monthrange(end_year, end_month)[1]
        end_d   = date(end_year, end_month, last)
        chunks  = _split_into_chunks(start_d, end_d)
        account_ids = data.get("account_ids") or []
        if not account_ids:
            account_ids = [None]

        results = []
        for aid in account_ids:
            r = _launch_chunks(chunks, app, account_id=aid, is_auto_sync=False)
            results.append(r)
        failed = [r for r in results if not r.get("ok")]
        result = results[0] if results else {"ok": False, "error": "No accounts."}
        if failed and len(failed) == len(results):
            result = failed[0]

    if not result["ok"]:
        return jsonify(result), 400

    if result.get("scheduled_only"):
        sched_lines = ", ".join(
            f"#{s['account_id'] if s['account_id'] is not None else '-'} @ {s['run_hour']:02d}:{s['run_minute']:02d}"
            for s in saved_cfg["account_schedules"]
        )
        return jsonify({
            **result,
            "total_chunks": 0,
            "mode": mode,
            "message": f"Auto-sync đã được lên lịch ({sched_lines} ICT). Sẽ tự chạy đúng giờ.",
        })

    return jsonify({**result, "total_chunks": len(result["chunks"]), "mode": mode})


# ── POST /crawler/api/stop ────────────────────────────────────────────────────

@bp.post("/api/stop")
def api_stop():
    auto_was_active = get_auto_sync_state()
    if auto_was_active:
        _stop_all_auto_sync_chains()

    aid_raw = (request.get_json(force=True, silent=True) or {}).get("account_id")
    account_id_stop = int(aid_raw) if aid_raw is not None else None
    stopped = stop_crawl(account_id=account_id_stop)

    persisted_cfg = load_auto_sync_config()
    if persisted_cfg.get("enabled"):
        disable_auto_sync()
        socketio.emit("auto_sync_state", {"active": False, "config": None})
        logger.info("Auto-sync scheduler disabled via /api/stop.")

    return jsonify({
        "ok": stopped or auto_was_active,
        "message": (
            "Auto-sync cancelled."
            if auto_was_active
            else ("Stop requested." if stopped else "No active crawl.")
        ),
        "auto_sync_cancelled": auto_was_active,
    })


# ── POST /crawler/api/auto-sync/disable ──────────────────────────────────────

@bp.post("/api/auto-sync/disable")
def api_disable_auto_sync():
    if get_auto_sync_state():
        _stop_all_auto_sync_chains()
    disable_auto_sync()
    socketio.emit("auto_sync_state", {"active": False, "config": None})
    logger.info("Auto-sync disabled via /api/auto-sync/disable.")
    return jsonify({"ok": True})


# ── GET /crawler/api/status ───────────────────────────────────────────────────

@bp.get("/api/status")
def api_status():
    status = get_crawl_status()
    status["auto_sync_active"] = (
        get_auto_sync_state() or status.get("auto_sync_active", False)
    )
    return jsonify(status)


# ── GET /crawler/api/job/<id> ─────────────────────────────────────────────────

@bp.get("/api/job/<int:job_id>")
def api_job_detail(job_id: int):
    job = CrawlJobRepository.get_by_id(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job.to_dict())


# ── POST /crawler/api/captcha ─────────────────────────────────────────────────

@bp.post("/api/captcha")
def api_captcha_submit():
    data   = request.json or {}
    answer = data.get("answer", "").strip().upper()
    if not answer:
        return jsonify({"ok": False, "error": "Empty answer"}), 400

    aid_raw    = data.get("account_id")
    account_id = int(aid_raw) if aid_raw is not None else None

    with _captcha_states_lock:
        state = _captcha_states.get(account_id)

    if state is None:
        with _captcha_states_lock:
            states = list(_captcha_states.values())
        state = next((s for s in states if not s["event"].is_set()), None)

    if state is None:
        return jsonify({"ok": False, "error": "No waiting captcha found"}), 404

    with state["lock"]:
        state["answer"] = answer
        state["event"].set()

    logger.info("Captcha answer received for account_id={}: '{}'", account_id, answer)
    return jsonify({"ok": True})


@bp.post("/api/captcha/refresh")
def api_captcha_refresh():
    data       = request.get_json(force=True, silent=True) or {}
    account_id = data.get("account_id")
    socketio.emit("crawler_captcha_refresh", {"account_id": account_id})
    return jsonify({"ok": True})


# ── GET /crawler/api/chunks-preview ──────────────────────────────────────────

@bp.get("/api/chunks-preview")
def api_chunks_preview():
    # Convention 1: explicit ISO date range
    sd_raw = request.args.get("start_date")
    ed_raw = request.args.get("end_date")
    if sd_raw and ed_raw:
        try:
            start_d = _parse_date_param(sd_raw)
            end_d   = _parse_date_param(ed_raw)
        except ValueError:
            return jsonify({"error": "Invalid date params"}), 400
        chunks = _split_into_chunks(start_d, end_d)
        return jsonify({"chunks": [{"start": s, "end": e} for s, e in chunks]})

    # Convention 2: months_back preset
    mb_raw = request.args.get("months_back")
    if mb_raw:
        try:
            months_back = int(mb_raw)
        except ValueError:
            return jsonify({"error": "Invalid months_back"}), 400
        chunks  = _compute_auto_sync_chunks(months_back=months_back)
        start_d, end_d = _range_from_months_back(months_back)
        return jsonify({
            "chunks":      [{"start": s, "end": e} for s, e in chunks],
            "range_start": start_d.strftime("%d/%m/%Y"),
            "range_end":   end_d.strftime("%d/%m/%Y"),
        })

    # Convention 3: legacy month/year selectors
    try:
        start_month = int(request.args.get("start_month", 0))
        start_year  = int(request.args.get("start_year",  0))
        end_month   = int(request.args.get("end_month",   0))
        end_year    = int(request.args.get("end_year",    0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid params"}), 400

    if not all([start_month, start_year, end_month, end_year]):
        return jsonify({"chunks": []})

    start_d = date(start_year, start_month, 1)
    last    = monthrange(end_year, end_month)[1]
    end_d   = date(end_year, end_month, last)
    chunks  = _split_into_chunks(start_d, end_d)
    return jsonify({"chunks": [{"start": s, "end": e} for s, e in chunks]})


# ─────────────────────────────────────────────────────── GDT Accounts API ────

@bp.get("/api/accounts")
def api_accounts_list():
    accounts = GdtAccountRepository.get_all()
    return jsonify([a.to_dict() for a in accounts])


@bp.post("/api/accounts")
def api_accounts_create():
    data = request.get_json(force=True, silent=True) or {}
    name     = (data.get("name")     or "").strip()
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not name or not username or not password:
        return jsonify({"ok": False, "error": "name, username, password required"}), 400
    try:
        acct = GdtAccountRepository.create(
            name=name, username=username, password=password,
            tax_code=(data.get("tax_code") or "").strip() or None,
            note=(data.get("note") or "").strip() or None,
            company_name=(data.get("company_name") or "").strip() or None,
            company_tax_code=(data.get("company_tax_code") or "").strip() or None,
            company_address=(data.get("company_address") or "").strip() or None,
            company_report_title=(data.get("company_report_title") or "").strip() or None,
        )
        return jsonify({"ok": True, "account": acct.to_dict()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp.patch("/api/accounts/<int:account_id>")
def api_accounts_update(account_id: int):
    data = request.get_json(force=True, silent=True) or {}
    allowed = {"name", "username", "password", "tax_code", "note", "is_active", "company_name", "company_tax_code", "company_address", "company_report_title"}
    kwargs  = {k: v for k, v in data.items() if k in allowed}
    acct = GdtAccountRepository.update(account_id, **kwargs)
    if not acct:
        return jsonify({"ok": False, "error": "Account not found"}), 404
    return jsonify({"ok": True, "account": acct.to_dict()})


@bp.delete("/api/accounts/<int:account_id>")
def api_accounts_delete(account_id: int):
    ok = GdtAccountRepository.delete(account_id)
    if not ok:
        return jsonify({"ok": False, "error": "Account not found"}), 404
    return jsonify({"ok": True})