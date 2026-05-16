"""
Application configuration — loads from environment / .env file.
All tunable parameters live here; no magic strings elsewhere.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (two levels up from this file)
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Config:
    # ------------------------------------------------------------------ Flask
    SECRET_KEY: str = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    HOST: str = os.getenv("FLASK_HOST", "0.0.0.0")
    PORT: int = int(os.getenv("FLASK_PORT", 5000))
    DEBUG: bool = os.getenv("FLASK_ENV", "production") == "development"

    # --------------------------------------------------------------- Database
    SQLALCHEMY_DATABASE_URI: str = os.getenv(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'invoices.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    # ------------------------------------------------------------------- GDT
    GDT_USERNAME: str = os.getenv("GDT_USERNAME", "")
    GDT_PASSWORD: str = os.getenv("GDT_PASSWORD", "")
    GDT_BASE_URL: str = "https://hoadondientu.gdt.gov.vn"
    GDT_LOGIN_URL: str = "https://hoadondientu.gdt.gov.vn/"
    GDT_SEARCH_URL: str = (
        "https://hoadondientu.gdt.gov.vn/tra-cuu/tra-cuu-hoa-don"
    )

    # ------------------------------------------------------------------ Paths
    DOWNLOAD_PATH: Path = BASE_DIR / os.getenv("DOWNLOAD_PATH", "downloads")
    INVOICE_PATH: Path = BASE_DIR / os.getenv("INVOICE_PATH", "invoices")
    EXPORT_PATH: Path = BASE_DIR / os.getenv("EXPORT_PATH", "exports")
    LOG_PATH: Path = BASE_DIR / os.getenv("LOG_PATH", "logs")
    SCREENSHOT_PATH: Path = LOG_PATH / "screenshots"

    # --------------------------------------------------------------- Playwright
    PLAYWRIGHT_HEADLESS: bool = (
        os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
    )
    PLAYWRIGHT_TIMEOUT: int = int(os.getenv("PLAYWRIGHT_TIMEOUT", 30000))
    PLAYWRIGHT_SLOW_MO: int = int(os.getenv("PLAYWRIGHT_SLOW_MO", 100))

    # ----------------------------------------------------------------- Crawler
    CRAWLER_MAX_RETRIES: int = int(os.getenv("CRAWLER_MAX_RETRIES", 5))
    CRAWLER_PAGE_SIZE: int = int(os.getenv("CRAWLER_PAGE_SIZE", 50))
    CRAWLER_DELAY_MS: int = int(os.getenv("CRAWLER_DELAY_MS", 500))

    # --------------------------------------------------------------- SocketIO
    SOCKETIO_ASYNC_MODE: str = os.getenv("SOCKETIO_ASYNC_MODE", "threading")

    @classmethod
    def ensure_directories(cls) -> None:
        """Create all required directories if they do not exist."""
        for path in (
            cls.DOWNLOAD_PATH,
            cls.INVOICE_PATH,
            cls.EXPORT_PATH,
            cls.LOG_PATH,
            cls.SCREENSHOT_PATH,
        ):
            path.mkdir(parents=True, exist_ok=True)