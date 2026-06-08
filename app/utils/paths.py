"""Path helpers — resolve invoice storage directories."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.config import Config
from app.utils.dates import date_to_path_parts


def invoice_dir(invoice_no: str, issue_date: date, suffix: Optional[str] = None) -> Path:
    """Return the directory path for a single invoice.
 
    suffix = invoice_category (e.g. 'sale_einvoice', 'purchase_einvoice').
    Khi có suffix, tên thư mục là '{invoice_no}_{suffix}' — tránh xung đột
    khi cùng số hóa đơn xuất hiện ở cả Bán ra lẫn Mua vào.
    """
    yyyy, mm, dd = date_to_path_parts(issue_date)
    safe_no = invoice_no.replace("/", "_").replace("\\", "_")
    folder_name = f"{safe_no}_{suffix}" if suffix else safe_no
    return Config.INVOICE_PATH / yyyy / mm / dd / folder_name
 
 
def ensure_invoice_dir(invoice_no: str, issue_date: date, suffix: Optional[str] = None) -> Path:
    """Create and return the invoice directory."""
    path = invoice_dir(invoice_no, issue_date, suffix=suffix)
    path.mkdir(parents=True, exist_ok=True)
    return path