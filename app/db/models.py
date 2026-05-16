"""
SQLAlchemy ORM models.
All models inherit from a shared Base provided by flask_sqlalchemy.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class Invoice(db.Model):  # type: ignore[name-defined]
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_no: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    invoice_symbol: Mapped[Optional[str]] = mapped_column(String(50))
    seller_name: Mapped[Optional[str]] = mapped_column(String(500))
    seller_tax_code: Mapped[Optional[str]] = mapped_column(String(50))
    buyer_name: Mapped[Optional[str]] = mapped_column(String(500))
    buyer_tax_code: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    issue_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    vat_amount: Mapped[float] = mapped_column(Float, default=0.0)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    invoice_type: Mapped[Optional[str]] = mapped_column(String(50))  # PURCHASE / SALE
    status: Mapped[Optional[str]] = mapped_column(String(50))
    xml_path: Mapped[Optional[str]] = mapped_column(String(1000))
    pdf_path: Mapped[Optional[str]] = mapped_column(String(1000))
    metadata_path: Mapped[Optional[str]] = mapped_column(String(1000))
    has_xml: Mapped[bool] = mapped_column(Boolean, default=False)
    has_pdf: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_data: Mapped[Optional[str]] = mapped_column(Text)  # JSON blob from portal
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "invoice_no": self.invoice_no,
            "invoice_symbol": self.invoice_symbol,
            "seller_name": self.seller_name,
            "seller_tax_code": self.seller_tax_code,
            "buyer_name": self.buyer_name,
            "buyer_tax_code": self.buyer_tax_code,
            "issue_date": self.issue_date.isoformat() if self.issue_date else None,
            "amount": self.amount,
            "vat_amount": self.vat_amount,
            "total_amount": self.total_amount,
            "invoice_type": self.invoice_type,
            "status": self.status,
            "xml_path": self.xml_path,
            "pdf_path": self.pdf_path,
            "has_xml": self.has_xml,
            "has_pdf": self.has_pdf,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CrawlJob(db.Model):  # type: ignore[name-defined]
    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    start_date: Mapped[Optional[str]] = mapped_column(String(20))  # dd/MM/yyyy
    end_date: Mapped[Optional[str]] = mapped_column(String(20))
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending | running | done | failed | stopped
    total_invoices: Mapped[int] = mapped_column(Integer, default=0)
    downloaded_invoices: Mapped[int] = mapped_column(Integer, default=0)
    failed_invoices: Mapped[int] = mapped_column(Integer, default=0)
    logs: Mapped[Optional[str]] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "status": self.status,
            "total_invoices": self.total_invoices,
            "downloaded_invoices": self.downloaded_invoices,
            "failed_invoices": self.failed_invoices,
            "error_message": self.error_message,
        }


class AppSetting(db.Model):  # type: ignore[name-defined]
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    value: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )