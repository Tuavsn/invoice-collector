"""
Centralised loguru configuration.
Call setup_logging() once at application startup.
"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
    "<level>{message}</level>"
)


def setup_logging(log_dir: Path, level: str = "DEBUG") -> None:
    """Configure loguru sinks: stderr + rotating file."""
    logger.remove()

    # Pretty console output
    logger.add(sys.stderr, format=_LOG_FORMAT, level=level, colorize=True)

    # Rotating file — one per day, kept for 30 days
    logger.add(
        log_dir / "app_{time:YYYY-MM-DD}.log",
        format=_LOG_FORMAT,
        level=level,
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
    )

    logger.info("Logging initialised — level={}", level)


def get_logger(name: str):  # type: ignore[return]
    """Return a contextual child logger bound to *name*."""
    return logger.bind(name=name)