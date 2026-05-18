"""Miscellaneous helper utilities."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

def safe_filename(name: str) -> str:
    """Replace characters illegal in filenames."""
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def write_json(path: Path, data: Any) -> None:
    """Write *data* as pretty-printed JSON to *path*."""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    """Read JSON from *path*; return None if file missing or invalid."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def format_currency(value: float) -> str:
    """Format number as Vietnamese currency string."""
    return f"{value:,.0f} VNĐ"