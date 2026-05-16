"""
Core crawler engine — orchestrates the full crawl lifecycle:
login → search → paginate → download per invoice → persist to DB.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from loguru import logger
from playwright.async_api import Page

from app.automation.browser import BrowserManager
from app.automation.invoice_detail import process_invoice_row
from app.automation.invoice_export import export_invoice_list
from app.automation.invoice_search import (
    get_total_rows,
    navigate_to_search,
    perform_search,
    set_date_filter,
    set_page_size,
)
from app.automation.login import attempt_login
from app.config import Config
from app.db.repository import CrawlJobRepository, InvoiceRepository
from app.services.xml_service import XmlService


class CrawlerEngine:
    """
    Stateful crawler engine.
    One instance per crawl job; không tái sử dụng giữa các job.
    """

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
        self.job_id = job_id
        self.username = username
        self.password = password
        self.start_date = start_date
        self.end_date = end_date
        self.emit_fn = emit_fn
        self.emit_captcha_fn = emit_captcha_fn
        self.captcha_event = captcha_event
        self.get_captcha_answer = get_captcha_answer
        self.app = app
        self._stop_requested = False
        self._browser = BrowserManager()

    # ------------------------------------------------------------------ public

    async def run(self) -> None:
        """Entry point — chạy toàn bộ vòng đời crawl."""
        self._update_job(status="running")
        self._emit("🚀 Crawler started.")

        page: Optional[Page] = None
        try:
            ctx = await self._browser.start()
            page = await ctx.new_page()

            # Login (có captcha manual)
            logged_in = await attempt_login(
                page=page,
                username=self.username,
                password=self.password,
                emit_fn=self.emit_fn,
                emit_captcha_fn=self.emit_captcha_fn,
                captcha_event=self.captcha_event,
                get_captcha_answer=self.get_captcha_answer,
            )
            if not logged_in:
                raise RuntimeError("Login failed after all attempts.")

            # Navigate to search
            ok = await navigate_to_search(page, emit_fn=self.emit_fn)
            if not ok:
                raise RuntimeError("Failed to reach search page.")

            await set_date_filter(page, self.start_date, self.end_date, emit_fn=self.emit_fn)

            ok = await perform_search(page, emit_fn=self.emit_fn)
            if not ok:
                raise RuntimeError("Search returned no results or failed.")

            await set_page_size(page, Config.CRAWLER_PAGE_SIZE)
            await export_invoice_list(page, emit_fn=self.emit_fn)
            await self._iterate_all_pages(page)

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

    # ----------------------------------------------------------------- private

    async def _iterate_all_pages(self, page: Page) -> None:
        page_num = 1
        total_processed = 0
        total_failed = 0

        while not self._stop_requested:
            self._emit(f"📄 Processing page {page_num}…")
            rows = page.locator("tbody.ant-table-tbody tr.ant-table-row")
            count = await rows.count()

            if count == 0:
                self._emit("No rows found on page — stopping pagination.")
                break

            self._emit(f"  Found {count} rows on page {page_num}.")

            for i in range(count):
                if self._stop_requested:
                    break

                row = rows.nth(i)
                with self._app_context():
                    result = await process_invoice_row(page, row, i, emit_fn=self.emit_fn)

                if result:
                    await self._persist_invoice(result)
                    total_processed += 1
                else:
                    total_failed += 1

                self._update_job(
                    total_invoices=total_processed + total_failed,
                    downloaded_invoices=total_processed,
                    failed_invoices=total_failed,
                )
                await asyncio.sleep(Config.CRAWLER_DELAY_MS / 1000)

            if not await self._go_to_next_page(page):
                self._emit("No more pages.")
                break

            page_num += 1
            await asyncio.sleep(1)

        self._emit(f"Iteration complete — processed={total_processed}, failed={total_failed}")

    async def _go_to_next_page(self, page: Page) -> bool:
        try:
            next_btn = page.locator(
                "li.ant-pagination-next:not(.ant-pagination-disabled) a, "
                "button.ant-pagination-item-link[aria-label='Next Page']:not([disabled])"
            ).first
            if await next_btn.count() == 0:
                return False
            await next_btn.click()
            await page.wait_for_load_state("networkidle", timeout=15_000)
            await asyncio.sleep(1)
            return True
        except Exception as exc:
            logger.debug("Next page: {}", exc)
            return False

    async def _persist_invoice(self, result: Dict[str, Any]) -> None:
        with self._app_context():
            try:
                xml_path = result.get("xml_path")
                if xml_path:
                    from pathlib import Path
                    xml_file = Path(xml_path)
                    if xml_file.exists():
                        xml_data = XmlService.parse_invoice_xml(xml_file)
                        if xml_data:
                            result.update(xml_data)

                issue_date = result.get("issue_date")
                if isinstance(issue_date, str):
                    from app.utils.dates import parse_date
                    issue_date = parse_date(issue_date)

                db_data = {
                    "invoice_no":       result.get("invoice_no", ""),
                    "invoice_symbol":   result.get("invoice_symbol"),
                    "seller_name":      result.get("seller_name"),
                    "seller_tax_code":  result.get("seller_tax_code"),
                    "buyer_name":       result.get("buyer_name"),
                    "buyer_tax_code":   result.get("buyer_tax_code"),
                    "issue_date":       datetime.combine(issue_date, datetime.min.time()) if issue_date else None,
                    "amount":           result.get("amount", 0.0),
                    "vat_amount":       result.get("vat_amount", 0.0),
                    "total_amount":     result.get("total_amount", 0.0),
                    "xml_path":         result.get("xml_path"),
                    "pdf_path":         result.get("pdf_path"),
                    "metadata_path":    result.get("metadata_path"),
                    "has_xml":          result.get("has_xml", False),
                    "has_pdf":          result.get("has_pdf", False),
                    "status":           "downloaded",
                }
                InvoiceRepository.upsert(db_data)
                logger.debug("Persisted invoice #{}", db_data["invoice_no"])

            except Exception as exc:
                logger.error("Failed to persist invoice: {}", exc)

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