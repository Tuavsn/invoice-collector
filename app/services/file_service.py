"""File management service — organize, list, and serve downloaded invoice files."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.config import Config


class FileService:

    @staticmethod
    def list_invoice_files(invoice_no: str, issue_date_str: str) -> Dict[str, Any]:
        """Return all file paths for a given invoice."""
        from app.utils.dates import parse_date
        from app.utils.paths import invoice_dir

        issue_date = parse_date(issue_date_str)
        if not issue_date:
            return {}

        inv_dir = invoice_dir(invoice_no, issue_date)
        if not inv_dir.exists():
            return {}

        result: Dict[str, Any] = {"directory": str(inv_dir), "files": []}
        for f in sorted(inv_dir.iterdir()):
            if f.is_file():
                result["files"].append(
                    {
                        "name": f.name,
                        "path": str(f),
                        "size": f.stat().st_size,
                        "extension": f.suffix.lower(),
                    }
                )
        return result

    @staticmethod
    def get_download_stats() -> Dict[str, int]:
        """Count total downloaded files by type."""
        counts: Dict[str, int] = {"xml": 0, "pdf": 0, "json": 0, "other": 0}
        if not Config.INVOICE_PATH.exists():
            return counts
        for f in Config.INVOICE_PATH.rglob("*"):
            if f.is_file():
                ext = f.suffix.lower().lstrip(".")
                if ext in counts:
                    counts[ext] += 1
                else:
                    counts["other"] += 1
        return counts

    @staticmethod
    def delete_invoice_files(invoice_dir_path: str) -> bool:
        """Delete all files for a single invoice (dangerous — use with caution)."""
        path = Path(invoice_dir_path)
        if path.exists() and path.is_dir():
            shutil.rmtree(path)
            logger.info("Deleted invoice directory: {}", path)
            return True
        return False

    @staticmethod
    def disk_usage() -> Dict[str, str]:
        """Return human-readable disk usage for the invoices directory."""
        total_bytes = sum(
            f.stat().st_size
            for f in Config.INVOICE_PATH.rglob("*")
            if f.is_file()
        )
        return {
            "bytes": total_bytes,
            "human": _human_size(total_bytes),
        }


def _human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"