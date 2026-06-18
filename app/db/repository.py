"""
Repository layer — all database interactions pass through here.
Business logic NEVER touches the ORM directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Union

from loguru import logger
from sqlalchemy import desc, func

from app.db.models import AppSetting, CrawlJob, GdtAccount, Invoice, SnapshotBatch
from app.extensions import db


# Kiểu cho tham số invoice_category — chấp nhận 1 giá trị (str) hoặc nhiều giá trị (list/tuple).
CategoryFilter = Optional[Union[str, Sequence[str]]]


def _apply_category_filter(query, invoice_category: CategoryFilter):
    """
    Áp dụng filter invoice_category lên query, hỗ trợ:
      - None / "" / []  → không filter
      - str             → filter bằng (tương thích cũ)
      - list/tuple/set  → filter IN (...) — chọn nhiều loại
    """
    if not invoice_category:
        return query
    if isinstance(invoice_category, str):
        return query.filter(Invoice.invoice_category == invoice_category)
    # list/tuple/set
    values = [v for v in invoice_category if v]
    if not values:
        return query
    if len(values) == 1:
        return query.filter(Invoice.invoice_category == values[0])
    return query.filter(Invoice.invoice_category.in_(values))


# ─────────────────────────────────────────── Invoice Repository


class InvoiceRepository:
    @staticmethod
    def upsert(data: Dict[str, Any]) -> Invoice:
        existing = (
            db.session.query(Invoice)
            .filter_by(
                invoice_no=data["invoice_no"],
                invoice_symbol=data.get("invoice_symbol"),
                invoice_form=data.get("invoice_form"),
                invoice_category=data.get("invoice_category"),
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
    def exists_by_composite_key(
        invoice_no: str,
        invoice_symbol: Optional[str] = None,
        invoice_form: Optional[str] = None,
        invoice_category: Optional[str] = None,
    ) -> Optional[Invoice]:
        """
        Lookup by (invoice_no, invoice_symbol, invoice_form, invoice_category).
        ...
        """
        query = db.session.query(Invoice).filter_by(invoice_no=invoice_no)
        if invoice_symbol is not None:
            query = query.filter_by(invoice_symbol=invoice_symbol)
        if invoice_form is not None:
            query = query.filter_by(invoice_form=invoice_form)
        if invoice_category is not None:
            query = query.filter_by(invoice_category=invoice_category)
        return query.first()

    @staticmethod
    def get_all(
        page: int = 1,
        per_page: int = 20,
        search: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        invoice_type: Optional[str] = None,
        invoice_category: CategoryFilter = None,
        account_id: Optional[int] = None,
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
        query = _apply_category_filter(query, invoice_category)
        if account_id is not None:
            query = query.filter(Invoice.account_id == account_id)
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
        account_id: Optional[int] = None,
        invoice_category: CategoryFilter = None,
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
        if account_id is not None:
            query = query.filter(Invoice.account_id == account_id)
        query = _apply_category_filter(query, invoice_category)
        return query.order_by(Invoice.issue_date).all()

    @staticmethod
    def account_breakdown() -> List[Dict[str, Any]]:
        """
        Thống kê theo từng tài khoản: số HĐ theo từng loại (category),
        tổng tiền mua vào / bán ra (gross, total_amount), tổng VAT,
        tổng doanh thu (net, amount của HĐ bán ra).
        """
        rows = (
            db.session.query(
                Invoice.account_id,
                Invoice.invoice_category,
                func.count(Invoice.id).label("count"),
                func.sum(Invoice.amount).label("amount"),
                func.sum(Invoice.vat_amount).label("vat"),
                func.sum(Invoice.total_amount).label("total"),
            )
            .group_by(Invoice.account_id, Invoice.invoice_category)
            .all()
        )

        accounts: Dict[Optional[int], Dict[str, Any]] = {}
        for r in rows:
            acc = accounts.setdefault(r.account_id, {
                "account_id": r.account_id,
                "categories": {},
                "total_invoices": 0,
                "total_purchase": 0.0,  # tổng tiền mua vào (gross)
                "total_sale": 0.0,      # tổng tiền bán ra (gross)
                "total_vat": 0.0,
                "total_revenue": 0.0,   # tổng doanh thu (net, từ HĐ bán ra)
            })
            category = r.invoice_category or "khac"
            acc["categories"][category] = {
                "count": r.count,
                "amount": r.amount or 0.0,
                "vat": r.vat or 0.0,
                "total": r.total or 0.0,
            }
            acc["total_invoices"] += r.count
            acc["total_vat"] += (r.vat or 0.0)
            if category.startswith("purchase"):
                acc["total_purchase"] += (r.total or 0.0)
            elif category.startswith("sale"):
                acc["total_sale"] += (r.total or 0.0)
                acc["total_revenue"] += (r.amount or 0.0)

        account_ids = [aid for aid in accounts if aid is not None]
        names = {}
        if account_ids:
            names = {
                a.id: a.name
                for a in db.session.query(GdtAccount).filter(GdtAccount.id.in_(account_ids)).all()
            }

        result = []
        for aid, data in accounts.items():
            data["account_name"] = names.get(aid, "Không có tài khoản")
            result.append(data)

        result.sort(key=lambda d: d["account_name"])
        return result


# ──────────────────────────────────────────── CrawlJob Repository


class CrawlJobRepository:
    @staticmethod
    def create(start_date: str, end_date: str, account_id: Optional[int] = None) -> CrawlJob:
        job = CrawlJob(
            start_date=start_date,
            end_date=end_date,
            account_id=account_id,
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

    @staticmethod
    def get_running_all() -> List[CrawlJob]:
        return db.session.query(CrawlJob).filter_by(status="running").all()


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

# ──────────────────────────────────────────── SnapshotBatch Repository


class SnapshotRepository:

    @staticmethod
    def create(label: str, invoice_ids: List[int], note: Optional[str] = None, account_id: Optional[int] = None) -> SnapshotBatch:
        """
        Tạo một batch chốt mới và gán snapshot_batch_id cho các hóa đơn được chọn.
        Chỉ update HĐ chưa chốt (snapshot_batch_id IS NULL) để tránh race condition.
        """
        batch = SnapshotBatch(label=label, note=note, invoice_count=len(invoice_ids), account_id=account_id)
        db.session.add(batch)
        db.session.flush()  # lấy batch.id trước khi update invoices

        if invoice_ids:
            db.session.query(Invoice).filter(
                Invoice.id.in_(invoice_ids),
                Invoice.snapshot_batch_id.is_(None),
            ).update({"snapshot_batch_id": batch.id}, synchronize_session="fetch")

        actual = db.session.query(Invoice).filter_by(snapshot_batch_id=batch.id).count()
        batch.invoice_count = actual
        db.session.commit()
        return batch

    @staticmethod
    def get_all(account_id: Optional[int] = None) -> List[SnapshotBatch]:
        query = db.session.query(SnapshotBatch)
        if account_id is not None:
            query = query.filter(SnapshotBatch.account_id == account_id)
        return query.order_by(SnapshotBatch.created_at.desc()).all()

    @staticmethod
    def merge_or_create(
        label: str,
        invoice_ids: List[int],
        note: Optional[str] = None,
        account_id: Optional[int] = None,
    ):
        """
        Nếu đã tồn tại batch cùng label (và cùng account) → gộp invoice_ids vào.
        Ngược lại → tạo batch mới.
        Trả về (batch, merged: bool).
        """
        query = db.session.query(SnapshotBatch).filter_by(label=label)
        if account_id is not None:
            query = query.filter(SnapshotBatch.account_id == account_id)
        existing = query.first()

        if existing:
            if invoice_ids:
                db.session.query(Invoice).filter(
                    Invoice.id.in_(invoice_ids),
                    Invoice.snapshot_batch_id.is_(None),
                ).update({"snapshot_batch_id": existing.id}, synchronize_session="fetch")
            existing.invoice_count = (
                db.session.query(Invoice).filter_by(snapshot_batch_id=existing.id).count()
            )
            if note and not existing.note:
                existing.note = note
            db.session.commit()
            return existing, True
        else:
            return SnapshotRepository.create(label=label, invoice_ids=invoice_ids, note=note, account_id=account_id), False

    @staticmethod
    def get_by_id(batch_id: int) -> Optional[SnapshotBatch]:
        return db.session.get(SnapshotBatch, batch_id)

    @staticmethod
    def delete(batch_id: int) -> int:
        """Xoá batch và giải phóng HĐ về NULL. Trả về số HĐ bị giải phóng."""
        released = db.session.query(Invoice).filter_by(
            snapshot_batch_id=batch_id
        ).update({"snapshot_batch_id": None}, synchronize_session="fetch")
        db.session.query(SnapshotBatch).filter_by(id=batch_id).delete()
        db.session.commit()
        return released

    @staticmethod
    def get_new_invoices(
        page: int = 1,
        per_page: int = 50,
        search: Optional[str] = None,
        invoice_category: CategoryFilter = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        account_id: Optional[int] = None,
    ):
        """HĐ chưa chốt (snapshot_batch_id IS NULL), có filter + phân trang."""
        from datetime import timedelta
        query = db.session.query(Invoice).filter(Invoice.snapshot_batch_id.is_(None))
        if account_id is not None:
            query = query.filter(Invoice.account_id == account_id)
        if search:
            like = f"%{search}%"
            query = query.filter(
                Invoice.invoice_no.ilike(like)
                | Invoice.seller_name.ilike(like)
                | Invoice.buyer_name.ilike(like)
                | Invoice.seller_tax_code.ilike(like)
            )
        query = _apply_category_filter(query, invoice_category)
        if start_date:
            query = query.filter(Invoice.issue_date >= start_date)
        if end_date:
            query = query.filter(Invoice.issue_date < end_date + timedelta(days=1))
        query = query.order_by(desc(Invoice.created_at))
        return query.paginate(page=page, per_page=per_page, error_out=False)

    @staticmethod
    def count_new(account_id: Optional[int] = None) -> int:
        query = db.session.query(func.count(Invoice.id)).filter(
            Invoice.snapshot_batch_id.is_(None)
        )
        if account_id is not None:
            query = query.filter(Invoice.account_id == account_id)
        return query.scalar() or 0

    @staticmethod
    def get_batch_invoices(
        batch_id: int,
        page: int = 1,
        per_page: int = 50,
        search: Optional[str] = None,
        invoice_category: CategoryFilter = None,
    ):
        query = db.session.query(Invoice).filter_by(snapshot_batch_id=batch_id)
        if search:
            like = f"%{search}%"
            query = query.filter(
                Invoice.invoice_no.ilike(like)
                | Invoice.seller_name.ilike(like)
                | Invoice.buyer_name.ilike(like)
            )
        query = _apply_category_filter(query, invoice_category)
        query = query.order_by(Invoice.issue_date)
        return query.paginate(page=page, per_page=per_page, error_out=False)

    @staticmethod
    def get_new_for_export(
        search: Optional[str] = None,
        invoice_category: CategoryFilter = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        account_id: Optional[int] = None,
    ) -> List[Invoice]:
        from datetime import timedelta
        query = db.session.query(Invoice).filter(Invoice.snapshot_batch_id.is_(None))
        if account_id is not None:
            query = query.filter(Invoice.account_id == account_id)
        if search:
            like = f"%{search}%"
            query = query.filter(
                Invoice.invoice_no.ilike(like)
                | Invoice.seller_name.ilike(like)
                | Invoice.buyer_name.ilike(like)
            )
        query = _apply_category_filter(query, invoice_category)
        if start_date:
            query = query.filter(Invoice.issue_date >= start_date)
        if end_date:
            query = query.filter(Invoice.issue_date < end_date + timedelta(days=1))
        return query.order_by(Invoice.issue_date).all()

    @staticmethod
    def get_batch_for_export(batch_id: int) -> List[Invoice]:
        return (
            db.session.query(Invoice)
            .filter_by(snapshot_batch_id=batch_id)
            .order_by(Invoice.issue_date)
            .all()
        )

# ──────────────────────────────────────────── GdtAccount Repository


class GdtAccountRepository:

    @staticmethod
    def create(name: str, username: str, password: str,
               tax_code: Optional[str] = None,
               note: Optional[str] = None,
               company_name: Optional[str] = None,
               company_tax_code: Optional[str] = None,
               company_address: Optional[str] = None,
               company_report_title: Optional[str] = None) -> GdtAccount:
        acct = GdtAccount(
            name=name, username=username, password=password,
            tax_code=tax_code, note=note, is_active=True,
            company_name=company_name,
            company_tax_code=company_tax_code,
            company_address=company_address,
            company_report_title=company_report_title,
        )
        db.session.add(acct)
        db.session.commit()
        return acct

    @staticmethod
    def get_all(active_only: bool = False) -> List[GdtAccount]:
        q = db.session.query(GdtAccount)
        if active_only:
            q = q.filter_by(is_active=True)
        return q.order_by(GdtAccount.name).all()

    @staticmethod
    def get_by_id(account_id: int) -> Optional[GdtAccount]:
        return db.session.get(GdtAccount, account_id)

    @staticmethod
    def update(account_id: int, **kwargs: Any) -> Optional[GdtAccount]:
        acct = db.session.get(GdtAccount, account_id)
        if not acct:
            return None
        allowed = {
            "name", "username", "password", "tax_code", "note", "is_active",
            "company_name", "company_tax_code", "company_address", "company_report_title",
        }
        for k, v in kwargs.items():
            if k in allowed:
                setattr(acct, k, v)
        db.session.commit()
        return acct

    @staticmethod
    def delete(account_id: int) -> bool:
        acct = db.session.get(GdtAccount, account_id)
        if not acct:
            return False
        db.session.delete(acct)
        db.session.commit()
        return True