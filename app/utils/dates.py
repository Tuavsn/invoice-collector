"""Date helpers used across the project."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional


def today_str(fmt: str = "%d/%m/%Y") -> str:
    return date.today().strftime(fmt)


def parse_date(value: str, fmt: str = "%d/%m/%Y") -> Optional[date]:
    """Parse *value* with *fmt*; return None on failure."""
    try:
        return datetime.strptime(value.strip(), fmt).date()
    except (ValueError, AttributeError):
        return None


def date_to_path_parts(d: date) -> tuple[str, str, str]:
    """Return (YYYY, MM, DD) strings suitable for folder construction."""
    return d.strftime("%Y"), d.strftime("%m"), d.strftime("%d")


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"