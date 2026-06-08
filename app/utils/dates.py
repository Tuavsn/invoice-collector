"""
app/utils/dates.py — Date parsing helpers.

GDT XML dùng nhiều format khác nhau:
  - NLap (ngày lập):     "29/04/2026"  (dd/mm/yyyy)  ← phổ biến nhất
  - NLap alternative:    "2026-04-29"  (yyyy-mm-dd)
  - Bảng web:            "29/04/2026"  hoặc "29-04-2026"
  - ISO datetime:        "2026-04-29T13:22:55"

parse_date()      → trả về date object hoặc None
date_to_path_parts() → trả về (yyyy, mm, dd) string tuple cho đường dẫn thư mục
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Tuple


# Tất cả format GDT có thể trả về cho trường ngày
_DATE_FORMATS = [
    "%d/%m/%Y",          # 29/04/2026  ← GDT XML NLap phổ biến nhất
    "%Y-%m-%d",          # 2026-04-29  ← ISO
    "%d-%m-%Y",          # 29-04-2026
    "%d/%m/%y",          # 29/04/26    ← 2-digit year
    "%Y/%m/%d",          # 2026/04/29
    "%d.%m.%Y",          # 29.04.2026
]

_DATETIME_FORMATS = [
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
]


def parse_date(raw: Optional[str]) -> Optional[date]:
    """
    Parse chuỗi ngày từ XML GDT hoặc bảng web thành date object.
    Hỗ trợ tất cả format GDT đã biết.
    Trả về None nếu không parse được.
    """
    if not raw:
        return None

    raw = raw.strip()
    if not raw:
        return None

    # Thử parse as date trực tiếp
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    # Thử parse as datetime rồi lấy .date()
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    # Fallback: nếu là datetime object đã parse sẵn
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    return None


def date_to_path_parts(d: date) -> Tuple[str, str, str]:
    """
    Chuyển date thành tuple (yyyy, mm, dd) dùng cho đường dẫn thư mục.
    VD: date(2026, 4, 29) → ("2026", "04", "29")
    """
    return (
        str(d.year),
        f"{d.month:02d}",
        f"{d.day:02d}",
    )