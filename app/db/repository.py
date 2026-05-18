"""
Repository layer — all database interactions pass through here.
Business logic NEVER touches the ORM directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import desc, func

from app.db.models import AppSetting, CrawlJob, Invoice
from app.extensions import db


# ─────────────────────────────────────────── Invoice Repository


class InvoiceRepository:
    @staticmethod
    def upsert(data: Dict[str, Any]) -> Invoice:
        """Insert or update an invoice identified by invoice_no + invoice_symbol."""
        existing = (
            db.session.query(Invoice)
            .filter_by(
                invoice_no=data["invoice_no"],
                invoice_symbol=data.get("invoice_symbol"),
            )
            .first()
        )
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
            existing.updated_at = datetime.utcnow()
            invoice = existing
        else:
            invoice = Invoice(**data)
            db.session.add(invoice)
        db.session.commit()
        return invoice

    @staticmethod
    def get_by_id(invoice_id: int) -> Optional[Invoice]:
        return db.session.get(Invoice, invoice_id)

    @staticmethod
    def get_by_invoice_no(invoice_no: str) -> Optional[Invoice]:
        """Lookup by invoice_no — used by skip-if-exists check in invoice_detail.py."""
        return (
            db.session.query(Invoice)
            .filter_by(invoice_no=invoice_no)
            .first()
        )

    @staticmethod
    def get_all(
        page: int = 1,
        per_page: int = 20,
        search: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        invoice_type: Optional[str] = None,
        ghi_chu: Optional[str] = None,
        thang_ke_khai: Optional[str] = None,
    ):
        query = db.session.query(Invoice)

        if search:
            like = f"%{search}%"
            query = query.filter(
                Invoice.invoice_no.ilike(like)
                | Invoice.seller_name.ilike(like)
                | Invoice.buyer_name.ilike(like)
                | Invoice.seller_tax_code.ilike(like)
            )
        if start_date:
            query = query.filter(Invoice.issue_date >= start_date)
        if end_date:
            # Include the full end_date day
            from datetime import timedelta
            query = query.filter(Invoice.issue_date < end_date + timedelta(days=1))
        if invoice_type:
            query = query.filter(Invoice.invoice_type == invoice_type)
        if ghi_chu:
            query = query.filter(Invoice.ghi_chu.ilike(f"%{ghi_chu}%"))
        if thang_ke_khai:
            query = query.filter(Invoice.thang_ke_khai.ilike(f"%{thang_ke_khai}%"))

        query = query.order_by(desc(Invoice.issue_date))
        return query.paginate(page=page, per_page=per_page, error_out=False)

    @staticmethod
    def stats() -> Dict[str, Any]:
        total        = db.session.query(func.count(Invoice.id)).scalar() or 0
        total_vat    = db.session.query(func.sum(Invoice.vat_amount)).scalar() or 0.0
        total_amount = db.session.query(func.sum(Invoice.total_amount)).scalar() or 0.0
        today        = datetime.utcnow().date()
        today_count  = (
            db.session.query(func.count(Invoice.id))
            .filter(func.date(Invoice.issue_date) == today)
            .scalar() or 0
        )
        return {
            "total_invoices": total,
            "total_vat":      total_vat,
            "total_amount":   total_amount,
            "today_count":    today_count,
        }

    @staticmethod
    def monthly_summary() -> List[Dict[str, Any]]:
        rows = (
            db.session.query(
                func.strftime("%Y-%m", Invoice.issue_date).label("month"),
                func.count(Invoice.id).label("count"),
                func.sum(Invoice.vat_amount).label("vat"),
                func.sum(Invoice.total_amount).label("total"),
            )
            .group_by("month")
            .order_by("month")
            .all()
        )
        return [
            {"month": r.month, "count": r.count, "vat": r.vat or 0, "total": r.total or 0}
            for r in rows
        ]

    @staticmethod
    def get_for_export(
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        ghi_chu: Optional[str] = None,
        thang_ke_khai: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Invoice]:
        query = db.session.query(Invoice)
        if start_date:
            query = query.filter(Invoice.issue_date >= start_date)
        if end_date:
            from datetime import timedelta
            query = query.filter(Invoice.issue_date < end_date + timedelta(days=1))
        if ghi_chu:
            query = query.filter(Invoice.ghi_chu.ilike(f"%{ghi_chu}%"))
        if thang_ke_khai:
            query = query.filter(Invoice.thang_ke_khai.ilike(f"%{thang_ke_khai}%"))
        if search:
            like = f"%{search}%"
            query = query.filter(
                Invoice.invoice_no.ilike(like)
                | Invoice.seller_name.ilike(like)
                | Invoice.buyer_name.ilike(like)
            )
        return query.order_by(Invoice.issue_date).all()


# ──────────────────────────────────────────── CrawlJob Repository


class CrawlJobRepository:
    @staticmethod
    def create(start_date: str, end_date: str) -> CrawlJob:
        job = CrawlJob(
            start_date=start_date,
            end_date=end_date,
            status="pending",
            start_time=datetime.utcnow(),
        )
        db.session.add(job)
        db.session.commit()
        logger.info("CrawlJob #{} created ({} → {})", job.id, start_date, end_date)
        return job

    @staticmethod
    def get_by_id(job_id: int) -> Optional[CrawlJob]:
        return db.session.get(CrawlJob, job_id)

    @staticmethod
    def update_status(job_id: int, status: str, **kwargs: Any) -> None:
        job = db.session.get(CrawlJob, job_id)
        if job:
            job.status = status
            for k, v in kwargs.items():
                setattr(job, k, v)
            if status in ("done", "failed", "stopped"):
                job.end_time = datetime.utcnow()
            db.session.commit()

    @staticmethod
    def append_log(job_id: int, message: str) -> None:
        job = db.session.get(CrawlJob, job_id)
        if job:
            existing = job.logs or ""
            job.logs = existing + f"\n{datetime.utcnow().isoformat()} {message}"
            db.session.commit()

    @staticmethod
    def get_recent(limit: int = 10) -> List[CrawlJob]:
        return (
            db.session.query(CrawlJob)
            .order_by(desc(CrawlJob.start_time))
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_running() -> Optional[CrawlJob]:
        return db.session.query(CrawlJob).filter_by(status="running").first()


# ──────────────────────────────────────────── Settings Repository


class SettingsRepository:
    @staticmethod
    def get(key: str, default: Optional[str] = None) -> Optional[str]:
        row = db.session.query(AppSetting).filter_by(key=key).first()
        return row.value if row else default

    @staticmethod
    def set(key: str, value: str) -> None:
        row = db.session.query(AppSetting).filter_by(key=key).first()
        if row:
            row.value      = value
            row.updated_at = datetime.utcnow()
        else:
            db.session.add(AppSetting(key=key, value=value))
        db.session.commit()

    @staticmethod
    def get_all() -> Dict[str, str]:
        rows = db.session.query(AppSetting).all()
        return {r.key: r.value for r in rows}