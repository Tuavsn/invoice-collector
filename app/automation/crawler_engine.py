"""
Core crawler engine — orchestrates the full crawl lifecycle:
login → search → paginate → download per invoice → persist to DB.

Thứ tự crawl (2 cấp tab):
  1. Bán ra
       └─ Hóa đơn điện tử        → paginate + export
       └─ Máy tính tiền           → paginate + export
  2. Mua vào
       └─ Hóa đơn điện tử        → paginate + export
       └─ Máy tính tiền           → paginate + export

One instance per crawl job; không tái sử dụng giữa các job.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from loguru import logger
from playwright.async_api import Page

from app.automation.browser import BrowserManager
from app.automation.invoice_detail import SKIPPED, process_invoice_row
from app.automation.invoice_export import export_invoice_list
from app.automation.invoice_search import (
    CRAWL_PLAN,
    MainTabConfig,
    SubTabConfig,
    get_total_rows,
    navigate_to_search,
    perform_search,
    set_date_filter,
    set_page_size,
    switch_to_main_tab,
    switch_to_sub_tab,
)
from app.automation.login import ensure_logged_in
from app.config import Config
from app.db.repository import CrawlJobRepository, InvoiceRepository


class CrawlerEngine:
    """Stateful crawler engine. One instance per crawl job."""

    def __init__(
        self,
        job_id: int,
        username: str,
        password: str,
        start_date: str,
        end_date: str,
        emit_fn: Optional[Callable[[str], None]] = None,
        emit_captcha_fn: Optional[Callable[[str], None]] = None,
        captcha_event: Optional[threading.Event] = None,
        get_captcha_answer: Optional[Callable[[], str]] = None,
        app=None,
    ) -> None:
        self.job_id             = job_id
        self.username           = username
        self.password           = password
        self.start_date         = start_date
        self.end_date           = end_date
        self.emit_fn            = emit_fn
        self.emit_captcha_fn    = emit_captcha_fn
        self.captcha_event      = captcha_event
        self.get_captcha_answer = get_captcha_answer
        self.app                = app
        self._stop_requested    = False
        self._browser           = BrowserManager()

    # ─────────────────────────────────────────────────────────────── public

    async def run(self) -> None:
        """Entry point — chạy toàn bộ vòng đời crawl theo CRAWL_PLAN."""
        self._update_job(status="running")
        self._emit("🚀 Crawler started.")

        page: Optional[Page] = None
        try:
            ctx  = await self._browser.start()
            page = await ctx.new_page()

            # ── 1. Đăng nhập
            logged_in = await ensure_logged_in(
                page=page,
                browser_manager=self._browser,
                username=self.username,
                password=self.password,
                emit_fn=self.emit_fn,
                emit_captcha_fn=self.emit_captcha_fn,
                captcha_event=self.captcha_event,
                get_captcha_answer=self.get_captcha_answer,
            )
            if not logged_in:
                raise RuntimeError("Login failed after all attempts.")

            # ── 2. Điều hướng đến trang tìm kiếm
            ok = await navigate_to_search(page, emit_fn=self.emit_fn)
            if not ok:
                raise RuntimeError("Failed to reach search page.")

            # ── 3. Lần lượt crawl từng main tab theo CRAWL_PLAN
            for main_tab in CRAWL_PLAN:
                if self._stop_requested:
                    break
                await self._crawl_main_tab(page, main_tab)

            self._update_job(status="done")
            self._emit("✅ Crawl completed successfully.")

        except asyncio.CancelledError:
            self._update_job(status="stopped")
            self._emit("⛔ Crawl stopped by user.")

        except Exception as exc:
            logger.exception("Crawler engine error: {}", exc)
            self._update_job(status="failed", error_message=str(exc))
            self._emit(f"❌ Crawl failed: {exc}")
            if page:
                await self._browser.screenshot(page, f"error_job_{self.job_id}")

        finally:
            await self._browser.close()

    def request_stop(self) -> None:
        self._stop_requested = True
        self._emit("Stop requested — will halt after current invoice…")

    # ─────────────────────────────────────────────────────────────── crawl flow

    async def _crawl_main_tab(self, page: Page, main_tab: MainTabConfig) -> None:
        """
        Xử lý một tab tìm kiếm cấp 1 (Bán ra / Mua vào):
          1. Click tab cấp 1
          2. Set date filter
          3. Với mỗi sub-tab: search → set page size → export list → iterate pages
        """
        self._emit(f"── Tab cấp 1: {main_tab.name} ──")

        # Click tab cấp 1
        ok = await switch_to_main_tab(page, main_tab, emit_fn=self.emit_fn)
        if not ok:
            self._emit(f"⚠️  Bỏ qua tab '{main_tab.name}' (không tìm thấy).")
            return

        # Set date filter một lần cho cả main tab
        await set_date_filter(
            page,
            self.start_date,
            self.end_date,
            emit_fn=self.emit_fn,
            main_tab=main_tab,
        )

        # Lần lượt từng sub-tab
        for sub_tab in main_tab.sub_tabs:
            if self._stop_requested:
                break
            await self._crawl_sub_tab(page, main_tab, sub_tab)

        await page.reload(wait_until="networkidle")

    async def _crawl_sub_tab(
        self,
        page: Page,
        main_tab: MainTabConfig,
        sub_tab: SubTabConfig,
    ) -> None:
        """
        Xử lý một sub-tab cấp 2:
          1. Click sub-tab
          2. Perform search
          3. Set page size
          4. Export invoice list
          5. Iterate all pages
        """
        self._emit(f"  ── Sub-tab: {sub_tab.name} ──")

        # Click sub-tab cấp 2
        ok = await switch_to_sub_tab(page, main_tab, sub_tab, emit_fn=self.emit_fn)
        if not ok:
            self._emit(f"  ⚠️  Bỏ qua sub-tab '{sub_tab.name}' (không tìm thấy).")
            return

        # Search
        ok = await perform_search(page, emit_fn=self.emit_fn, sub_tab=sub_tab)
        if not ok:
            self._emit(f"  ⚠️  Search thất bại cho '{sub_tab.name}' — bỏ qua.")
            return

        # Chọn số lượng hiển thị mỗi trang
        await set_page_size(page, Config.CRAWLER_PAGE_SIZE)

        # Xuất danh sách hóa đơn (export toàn bộ list trước khi đi từng row)
        await export_invoice_list(page, emit_fn=self.emit_fn)

        # Iterate tất cả trang, chọn từng row và xuất XML
        await self._iterate_all_pages(page, sub_tab)

    # ─────────────────────────────────────────────────────────────── pagination

    async def _iterate_all_pages(self, page: Page, sub_tab: SubTabConfig) -> None:
        """Duyệt qua toàn bộ trang kết quả của sub_tab hiện tại."""
        page_num        = 1
        total_processed = 0
        total_failed    = 0
        total_skipped   = 0

        while not self._stop_requested:
            self._emit(f"  📄 [{sub_tab.name}] Processing page {page_num}…")
            rows  = page.locator("tbody.ant-table-tbody tr.ant-table-row")
            count = await rows.count()

            if count == 0:
                self._emit(f"  No rows found on page {page_num} — stopping pagination.")
                break

            self._emit(f"  Found {count} rows on page {page_num}.")

            for i in range(count):
                if self._stop_requested:
                    break

                row = rows.nth(i)
                with self._app_context():
                    result = await process_invoice_row(
                        page, row, i,
                        emit_fn=self.emit_fn,
                        invoice_category=sub_tab.invoice_type,
                    )

                if result is SKIPPED:
                    total_skipped += 1
                elif result:
                    await self._persist_invoice(result)
                    total_processed += 1
                else:
                    total_failed += 1

                self._update_job(
                    total_invoices=total_processed + total_failed + total_skipped,
                    downloaded_invoices=total_processed,
                    failed_invoices=total_failed,
                )
                await asyncio.sleep(Config.CRAWLER_DELAY_MS / 1000)

            if not await self._go_to_next_page(page):
                self._emit(f"  No more pages for [{sub_tab.name}].")
                break

            page_num += 1

        self._emit(
            f"  [{sub_tab.name}] Done — "
            f"processed={total_processed}, skipped={total_skipped}, failed={total_failed}"
        )

    async def _go_to_next_page(self, page: Page) -> bool:
        try:
            next_btn = page.locator(
                "li.ant-pagination-next:not(.ant-pagination-disabled) a, "
                "button.ant-pagination-item-link[aria-label='Next Page']:not([disabled])"
            ).first
            if await next_btn.count() == 0:
                return False

            # Record current first-row text to detect page change
            first_row = page.locator("tbody.ant-table-tbody tr.ant-table-row").first
            before_text = await first_row.inner_text() if await first_row.count() > 0 else ""

            await next_btn.click()

            # Wait for table rows to change (indicates new page loaded)
            try:
                await page.wait_for_function(
                    f"""() => {{
                        const row = document.querySelector('tbody.ant-table-tbody tr.ant-table-row');
                        return row && row.innerText !== {json.dumps(before_text)};
                    }}""",
                    timeout=15_000,
                )
            except Exception:
                # Fallback: wait for networkidle
                await page.wait_for_load_state("networkidle", timeout=15_000)

            return True
        except Exception as exc:
            logger.debug("Next page navigation error: {}", exc)
            return False

    # ─────────────────────────────────────────────────────────────── persistence

    async def _persist_invoice(
        self,
        result: Dict[str, Any],
    ) -> None:
        """
        Lưu invoice vào DB từ result dict của process_invoice_row().
        Tất cả field đã chuẩn hóa kiểu dữ liệu tại invoice_detail.py + XmlService.
        Không parse lại bất kỳ dữ liệu nào ở đây.
        invoice_category được lấy từ result["invoice_category"] (đã gán bởi process_invoice_row).
        """
        with self._app_context():
            try:
                # ── Chuẩn hóa issue_date → datetime (nếu chưa phải)
                issue_date = result.get("issue_date")
                if isinstance(issue_date, str) and issue_date:
                    from app.utils.dates import parse_date
                    issue_date = parse_date(issue_date)

                issue_datetime = (
                    datetime.combine(issue_date, datetime.min.time())
                    if issue_date else None
                )

                # ── Serialize line_items → JSON string
                line_items      = result.get("line_items", [])
                line_items_json = (
                    json.dumps(line_items, ensure_ascii=False)
                    if line_items else None
                )

                # ── Serialize vat_breakdown → JSON string
                vat_breakdown      = result.get("vat_breakdown", [])
                vat_breakdown_json = (
                    json.dumps(vat_breakdown, ensure_ascii=False)
                    if vat_breakdown else None
                )

                db_data: Dict[str, Any] = {
                    # ── Thông tin hóa đơn
                    "invoice_no":     result.get("invoice_no", ""),
                    "invoice_symbol": result.get("invoice_symbol"),
                    "invoice_form":   result.get("invoice_form"),
                    # invoice_type = THDon từ XML (loại HĐ theo chuẩn GDT)
                    "invoice_type":   result.get("invoice_type"),
                    # invoice_category = phân loại crawl từ SubTabConfig (sale_einvoice / purchase_einvoice / ...)
                    "invoice_category": result.get("invoice_category"),
                    "issue_date":     issue_datetime,
                    "status":         result.get("status", "downloaded"),
                    "currency":       result.get("currency"),
                    "exchange_rate":  result.get("exchange_rate"),
                    "payment_method": result.get("payment_method"),
                    # ── XML meta
                    "xml_version":       result.get("xml_version"),
                    "software_tax_code": result.get("software_tax_code"),
                    "is_adjustment":     result.get("is_adjustment"),
                    "portal_link":       result.get("portal_link"),
                    "fkey":              result.get("fkey"),
                    # ── Ngày ký
                    "seller_signing_time": result.get("seller_signing_time"),
                    "tax_signing_time":    result.get("tax_signing_time"),
                    # ── Người bán
                    "seller_name":      result.get("seller_name"),
                    "seller_tax_code":  result.get("seller_tax_code"),
                    "seller_address":   result.get("seller_address"),
                    "seller_phone":     result.get("seller_phone"),
                    "seller_email":     result.get("seller_email"),
                    "seller_bank":      result.get("seller_bank"),
                    "seller_bank_name": result.get("seller_bank_name"),
                    "seller_fax":       result.get("seller_fax"),
                    "seller_website":   result.get("seller_website"),
                    # ── Người mua
                    "buyer_name":      result.get("buyer_name"),
                    "buyer_tax_code":  result.get("buyer_tax_code"),
                    "buyer_address":   result.get("buyer_address"),
                    "buyer_phone":     result.get("buyer_phone"),
                    "buyer_email":     result.get("buyer_email"),
                    "buyer_bank":      result.get("buyer_bank"),
                    "buyer_bank_name": result.get("buyer_bank_name"),
                    # ── Số tiền
                    "amount":             result.get("amount",       0.0),
                    "vat_rate":           result.get("vat_rate"),
                    "vat_amount":         result.get("vat_amount",   0.0),
                    "total_amount":       result.get("total_amount", 0.0),
                    "total_in_words":     result.get("total_in_words"),
                    "discount_amount":    result.get("discount_amount"),
                    "non_taxable_amount": result.get("non_taxable_amount"),
                    "other_amount":       result.get("other_amount"),
                    # ── Thuế chi tiết
                    "vat_breakdown_json": vat_breakdown_json,
                    # ── Mã CQT / QR
                    "tax_authority_code": result.get("tax_authority_code"),
                    "qr_data":            result.get("qr_data"),
                    # ── Hàng hóa / dịch vụ
                    "line_items_json": line_items_json,
                    # ── Đường dẫn file
                    "zip_path":       result.get("zip_path"),
                    "xml_data_path":  result.get("xml_data_path"),
                    "view_html_path": result.get("view_html_path"),
                    "pdf_path":       result.get("pdf_path"),
                    "metadata_path":  result.get("metadata_path"),
                    "invoice_dir":    result.get("invoice_dir"),
                    # ── Flags
                    "has_zip":  result.get("has_zip",  False),
                    "has_xml":  result.get("has_xml",  False),
                    "has_html": result.get("has_html", False),
                    "has_pdf":  result.get("has_pdf",  False),
                }

                InvoiceRepository.upsert(db_data)
                logger.debug(
                    "Persisted invoice #{} category={} type_gdt={} ({} line items, {} vat brackets)",
                    db_data["invoice_no"],
                    db_data["invoice_category"],
                    db_data["invoice_type"],
                    len(line_items),
                    len(vat_breakdown),
                )

            except Exception as exc:
                logger.error("Failed to persist invoice: {}", exc)

    # ─────────────────────────────────────────────────────────────── helpers

    def _update_job(self, **kwargs: Any) -> None:
        with self._app_context():
            try:
                CrawlJobRepository.update_status(
                    self.job_id, kwargs.pop("status", "running"), **kwargs
                )
            except Exception as exc:
                logger.warning("Job update failed: {}", exc)

    def _emit(self, message: str) -> None:
        logger.info("[Job {}] {}", self.job_id, message)
        if self.emit_fn:
            self.emit_fn(message)
        with self._app_context():
            try:
                CrawlJobRepository.append_log(self.job_id, message)
            except Exception:
                pass

    def _app_context(self):
        if self.app:
            return self.app.app_context()

        class _Noop:
            def __enter__(self): return self
            def __exit__(self, *a): pass

        return _Noop()