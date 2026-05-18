"""
Crawler service — bridge between Flask routes and the async crawler engine.
Manages the background asyncio event loop and running job state.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Callable, Optional

from loguru import logger

from app.automation.crawler_engine import CrawlerEngine
from app.db.repository import CrawlJobRepository

# ── Background asyncio loop (runs for the lifetime of the process) ────────────
_loop:        Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread]          = None

# ── Active engine reference (one job at a time) ───────────────────────────────
_active_engine: Optional[CrawlerEngine]           = None
_active_future: Optional[asyncio.Future]          = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    if _loop is None or not _loop.is_running():
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(
            target=_loop.run_forever, daemon=True, name="crawler-loop"
        )
        _loop_thread.start()
        logger.info("Background asyncio loop started.")
    return _loop


def start_crawl(
    job_id: int,
    username: str,
    password: str,
    start_date: str,
    end_date: str,
    emit_fn: Optional[Callable[[str], None]] = None,
    emit_captcha_fn: Optional[Callable[[str], None]] = None,
    captcha_event: Optional[threading.Event] = None,
    get_captcha_answer: Optional[Callable[[], str]] = None,
    app=None,
) -> bool:
    """
    Launch the crawler engine in the background loop.
    Returns False if a crawl is already running.
    """
    global _active_engine, _active_future

    if _active_engine is not None:
        running_job = CrawlJobRepository.get_running()
        if running_job and running_job.status == "running":
            logger.warning("Crawl already running — job #{}", running_job.id)
            return False

    loop = _ensure_loop()

    engine = CrawlerEngine(
        job_id=job_id,
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
    _active_engine = engine

    future = asyncio.run_coroutine_threadsafe(engine.run(), loop)
    _active_future = future

    def _on_done(f: asyncio.Future) -> None:
        global _active_engine, _active_future
        _active_engine = None
        _active_future = None
        try:
            exc = f.exception()
            if exc:
                logger.error("Crawler task exception: {}", exc)
        except asyncio.CancelledError:
            pass

    future.add_done_callback(_on_done)
    logger.info("Crawl job #{} submitted to background loop.", job_id)
    return True


def stop_crawl() -> bool:
    """Signal the active crawler to stop and cancel its future."""
    global _active_engine, _active_future

    if _active_engine is None:
        return False

    _active_engine.request_stop()

    if _active_future and not _active_future.done():
        _active_future.cancel()

    return True


def get_crawl_status() -> dict:
    """Return current crawl status summary."""
    running_job = CrawlJobRepository.get_running()
    recent      = CrawlJobRepository.get_recent(5)

    return {
        "is_running":  _active_engine is not None,
        "running_job": running_job.to_dict() if running_job else None,
        "recent_jobs": [j.to_dict() for j in recent],
    }