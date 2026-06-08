"""
Crawler service — bridge between Flask routes and the async crawler engine.
Manages the background asyncio event loop, running job state, and the
daily auto-sync scheduler (fires every day at 21:19 ICT / Asia/Ho_Chi_Minh).

Auto-sync state is persisted in a simple JSON sidecar file so it survives
process restarts.  The file lives at:
    <app.config.Config.DATA_DIR>/auto_sync_config.json
or falls back to  ./auto_sync_config.json  when DATA_DIR is not defined.

Schema:
    {
        "enabled":      true,
        "preset":       "2m",          # "2m" | "6m" | "1y" | "custom_date"
        "start_date":   null,          # ISO date, only for preset == "custom_date"
        "end_date":     null,          # ISO date, only for preset == "custom_date"
        "next_run_at":  "21:19",       # display only — always 21:19 local
        "activated_at": "<ISO datetime>"
    }
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from app.automation.crawler_engine import CrawlerEngine
from app.db.repository import CrawlJobRepository


# ── Background asyncio loop (runs for the lifetime of the process) ─────────────
_loop:        Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread]          = None

# ── Active engines — keyed by account_id (None = legacy single-account mode) ───
# Dict[Optional[int], CrawlerEngine]
_active_engines: dict = {}
# Dict[Optional[int], asyncio.Future]
_active_futures: dict = {}

# ── APScheduler instance ───────────────────────────────────────────────────────
_scheduler: Optional[BackgroundScheduler]         = None


# ══════════════════════════════════════════════════════════════════════════════
# Auto-sync config — path resolution
# ══════════════════════════════════════════════════════════════════════════════

def _config_path() -> Path:
    """Return the filesystem path for the auto-sync JSON sidecar."""
    try:
        from app.config import Config
        data_dir = getattr(Config, "DATA_DIR", None)
        if data_dir:
            return Path(data_dir) / "auto_sync_config.json"
    except Exception:
        pass
    return Path("auto_sync_config.json")


# ══════════════════════════════════════════════════════════════════════════════
# Auto-sync config persistence
# ══════════════════════════════════════════════════════════════════════════════

def load_auto_sync_config() -> dict:
    """Return persisted auto-sync config, or a disabled default."""
    p = _config_path()
    if p.exists():
        try:
            with p.open() as f:
                return json.load(f)
        except Exception:
            pass
    return {"enabled": False}


def save_auto_sync_config(cfg: dict) -> None:
    """Persist auto-sync config to disk (creates parent dirs as needed)."""
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    logger.info("Auto-sync config saved: {}", cfg)


def clear_auto_sync_config() -> None:
    """Overwrite the sidecar with a disabled state."""
    save_auto_sync_config({"enabled": False})


# ══════════════════════════════════════════════════════════════════════════════
# Date-range helpers
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_date_range(cfg: dict) -> tuple[str, str]:
    """
    Given a saved auto-sync config dict, return (start_date, end_date) as ISO
    strings relative to *today*.

    For preset modes (2m / 6m / 1y) end_date is always today so each daily
    scheduled run covers up to the current day.  For custom_date the stored
    values are returned verbatim.
    """
    preset = cfg.get("preset", "2m")
    today  = date.today()

    if preset == "custom_date":
        return cfg["start_date"], cfg["end_date"]

    months_map = {"2m": 2, "6m": 6, "1y": 12}
    months     = months_map.get(preset, 2)

    start_month = today.month - months
    start_year  = today.year
    while start_month <= 0:
        start_month += 12
        start_year  -= 1

    start = date(start_year, start_month, 1)
    return start.isoformat(), today.isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Background asyncio loop
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Return the shared background event loop, starting it if necessary."""
    global _loop, _loop_thread
    if _loop is None or not _loop.is_running():
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(
            target=_loop.run_forever, daemon=True, name="crawler-loop"
        )
        _loop_thread.start()
        logger.info("Background asyncio loop started.")
    return _loop


# ══════════════════════════════════════════════════════════════════════════════
# Core crawl launcher
# ══════════════════════════════════════════════════════════════════════════════

def start_crawl(
    job_id: int,
    username: str,
    password: str,
    start_date: str,
    end_date: str,
    account_id: Optional[int] = None,
    emit_fn: Optional[Callable[[str], None]] = None,
    emit_captcha_fn: Optional[Callable[[str], None]] = None,
    captcha_event: Optional[threading.Event] = None,
    get_captcha_answer: Optional[Callable[[], str]] = None,
    app=None,
) -> bool:
    """
    Submit a CrawlerEngine coroutine to the background asyncio loop.

    Multiple accounts can run simultaneously — keyed by account_id.
    Returns False if this specific account_id already has a running crawl.
    """
    global _active_engines, _active_futures

    if account_id in _active_engines and _active_engines[account_id] is not None:
        logger.warning("Crawl already running for account_id={}", account_id)
        return False

    loop = _ensure_loop()

    engine = CrawlerEngine(
        job_id=job_id,
        username=username,
        password=password,
        start_date=start_date,
        end_date=end_date,
        account_id=account_id,
        emit_fn=emit_fn,
        emit_captcha_fn=emit_captcha_fn,
        captcha_event=captcha_event,
        get_captcha_answer=get_captcha_answer,
        app=app,
    )
    _active_engines[account_id] = engine

    future = asyncio.run_coroutine_threadsafe(engine.run(), loop)
    _active_futures[account_id] = future

    def _on_done(f: asyncio.Future, _aid=account_id) -> None:
        _active_engines.pop(_aid, None)
        _active_futures.pop(_aid, None)
        try:
            exc = f.exception()
            if exc:
                logger.error("Crawler task exception (account_id={}): {}", _aid, exc)
        except asyncio.CancelledError:
            pass

    future.add_done_callback(_on_done)
    logger.info("Crawl job #{} (account_id={}) submitted to background loop.", job_id, account_id)
    return True


def stop_crawl(account_id: Optional[int] = None) -> bool:
    """
    Stop a running crawl.
    If account_id is given, stop only that account's engine.
    If None, stop ALL running engines.
    """
    global _active_engines, _active_futures

    if account_id is not None:
        engine = _active_engines.get(account_id)
        future = _active_futures.get(account_id)
        if engine is None:
            return False
        engine.request_stop()
        if future and not future.done():
            future.cancel()
        return True

    # Stop all
    if not _active_engines:
        return False
    for aid, engine in list(_active_engines.items()):
        engine.request_stop()
        future = _active_futures.get(aid)
        if future and not future.done():
            future.cancel()
    return True


def get_crawl_status() -> dict:
    """
    Return a status snapshot that merges live engine state with the persisted
    auto-sync config.

    Fields returned:
        is_running       bool   — True when an engine coroutine is active
        running_job      dict|None — serialised CrawlJob if running
        recent_jobs      list[dict] — last 5 jobs
        auto_sync_active bool   — True when the JSON config has enabled=True
        auto_sync_config dict   — the full persisted config
    """
    running_jobs  = CrawlJobRepository.get_running_all()
    recent        = CrawlJobRepository.get_recent(10)
    auto_sync_cfg = load_auto_sync_config()

    return {
        "is_running":         bool(_active_engines),
        "active_account_ids": list(_active_engines.keys()),
        "running_jobs":       [j.to_dict() for j in running_jobs],
        # backward compat — single job
        "running_job":        running_jobs[0].to_dict() if running_jobs else None,
        "recent_jobs":        [j.to_dict() for j in recent],
        "auto_sync_active":   auto_sync_cfg.get("enabled", False),
        "auto_sync_config":   auto_sync_cfg,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Auto-sync public API  (called from the blueprint)
# ══════════════════════════════════════════════════════════════════════════════

def enable_auto_sync(
    preset: str,
    start_date: str | None = None,
    end_date: str | None = None,
    run_hour: int = 19,
    run_minute: int = 0,
    app=None,
) -> dict:
    """
    Persist the auto-sync config and register the daily scheduler job.

    Parameters
    ----------
    preset:
        One of "2m", "6m", "1y", "custom_date".
    start_date / end_date:
        ISO date strings; only used when preset == "custom_date".
    run_hour / run_minute:
        Local time (Asia/Ho_Chi_Minh) at which the daily job fires.
        Defaults to 19:00.

    Returns the saved config dict so the caller can forward it to clients.
    """
    run_hour   = max(0, min(23, int(run_hour)))
    run_minute = max(0, min(59, int(run_minute)))

    cfg: dict = {
        "enabled":      True,
        "preset":       preset,
        "start_date":   start_date,
        "end_date":     end_date,
        "run_hour":     run_hour,
        "run_minute":   run_minute,
        "next_run_at":  f"{run_hour:02d}:{run_minute:02d}",
        "activated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_auto_sync_config(cfg)
    _ensure_scheduler(app=app, run_hour=run_hour, run_minute=run_minute)
    logger.info(
        "Auto-sync enabled — preset='{}' schedule={:02d}:{:02d} activated_at='{}'",
        preset, run_hour, run_minute, cfg["activated_at"],
    )
    return cfg


def disable_auto_sync() -> None:
    """
    Disable auto-sync: clear the JSON config and remove the APScheduler job.
    The scheduler process itself stays alive so re-enabling later is cheap.
    """
    clear_auto_sync_config()
    _remove_scheduler_job()
    logger.info("Auto-sync disabled.")


# ══════════════════════════════════════════════════════════════════════════════
# APScheduler — daily 21:19 ICT job
# ══════════════════════════════════════════════════════════════════════════════

def _run_scheduled_sync(app=None) -> None:
    """
    Callback executed by APScheduler at the configured daily time.

    Delegates chunk splitting + execution to the blueprint's _launch_chunks
    via a direct import so socket events, auto_sync state flags, and the
    chunk-chain thread are all managed in one place — identical to what happens
    when the user clicks Start manually.
    """
    cfg = load_auto_sync_config()
    if not cfg.get("enabled"):
        logger.info("Scheduled sync fired but auto-sync is disabled — skipping.")
        return

    logger.info(
        "⏰ Scheduled auto-sync triggered at {}",
        datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    start_date, end_date = _resolve_date_range(cfg)

    # Import blueprint helpers at call time to avoid circular imports at module load.
    try:
        from app.blueprints.crawler import _chunks_from_iso_range, _launch_chunks  # type: ignore[import]
    except ImportError:
        try:
            from app.routes.crawler import _chunks_from_iso_range, _launch_chunks  # type: ignore[import]
        except ImportError:
            logger.error("Auto-sync: cannot import _launch_chunks from crawler blueprint — aborting.")
            return

    chunks = _chunks_from_iso_range(start_date, end_date)
    if not chunks:
        logger.warning("Auto-sync: no chunks generated for {} → {} — skipping.", start_date, end_date)
        return

    result = _launch_chunks(chunks, app, is_auto_sync=True, auto_sync_cfg=cfg)
    if result.get("ok"):
        logger.info(
            "Auto-sync: {} chunk(s) queued for {} → {}",
            len(result.get("chunks", [])), start_date, end_date,
        )
    else:
        logger.error("Auto-sync: _launch_chunks failed — {}", result.get("error"))


def _ensure_scheduler(app=None, run_hour: int = 19, run_minute: int = 0) -> None:
    """
    Start the BackgroundScheduler (if not running) and register / refresh the
    daily cron job at the specified local time.  Idempotent — safe to call
    multiple times; the old job is always removed before adding the new one.
    """
    global _scheduler

    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
        _scheduler.start()
        logger.info("APScheduler started (tz=Asia/Ho_Chi_Minh).")

    # Remove stale job first to prevent duplicates.
    _remove_scheduler_job()

    import functools
    job_fn = functools.partial(_run_scheduled_sync, app=app) if app else _run_scheduled_sync

    _scheduler.add_job(
        func=job_fn,
        trigger=CronTrigger(hour=run_hour, minute=run_minute, timezone="Asia/Ho_Chi_Minh"),
        id="auto_sync_daily",
        name=f"Daily auto-sync at {run_hour:02d}:{run_minute:02d} ICT",
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info(
        "Scheduled daily auto-sync job registered ({:02d}:{:02d} ICT).",
        run_hour, run_minute,
    )


def _remove_scheduler_job() -> None:
    """Remove the daily auto-sync job from the scheduler if it exists."""
    global _scheduler
    if _scheduler and _scheduler.get_job("auto_sync_daily"):
        _scheduler.remove_job("auto_sync_daily")
        logger.info("Removed existing auto_sync_daily scheduler job.")


# ══════════════════════════════════════════════════════════════════════════════
# App startup hook
# ══════════════════════════════════════════════════════════════════════════════

def restore_auto_sync_on_startup(app=None) -> None:
    """
    Call this once inside ``create_app`` (or equivalent) after the Flask app is
    fully initialised.

    If auto-sync was active before the process restarted the JSON sidecar will
    still have ``enabled: true``, so we re-register the APScheduler job to
    ensure the 21:19 daily trigger continues without any user interaction.

    Usage in ``app/__init__.py``::

        from app.services.crawler_service import restore_auto_sync_on_startup

        with app.app_context():
            restore_auto_sync_on_startup(app=app)
    """
    cfg = load_auto_sync_config()
    if cfg.get("enabled"):
        run_hour   = int(cfg.get("run_hour",   19))
        run_minute = int(cfg.get("run_minute",  0))
        logger.info(
            "Restoring auto-sync on startup (preset='{}', schedule={:02d}:{:02d}, activated_at={}).",
            cfg.get("preset"), run_hour, run_minute, cfg.get("activated_at"),
        )
        _ensure_scheduler(app=app, run_hour=run_hour, run_minute=run_minute)
    else:
        logger.info("Auto-sync is not enabled — skipping scheduler restore.")