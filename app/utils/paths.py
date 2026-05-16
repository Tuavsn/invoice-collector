"""Path helpers — resolve invoice storage directories."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.config import Config
from app.utils.dates import date_to_path_parts


def invoice_dir(invoice_no: str, issue_date: date) -> Path:
    """Return the directory path for a single invoice."""
    yyyy, mm, dd = date_to_path_parts(issue_date)
    safe_no = invoice_no.replace("/", "_").replace("\\", "_")
    return Config.INVOICE_PATH / yyyy / mm / dd / safe_no


def ensure_invoice_dir(invoice_no: str, issue_date: date) -> Path:
    """Create and return the invoice directory."""
    path = invoice_dir(invoice_no, issue_date)
    path.mkdir(parents=True, exist_ok=True)
    return path