"""
SQLAlchemy ORM models.
All models inherit from a shared Base provided by flask_sqlalchemy.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

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

    # ── Thông tin hóa đơn
    invoice_no:     Mapped[str]            = mapped_column(String(100), nullable=False, index=True)
    invoice_symbol: Mapped[Optional[str]]  = mapped_column(String(50))
    invoice_form:   Mapped[Optional[str]]  = mapped_column(String(50))
    invoice_type:   Mapped[Optional[str]]  = mapped_column(String(50))
    issue_date:     Mapped[Optional[datetime]] = mapped_column(DateTime)
    status:         Mapped[Optional[str]]  = mapped_column(String(50))
    currency:       Mapped[Optional[str]]  = mapped_column(String(10))
    payment_method: Mapped[Optional[str]]  = mapped_column(String(100))

    # ── Người bán
    seller_name:      Mapped[Optional[str]] = mapped_column(String(500))
    seller_tax_code:  Mapped[Optional[str]] = mapped_column(String(50))
    seller_address:   Mapped[Optional[str]] = mapped_column(String(500))
    seller_phone:     Mapped[Optional[str]] = mapped_column(String(50))
    seller_email:     Mapped[Optional[str]] = mapped_column(String(200))
    seller_bank:      Mapped[Optional[str]] = mapped_column(String(100))
    seller_bank_name: Mapped[Optional[str]] = mapped_column(String(200))

    # ── Người mua
    buyer_name:     Mapped[Optional[str]] = mapped_column(String(500))
    buyer_tax_code: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    buyer_address:  Mapped[Optional[str]] = mapped_column(String(500))

    # ── Số tiền
    amount:         Mapped[float] = mapped_column(Float, default=0.0)
    vat_amount:     Mapped[float] = mapped_column(Float, default=0.0)
    total_amount:   Mapped[float] = mapped_column(Float, default=0.0)
    total_in_words: Mapped[Optional[str]] = mapped_column(String(500))

    # ── Thuế suất
    vat_rate: Mapped[Optional[str]] = mapped_column(String(20))  # "8%", "10%", "KCT", "KKKNT"

    # ── Mã cơ quan thuế / QR
    tax_authority_code: Mapped[Optional[str]] = mapped_column(String(100))
    qr_data:            Mapped[Optional[str]] = mapped_column(Text)

    # ── Hàng hóa / dịch vụ (JSON array)
    line_items_json: Mapped[Optional[str]] = mapped_column(Text)

    # ── Mô tả mặt hàng tổng hợp (từ bảng kê Excel)
    mat_hang: Mapped[Optional[str]] = mapped_column(Text)

    # ── Kê khai / thanh toán
    thang_ke_khai:  Mapped[Optional[str]] = mapped_column(String(20), index=True)  # "QUÝ I/2026"
    payment_note:   Mapped[Optional[str]] = mapped_column(String(500))              # Nội dung chứng từ TT
    bank_name:      Mapped[Optional[str]] = mapped_column(String(100))              # Ngân hàng

    # ── Phân loại & theo dõi
    ghi_chu:        Mapped[Optional[str]] = mapped_column(String(500), index=True)  # CHI PHÍ / THƯƠNG MẠI…
    ma_cong_trinh:  Mapped[Optional[str]] = mapped_column(String(100), index=True)
    so_hop_dong:    Mapped[Optional[str]] = mapped_column(String(200))
    ngay_hop_dong:  Mapped[Optional[str]] = mapped_column(String(50))

    # ── Liên kết HĐ đầu vào ↔ HĐ bán ra
    hd_ban_ra_tuong_ung: Mapped[Optional[str]] = mapped_column(String(200))

    # ── Đường dẫn file
    zip_path:       Mapped[Optional[str]] = mapped_column(String(1000))
    xml_data_path:  Mapped[Optional[str]] = mapped_column(String(1000))
    view_html_path: Mapped[Optional[str]] = mapped_column(String(1000))
    pdf_path:       Mapped[Optional[str]] = mapped_column(String(1000))
    metadata_path:  Mapped[Optional[str]] = mapped_column(String(1000))
    invoice_dir:    Mapped[Optional[str]] = mapped_column(String(1000))

    # ── Flags
    has_zip:  Mapped[bool] = mapped_column(Boolean, default=False)
    has_xml:  Mapped[bool] = mapped_column(Boolean, default=False)
    has_html: Mapped[bool] = mapped_column(Boolean, default=False)
    has_pdf:  Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Misc
    raw_data:   Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ── Helpers

    def get_line_items(self) -> List[Dict[str, Any]]:
        if not self.line_items_json:
            return []
        try:
            return json.loads(self.line_items_json)
        except (json.JSONDecodeError, TypeError):
            return []

    def to_dict(self) -> dict:
        return {
            "id":             self.id,
            # Hóa đơn
            "invoice_no":     self.invoice_no,
            "invoice_symbol": self.invoice_symbol,
            "invoice_form":   self.invoice_form,
            "invoice_type":   self.invoice_type,
            "issue_date":     self.issue_date.isoformat() if self.issue_date else None,
            "status":         self.status,
            "currency":       self.currency,
            "payment_method": self.payment_method,
            # Người bán
            "seller_name":      self.seller_name,
            "seller_tax_code":  self.seller_tax_code,
            "seller_address":   self.seller_address,
            "seller_phone":     self.seller_phone,
            "seller_email":     self.seller_email,
            "seller_bank":      self.seller_bank,
            "seller_bank_name": self.seller_bank_name,
            # Người mua
            "buyer_name":     self.buyer_name,
            "buyer_tax_code": self.buyer_tax_code,
            "buyer_address":  self.buyer_address,
            # Số tiền
            "amount":         self.amount,
            "vat_rate":       self.vat_rate,
            "vat_amount":     self.vat_amount,
            "total_amount":   self.total_amount,
            "total_in_words": self.total_in_words,
            # Mã CQT / QR
            "tax_authority_code": self.tax_authority_code,
            "qr_data":            self.qr_data,
            # Hàng hóa / dịch vụ
            "line_items": self.get_line_items(),
            "mat_hang":   self.mat_hang,
            # Kê khai / thanh toán
            "thang_ke_khai": self.thang_ke_khai,
            "payment_note":  self.payment_note,
            "bank_name":     self.bank_name,
            # Phân loại
            "ghi_chu":       self.ghi_chu,
            "ma_cong_trinh": self.ma_cong_trinh,
            "so_hop_dong":   self.so_hop_dong,
            "ngay_hop_dong": self.ngay_hop_dong,
            # Liên kết bán ra
            "hd_ban_ra_tuong_ung": self.hd_ban_ra_tuong_ung,
            # Paths
            "zip_path":       self.zip_path,
            "xml_data_path":  self.xml_data_path,
            "view_html_path": self.view_html_path,
            "pdf_path":       self.pdf_path,
            "metadata_path":  self.metadata_path,
            "invoice_dir":    self.invoice_dir,
            # Flags
            "has_zip":  self.has_zip,
            "has_xml":  self.has_xml,
            "has_html": self.has_html,
            "has_pdf":  self.has_pdf,
            # Timestamps
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CrawlJob(db.Model):  # type: ignore[name-defined]
    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    start_date:          Mapped[Optional[str]]      = mapped_column(String(20))
    end_date:            Mapped[Optional[str]]      = mapped_column(String(20))
    start_time:          Mapped[Optional[datetime]] = mapped_column(DateTime)
    end_time:            Mapped[Optional[datetime]] = mapped_column(DateTime)
    status:              Mapped[str]                = mapped_column(String(20), default="pending")
    total_invoices:      Mapped[int]                = mapped_column(Integer, default=0)
    downloaded_invoices: Mapped[int]                = mapped_column(Integer, default=0)
    failed_invoices:     Mapped[int]                = mapped_column(Integer, default=0)
    logs:                Mapped[Optional[str]]      = mapped_column(Text)
    error_message:       Mapped[Optional[str]]      = mapped_column(Text)
    created_at:          Mapped[datetime]           = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "id":                  self.id,
            "start_date":          self.start_date,
            "end_date":            self.end_date,
            "start_time":          self.start_time.isoformat() if self.start_time else None,
            "end_time":            self.end_time.isoformat() if self.end_time else None,
            "status":              self.status,
            "total_invoices":      self.total_invoices,
            "downloaded_invoices": self.downloaded_invoices,
            "failed_invoices":     self.failed_invoices,
            "error_message":       self.error_message,
        }


class AppSetting(db.Model):  # type: ignore[name-defined]
    __tablename__ = "app_settings"

    id:         Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    key:        Mapped[str]           = mapped_column(String(100), unique=True, nullable=False)
    value:      Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime]      = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )