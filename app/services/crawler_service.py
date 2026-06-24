from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from app.automation.api_engine import ApiCrawlerEngine as CrawlerEngine
from app.db.repository import CrawlJobRepository


_loop:        Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread]          = None
_active_engines: dict = {}
_active_futures: dict = {}
_scheduler: Optional[BackgroundScheduler] = None


def _config_path() -> Path:
    try:
        from app.config import Config
        data_dir = getattr(Config, "DATA_DIR", None)
        if data_dir:
            return Path(data_dir) / "auto_sync_config.json"
    except Exception:
        pass
    return Path("auto_sync_config.json")


def load_auto_sync_config() -> dict:
    p = _config_path()
    if p.exists():
        try:
            with p.open() as f:
                return json.load(f)
        except Exception:
            pass
    return {"enabled": False}


def save_auto_sync_config(cfg: dict) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def clear_auto_sync_config() -> None:
    save_auto_sync_config({"enabled": False})


def _resolve_date_range(cfg: dict) -> tuple[str, str]:
    preset = cfg.get("preset", "2m")
    today  = date.today()

    if preset == "custom_date":
        return cfg["start_date"], cfg["end_date"]

    months = {"2m": 2, "6m": 6, "1y": 12}.get(preset, 2)
    start_month = today.month - months
    start_year  = today.year
    while start_month <= 0:
        start_month += 12
        start_year  -= 1

    return date(start_year, start_month, 1).isoformat(), today.isoformat()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    if _loop is None or not _loop.is_running():
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(
            target=_loop.run_forever, daemon=True, name="crawler-loop"
        )
        _loop_thread.start()
    return _loop


def start_crawl(
    job_id: int,
    username: str,
    password: str,
    chunks: List[Tuple[str, str]],
    account_id: Optional[int] = None,
    emit_fn: Optional[Callable[[str], None]] = None,
    emit_captcha_fn: Optional[Callable[[str], None]] = None,
    captcha_event: Optional[threading.Event] = None,
    get_captcha_answer: Optional[Callable[[], str]] = None,
    app=None,
) -> bool:
    global _active_engines, _active_futures

    if account_id in _active_engines and _active_engines[account_id] is not None:
        logger.warning("Crawl already running for account_id={}", account_id)
        return False

    loop   = _ensure_loop()
    engine = CrawlerEngine(
        job_id=job_id, username=username, password=password,
        chunks=chunks,
        account_id=account_id, emit_fn=emit_fn,
        emit_captcha_fn=emit_captcha_fn, captcha_event=captcha_event,
        get_captcha_answer=get_captcha_answer, app=app,
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
    return True


def stop_crawl(account_id: Optional[int] = None) -> bool:
    global _active_engines, _active_futures

    if account_id is not None:
        engine = _active_engines.get(account_id)
        if engine is None:
            return False
        engine.request_stop()
        future = _active_futures.get(account_id)
        if future and not future.done():
            future.cancel()
        return True

    if not _active_engines:
        return False
    for aid, engine in list(_active_engines.items()):
        engine.request_stop()
        future = _active_futures.get(aid)
        if future and not future.done():
            future.cancel()
    return True


def get_crawl_status() -> dict:
    running_jobs  = CrawlJobRepository.get_running_all()
    recent        = CrawlJobRepository.get_recent(10)
    auto_sync_cfg = load_auto_sync_config()

    return {
        "is_running":         bool(_active_engines),
        "active_account_ids": list(_active_engines.keys()),
        "running_jobs":       [j.to_dict() for j in running_jobs],
        "running_job":        running_jobs[0].to_dict() if running_jobs else None,
        "recent_jobs":        [j.to_dict() for j in recent],
        "auto_sync_active":   auto_sync_cfg.get("enabled", False),
        "auto_sync_config":   auto_sync_cfg,
    }


def enable_auto_sync(
    preset: str,
    start_date: str | None = None,
    end_date: str | None = None,
    account_schedules: Optional[list] = None,
    account_id: Optional[int] = None,
    account_ids: Optional[list] = None,
    run_hour: int = 19,
    run_minute: int = 0,
    app=None,
) -> dict:
    if not account_schedules:
        ids = account_ids or ([account_id] if account_id is not None else [])
        account_schedules = [
            {"account_id": aid, "run_hour": run_hour, "run_minute": run_minute}
            for aid in ids
        ] if ids else [{"account_id": None, "run_hour": run_hour, "run_minute": run_minute}]

    account_schedules = [
        {
            "account_id": s.get("account_id"),
            "run_hour":   max(0, min(23, int(s.get("run_hour",   19)))),
            "run_minute": max(0, min(59, int(s.get("run_minute",  0)))),
        }
        for s in account_schedules
    ]

    cfg: dict = {
        "enabled":           True,
        "preset":            preset,
        "start_date":        start_date,
        "end_date":          end_date,
        "account_schedules": account_schedules,
        "activated_at":      datetime.now().isoformat(timespec="seconds"),
        "account_id":  account_schedules[0]["account_id"],
        "run_hour":    account_schedules[0]["run_hour"],
        "run_minute":  account_schedules[0]["run_minute"],
        "next_run_at": f"{account_schedules[0]['run_hour']:02d}:{account_schedules[0]['run_minute']:02d}",
    }
    save_auto_sync_config(cfg)
    _ensure_scheduler(app=app, account_schedules=account_schedules)
    return cfg


def disable_auto_sync() -> None:
    clear_auto_sync_config()
    _remove_all_scheduler_jobs()


def _run_scheduled_sync(app=None, account_id: Optional[int] = None) -> None:
    cfg = load_auto_sync_config()
    if not cfg.get("enabled"):
        return

    start_date, end_date = _resolve_date_range(cfg)

    try:
        from app.blueprints.crawler import _chunks_from_iso_range, _launch_chunks  # type: ignore[import]
    except ImportError:
        try:
            from app.routes.crawler import _chunks_from_iso_range, _launch_chunks  # type: ignore[import]
        except ImportError:
            logger.error("Auto-sync: cannot import _launch_chunks — aborting.")
            return

    chunks = _chunks_from_iso_range(start_date, end_date)
    if not chunks:
        return

    _launch_chunks(chunks, app, account_id=account_id, is_auto_sync=True, auto_sync_cfg=cfg)


def _ensure_scheduler(
    app=None,
    run_hour: int = 19,
    run_minute: int = 0,
    account_schedules: Optional[list] = None,
) -> None:
    global _scheduler

    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
        _scheduler.start()

    _remove_all_scheduler_jobs()

    import functools
    for sched in (account_schedules or [{"account_id": None, "run_hour": run_hour, "run_minute": run_minute}]):
        aid    = sched.get("account_id")
        h      = max(0, min(23, int(sched.get("run_hour",   run_hour))))
        m      = max(0, min(59, int(sched.get("run_minute", run_minute))))
        job_id = f"auto_sync_{aid if aid is not None else 'default'}"

        _scheduler.add_job(
            func=functools.partial(_run_scheduled_sync, app=app, account_id=aid),
            trigger=CronTrigger(hour=h, minute=m, timezone="Asia/Ho_Chi_Minh"),
            id=job_id,
            name=f"Daily auto-sync account={aid} at {h:02d}:{m:02d} ICT",
            replace_existing=True,
            misfire_grace_time=300,
        )


def _remove_all_scheduler_jobs() -> None:
    global _scheduler
    if not _scheduler:
        return
    for job in _scheduler.get_jobs():
        if job.id.startswith("auto_sync_"):
            _scheduler.remove_job(job.id)


def restore_auto_sync_on_startup(app=None) -> None:
    cfg = load_auto_sync_config()
    if cfg.get("enabled"):
        schedules = cfg.get("account_schedules") or [{
            "account_id": cfg.get("account_id"),
            "run_hour":   int(cfg.get("run_hour",   19)),
            "run_minute": int(cfg.get("run_minute",  0)),
        }]
        _ensure_scheduler(app=app, account_schedules=schedules)
