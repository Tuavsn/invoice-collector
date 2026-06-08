"""Crawler blueprint — UI and API for starting/stopping crawls."""
from __future__ import annotations

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
# Each entry: { "lock": Lock, "event": Event, "answer": str, "account_name": str }
_captcha_states: dict = {}
_captcha_states_lock = threading.Lock()  # guards the dict itself

# ── In-process auto-sync state ────────────────────────────────────────────────
# Tracks whether THIS process is actively running a chunk-chain thread right now.
# Distinct from the persisted scheduler config: the scheduler fires once a day
# automatically, while this flag reflects the live thread looping through chunks.
_auto_sync_lock:   threading.Lock  = threading.Lock()
_auto_sync_active: bool            = False
_auto_sync_stop:   threading.Event = threading.Event()


# ── State helpers ─────────────────────────────────────────────────────────────

def _set_auto_sync(active: bool, cfg: dict | None = None) -> None:
    """
    Update the in-process flag and broadcast the new state to all connected clients.

    ``cfg`` is forwarded in the socket payload so the UI can display the preset
    and next-run time without issuing a separate /api/status poll.
    """
    global _auto_sync_active
    with _auto_sync_lock:
        _auto_sync_active = active
        if not active:
            _auto_sync_stop.clear()
    socketio.emit("auto_sync_state", {"active": active, "config": cfg or {}})
    logger.info("Auto-sync state → {}", active)


def get_auto_sync_state() -> bool:
    with _auto_sync_lock:
        return _auto_sync_active


def _get_or_create_captcha_state(account_id, account_name: str = "") -> dict:
    """Return (or create) the captcha state dict for a given account_id."""
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
    """Return (event, get_answer_fn) for a specific account."""
    state = _get_or_create_captcha_state(account_id, account_name)
    with state["lock"]:
        state["event"].clear()
    return state["event"], lambda: state["answer"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_credentials() -> Tuple[str, str]:
    username = os.environ.get("GDT_USERNAME", "").strip()
    password = os.environ.get("GDT_PASSWORD", "").strip()
    return username, password


def _split_into_chunks(start_date: date, end_date: date) -> List[Tuple[str, str]]:
    """
    Split [start_date, end_date] into per-calendar-month chunks.
    Each chunk spans exactly one calendar month so day counts are always correct.
    Returns list of (start_str, end_str) in DD/MM/YYYY format.
    """
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
    """Return (start_date, today) going back ``months_back`` full calendar months."""
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
    """Parse YYYY-MM-DD from an API query/body parameter."""
    return date.fromisoformat(s)


def _chunks_from_iso_range(start_iso: str, end_iso: str) -> List[Tuple[str, str]]:
    """Build month-chunks from ISO-date strings (output of ``_resolve_date_range``)."""
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
    """
    Spin up a daemon thread that runs each chunk sequentially, waiting for the
    crawler engine to finish before starting the next one.

    Parameters
    ----------
    chunks:
        List of (start_dd/mm/yyyy, end_dd/mm/yyyy) tuples.
    app:
        The Flask application object (for app_context inside the thread).
    is_auto_sync:
        When True the thread manages the in-process _auto_sync_active flag and
        respects _auto_sync_stop so the chain can be cancelled mid-flight.
    auto_sync_cfg:
        The saved config dict emitted to clients alongside state changes.
        Keeps socket payloads consistent without extra /api/status polls.
    """
    # Resolve credentials: explicit params > account DB > env vars
    _username = username
    _password = password
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

        if is_auto_sync:
            _set_auto_sync(True, cfg=auto_sync_cfg)

        try:
            for i, (sd, ed) in enumerate(chunks):
                if is_auto_sync and _auto_sync_stop.is_set():
                    logger.info("Auto-sync cancelled before chunk {}", i + 1)
                    break

                # ── Wait for the engine to be free ────────────────────────────
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

                # ── Create DB job record ──────────────────────────────────────
                with app.app_context():
                    job = CrawlJobRepository.create(start_date=sd, end_date=ed, account_id=account_id)

                def emit_fn(msg, _jid=job.id):
                    socketio.emit("crawler_log", {"message": msg, "job_id": _jid})

                def emit_captcha_fn(b64, _jid=job.id, _aid=account_id, _aname=_account_name):
                    socketio.emit("crawler_captcha", {
                        "image":        b64,
                        "job_id":       _jid,
                        "account_id":   _aid,
                        "account_name": _aname,
                    })

                captcha_event, get_captcha_answer = get_captcha_callbacks(
                    account_id=account_id, account_name=_account_name
                )

                # ── Launch the async engine ───────────────────────────────────
                ok = start_crawl(
                    job_id=job.id,
                    username=_username,
                    password=_password,
                    start_date=sd,
                    end_date=ed,
                    account_id=account_id,
                    emit_fn=emit_fn,
                    emit_captcha_fn=emit_captcha_fn,
                    captcha_event=captcha_event,
                    get_captcha_answer=get_captcha_answer,
                    app=app,
                )
                if not ok:
                    logger.warning(
                        "Could not start chunk {}/{}: {} → {}", i + 1, len(chunks), sd, ed
                    )
                else:
                    logger.info(
                        "Chunk {}/{} started: {} → {}", i + 1, len(chunks), sd, ed
                    )

                time.sleep(5)

        finally:
            if is_auto_sync:
                # Chain finished (all chunks done or cancelled).
                # Emit disabled state but keep the scheduler config intact on
                # disk — the daily 21:19 APScheduler job survives process state.
                _set_auto_sync(False, cfg=load_auto_sync_config())

    t = threading.Thread(
        target=_run_chain,
        daemon=True,
        name="auto-sync-chain" if is_auto_sync else "chunk-chain",
    )
    t.start()
    return {"ok": True, "chunks": [{"start": s, "end": e} for s, e in chunks]}


# ── POST /crawler/api/start ───────────────────────────────────────────────────

@bp.post("/api/start")
def api_start():
    """
    Start a crawl job.

    Supported modes (``mode`` field in JSON body):

    Auto-sync modes — persist config, register/update APScheduler daily job at
    21:19 ICT, run immediately, and re-run automatically every day:
      "2m"          Last 2 calendar months → today
      "6m"          Last 6 calendar months → today
      "1y"          Last 12 calendar months → today
      "custom_date" Explicit start_date / end_date (YYYY-MM-DD)

    Manual mode — one-off crawl, disables auto-sync scheduler:
      "range"       start_month / start_year / end_month / end_year integers
    """
    data = request.get_json(force=True, silent=True) or {}
    app  = current_app._get_current_object()
    mode = data.get("mode", "range")

    AUTO_SYNC_MODES = {"2m", "6m", "1y", "custom_date"}

    # ── Auto-sync modes ───────────────────────────────────────────────────────
    if mode in AUTO_SYNC_MODES:
        # Guard: don't allow a second concurrent chunk-chain.
        if get_auto_sync_state():
            return jsonify({"ok": False, "error": "Auto-sync is already running."}), 409

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
            # Derive the ISO date range the same way the scheduler would, so
            # the immediate run and the daily scheduled runs are always consistent.
            start_iso, end_iso = _resolve_date_range({"preset": mode})

        # Persist config to disk and register / refresh the daily scheduled job.
        try:
            run_hour   = max(0, min(23, int(data.get("run_hour",   19))))
            run_minute = max(0, min(59, int(data.get("run_minute",  0))))
        except (TypeError, ValueError):
            run_hour, run_minute = 19, 0

        account_ids = data.get("account_ids") or []
        saved_cfg = enable_auto_sync(
            preset=mode,
            start_date=start_iso if mode == "custom_date" else None,
            end_date=end_iso     if mode == "custom_date" else None,
            run_hour=run_hour,
            run_minute=run_minute,
            account_ids=account_ids,
            app=app,
        )

        # Chỉ đăng ký lịch, KHÔNG chạy ngay. Scheduler sẽ kích hoạt đúng giờ.
        socketio.emit("auto_sync_state", {"active": False, "config": saved_cfg})
        result = {"ok": True, "chunks": [], "scheduled_only": True}

    # ── Manual range mode ─────────────────────────────────────────────────────
    else:
        # Cancel any live auto-sync chain first.
        if get_auto_sync_state():
            _auto_sync_stop.set()
            _set_auto_sync(False)
            stop_crawl()
            logger.info("Auto-sync cancelled by manual range crawl.")

        # Disable the persisted scheduler so the 21:19 job won't fire unexpectedly.
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
            # fallback: single account or legacy env creds
            account_ids = [None]
        # Launch each account as a separate parallel chain
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
        return jsonify({
            **result,
            "total_chunks": 0,
            "mode": mode,
            "message": f"Auto-sync đã được lên lịch lúc {run_hour:02d}:{run_minute:02d} ICT. Sẽ tự chạy đúng giờ.",
        })

    return jsonify({**result, "total_chunks": len(result["chunks"]), "mode": mode})


# ── POST /crawler/api/stop ────────────────────────────────────────────────────

@bp.post("/api/stop")
def api_stop():
    """
    Stop the running crawl engine AND disable the auto-sync scheduler.

    This is the "full stop" action.  Use /api/auto-sync/disable if you only
    want to turn off the scheduler while letting the current job finish.
    """
    auto_was_active = get_auto_sync_state()
    if auto_was_active:
        _auto_sync_stop.set()
        _set_auto_sync(False)

    # Support stopping a specific account or all
    aid_raw = (request.get_json(force=True, silent=True) or {}).get("account_id")
    account_id_stop = int(aid_raw) if aid_raw is not None else None
    stopped = stop_crawl(account_id=account_id_stop)

    # Disable the persisted scheduler config (even if no in-process chain was
    # running, the 21:19 job should not fire after an explicit stop).
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
    """
    Turn off the daily scheduler without stopping an already-running crawl job.

    Useful when the user wants to cancel tomorrow's automatic run but is happy
    to let tonight's job complete normally.
    """
    # Stop the in-process chain if it's still looping between chunks.
    if get_auto_sync_state():
        _auto_sync_stop.set()
        _set_auto_sync(False)

    disable_auto_sync()
    socketio.emit("auto_sync_state", {"active": False, "config": None})
    logger.info("Auto-sync disabled via /api/auto-sync/disable.")
    return jsonify({"ok": True})


# ── GET /crawler/api/status ───────────────────────────────────────────────────

@bp.get("/api/status")
def api_status():
    """
    Return a merged status snapshot.

    ``get_crawl_status()`` reflects the persisted scheduler config (disk);
    ``get_auto_sync_state()`` reflects whether a chunk-chain thread is running
    right now.  Both sources are merged so the client always sees the most
    accurate picture.
    """
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

    # account_id in body — None means legacy single-account
    aid_raw    = data.get("account_id")
    account_id = int(aid_raw) if aid_raw is not None else None

    with _captcha_states_lock:
        state = _captcha_states.get(account_id)

    if state is None:
        # Fallback: answer any waiting state (e.g. if account_id not sent)
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
    """
    Preview which month-chunks a given date range would produce.

    Supports three calling conventions:
      1. ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD   (date picker)
      2. ?months_back=N                                (preset buttons)
      3. ?start_month=M&start_year=Y&end_month=M&end_year=Y  (legacy selectors)
    """
    # ── Convention 1: explicit ISO date range ─────────────────────────────────
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

    # ── Convention 2: months_back preset ─────────────────────────────────────
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

    # ── Convention 3: legacy month/year selectors ─────────────────────────────
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

# ─────────────────────────────────────────────────────── GDT Accounts API

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