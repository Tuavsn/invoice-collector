from __future__ import annotations

import asyncio
import json
import re
import zipfile
from datetime import datetime, date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import httpx
from loguru import logger
from playwright.async_api import Page

from app.automation.browser import BrowserManager
from app.automation.login import ensure_logged_in
from app.db.repository import CrawlJobRepository, InvoiceRepository
from app.services.xml_service import XmlService
from app.utils.paths import ensure_invoice_dir

GDT_BASE     = "https://hoadondientu.gdt.gov.vn"
_PAGE_SIZE   = 50
_RETRY       = 3
_CONCURRENCY = 3
_DELAY       = 0.1

_CRAWL_PLAN: List[Tuple[str, str, Optional[int], str]] = [
    ("sale_einvoice",     "/api/query/invoices/sold",         None, "Bán ra - HĐ điện tử"),
    ("sale_pos",          "/api/sco-query/invoices/sold",     None, "Bán ra - HĐ máy tính tiền"),
    ("purchase_einvoice", "/api/query/invoices/purchase",     5,    "Mua vào - Đã cấp HĐ - điện tử"),
    ("purchase_pos",      "/api/sco-query/invoices/purchase", 5,    "Mua vào - Đã cấp HĐ - máy tính tiền"),
    ("purchase_einvoice", "/api/query/invoices/purchase",     6,    "Mua vào - Cục thuế không nhận mã"),
    ("purchase_einvoice", "/api/query/invoices/purchase",     8,    "Mua vào - Cục thuế đã nhận có mã"),
]

_DB_FIELDS = [
    "invoice_no", "invoice_symbol", "invoice_form", "invoice_type", "invoice_category",
    "status", "currency", "exchange_rate", "payment_method",
    "xml_version", "software_tax_code", "is_adjustment", "portal_link", "fkey",
    "seller_signing_time", "tax_signing_time",
    "seller_name", "seller_tax_code", "seller_address", "seller_phone", "seller_email",
    "seller_bank", "seller_bank_name", "seller_fax", "seller_website",
    "buyer_name", "buyer_tax_code", "buyer_address", "buyer_phone", "buyer_email",
    "buyer_bank", "buyer_bank_name",
    "amount", "vat_rate", "vat_amount", "total_amount", "total_in_words",
    "discount_amount", "non_taxable_amount", "other_amount",
    "tax_authority_code", "qr_data",
    "zip_path", "xml_data_path", "view_html_path", "pdf_path",
    "metadata_path", "invoice_dir",
    "has_zip", "has_xml", "has_html", "has_pdf",
]

class ZipExtractResult:
    __slots__ = ("data_xml_path", "view_html_path", "all_paths")

    def __init__(self) -> None:
        self.data_xml_path:  Optional[Path] = None
        self.view_html_path: Optional[Path] = None
        self.all_paths: List[Path] = []

class GdtApiClient:
    def __init__(self, jwt: str) -> None:
        self._jwt    = jwt
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "GdtApiClient":
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._jwt}"}, timeout=30
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()

    def update_jwt(self, jwt: str) -> None:
        self._jwt = jwt
        if self._client:
            self._client.headers["Authorization"] = f"Bearer {jwt}"

    async def _get(self, url: str, label: str = "") -> Optional[bytes]:
        for attempt in range(_RETRY):
            try:
                r = await self._client.get(url)
            except Exception as exc:
                logger.warning("[{}] GET error (attempt {}): {}", label, attempt + 1, exc)
                await asyncio.sleep(1)
                continue

            if r.status_code == 404:
                return None
            if r.status_code == 401:
                raise PermissionError("401")
            if r.status_code >= 400:
                await asyncio.sleep(1)
                continue
            return r.content
        return None

    async def list_invoices(
        self,
        path: str,
        start_date: str,
        end_date: str,
        ttxly: Optional[int] = None,
        label: str = "",
        log_buffer: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        def _log(msg: str) -> None:
            if log_buffer is not None:
                log_buffer.append(msg)
            else:
                logger.debug(msg)

        search = (
            f"tdlap=ge={_to_gdt_dt(start_date, '00:00:00')};"
            f"tdlap=le={_to_gdt_dt(end_date, '23:59:59')}"
        )
        if ttxly is not None:
            search += f";ttxly=={ttxly}"

        all_items: List[Dict[str, Any]] = []
        seen_keys: set = set()
        page  = 0
        state: Optional[str] = None
        expected_total: Optional[int] = None
        _MAX_PAGES = 200

        while page < _MAX_PAGES:
            params: Dict[str, Any] = {"sort": "tdlap:desc", "size": _PAGE_SIZE, "search": search}
            if state:
                params["state"] = state
            url  = GDT_BASE + path + "?" + urlencode(params)
            raw  = await self._get(url, label=label)
            if not raw:
                break

            data       = json.loads(raw.decode("utf-8"))
            items      = data.get("datas") or []
            total      = int(data.get("total") or 0)
            next_state = data.get("state")

            if expected_total is None:
                expected_total = total
            if not items:
                break

            new_items  = [it for it in items if _raw_item_key(it) not in seen_keys]
            for it in new_items:
                seen_keys.add(_raw_item_key(it))
            all_items.extend(new_items)

            _log(f"[{label}] page {page}: +{len(new_items)} (accum={len(all_items)}, total={expected_total})")

            if len(items) < _PAGE_SIZE or len(items) - len(new_items) == len(items):
                break
            if not next_state:
                break

            state = next_state
            page += 1
        else:
            logger.warning("[{}] Reached _MAX_PAGES={}", label, _MAX_PAGES)

        return all_items

    async def export_xml(self, nbmst: str, khhdon: str, shdon: str, khmshdon: str) -> Optional[bytes]:
        params = {"nbmst": nbmst, "khhdon": khhdon, "shdon": shdon, "khmshdon": khmshdon}
        return await self._get(GDT_BASE + "/api/query/invoices/export-xml?" + urlencode(params))

    async def get_detail(self, nbmst: str, khhdon: str, shdon: str, khmshdon: str) -> Optional[bytes]:
        params = {"nbmst": nbmst, "khhdon": khhdon, "shdon": shdon, "khmshdon": khmshdon}
        return await self._get(GDT_BASE + "/api/query/invoices/detail?" + urlencode(params))

def extract_invoice_zip(zip_path: Path, dest_dir: Path) -> ZipExtractResult:
    result = ZipExtractResult()
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not zip_path.exists():
        logger.warning("ZIP not found: {}", zip_path)
        return result

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                if member.filename.endswith("/"):
                    continue
                content  = zf.read(member.filename)
                out_path = dest_dir / Path(member.filename).name
                out_path.write_bytes(content)
                result.all_paths.append(out_path)

                ext = Path(member.filename).suffix.lower()
                if ext == ".xml":
                    try:
                        root      = ET.fromstring(content)
                        local_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
                        if local_tag == "HDon":
                            result.data_xml_path = out_path
                    except ET.ParseError:
                        logger.warning("XML parse error, skipped: {}", out_path.name)
                elif ext in (".html", ".htm"):
                    result.view_html_path = out_path

    except zipfile.BadZipFile:
        logger.error("File ZIP không hợp lệ: {}", zip_path.name)
    except Exception as exc:
        logger.exception("Lỗi khi giải nén {}: {}", zip_path.name, exc)

    return result

def _raw_item_key(raw: Dict[str, Any]) -> tuple:
    return (raw.get("shdon"), raw.get("khhdon"), raw.get("khmshdon"),
            raw.get("nbmst"), raw.get("nmmst"), raw.get("ttxly"))


def _dedup_key(inv: Dict[str, Any], category: str) -> tuple:
    return (inv["invoice_no"], inv["invoice_symbol"] or "", inv["invoice_form"] or "",
            category, inv["total_amount"])


def _map_inv(inv: Dict[str, Any]) -> Dict[str, Any]:
    def _f(key: str) -> float:
        v = inv.get(key)
        return float(v) if v is not None else 0.0

    def _s(key: str) -> str:
        return str(inv.get(key) or "").strip()

    def _ttkhac(field: str) -> Optional[str]:
        for item in (inv.get("ttkhac") or []):
            if item.get("ttruong") == field:
                return item.get("dlieu")
        return None

    def _cks_field(raw_key: str, field: str) -> Optional[str]:
        raw = inv.get(raw_key)
        if not raw:
            return None
        try:
            return json.loads(raw).get(field)
        except Exception:
            return None

    return {
        "invoice_no":       _s("shdon"),
        "invoice_symbol":   _s("khhdon"),
        "invoice_form":     str(inv.get("khmshdon") or ""),
        "invoice_type":     _s("thdon"),
        "issue_date":       _s("tdlap"),
        "currency":         _s("dvtte"),
        "exchange_rate":    _f("tgia"),
        "payment_method":   _s("thtttoan"),
        "seller_tax_code":  _s("nbmst"),
        "seller_name":      _s("nbten"),
        "seller_address":   _s("nbdchi"),
        "seller_phone":     _s("nbsdthoai"),
        "seller_email":     _s("nbdctdtu"),
        "seller_bank":      _s("nbstkhoan"),
        "seller_bank_name": _s("nbtnhang"),
        "seller_fax":       _s("nbfax"),
        "seller_website":   _s("nbwebsite"),
        "seller_signing_time": _cks_field("nbcks", "SigningTime"),
        "buyer_tax_code":   _s("nmmst"),
        "buyer_name":       _s("nmten"),
        "buyer_address":    _s("nmdchi"),
        "buyer_phone":      _s("nmsdthoai"),
        "buyer_email":      _s("nmdctdtu"),
        "buyer_bank":       _s("nmstkhoan"),
        "buyer_bank_name":  _s("nmtnhang"),
        "amount":           _f("tgtcthue"),
        "vat_amount":       _f("tgtthue"),
        "total_amount":     _f("tgtttbso"),
        "total_in_words":   _s("tgtttbchu"),
        "discount_amount":  _f("ttcktmai"),
        "vat_breakdown": [
            {"vat_rate": t.get("tsuat"), "taxable": float(t.get("thtien") or 0),
             "vat_amount": float(t.get("tthue") or 0)}
            for t in (inv.get("thttltsuat") or [])
            if t.get("tsuat") is not None
        ],
        "tax_authority_code": _s("cqt"),
        "tax_signing_time":   _cks_field("cqtcks", "SigningTime"),
        "portal_link": _ttkhac("PortalLink"),
        "fkey":        _ttkhac("Fkey"),
        "ttxly":       inv.get("ttxly"),
        "tthai":       inv.get("tthai"),
    }


class ApiCrawlerEngine:
    def __init__(
        self,
        job_id: int,
        username: str,
        password: str,
        chunks: List[Tuple[str, str]],
        emit_fn: Optional[Callable[[str], None]] = None,
        account_id: Optional[int] = None,
        app=None,
        emit_captcha_fn=None,
        captcha_event=None,
        get_captcha_answer=None,
    ) -> None:
        self.job_id             = job_id
        self.account_id         = account_id
        self.username           = username
        self.password           = password
        self.chunks             = chunks
        self.emit_fn            = emit_fn
        self.app                = app
        self._stop              = False
        self.emit_captcha_fn    = emit_captcha_fn
        self.captcha_event      = captcha_event
        self.get_captcha_answer = get_captcha_answer
        self._browser           = BrowserManager()
        self._jwt: str          = ""
        self._jwt_from_env: str = ""
        self._total_to_process  = 0
        self._total_processed   = 0
        self._total_failed      = 0
        self._total_skipped     = 0

    async def run(self) -> None:
        self._update_job(status="running")
        self._emit("🚀 Crawler khởi động.")
        try:
            self._jwt = await self._login_and_get_jwt()
            if not self._jwt:
                self._jwt = await self._get_or_refresh_jwt()
            if not self._jwt:
                raise RuntimeError("Không lấy được JWT sau khi đăng nhập.")

            async with GdtApiClient(self._jwt) as client:
                work_items = await self._gather_all(client)
                if not work_items or self._stop:
                    self._update_job(status="done")
                    self._emit("✅ Không có hóa đơn mới cần tải.")
                    return
                await self._process_all(client, work_items)

            self._update_job(status="done")
            self._emit(
                f"✅ Hoàn thành — "
                f"processed={self._total_processed}, "
                f"skipped={self._total_skipped}, "
                f"failed={self._total_failed}"
            )

        except asyncio.CancelledError:
            self._update_job(status="stopped")
            self._emit("⛔ Đã dừng.")
        except Exception as exc:
            logger.exception("Lỗi: {}", exc)
            self._update_job(status="failed", error_message=str(exc))
            self._emit(f"❌ Thất bại: {exc}")

    async def _get_or_refresh_jwt(self, force: bool = False) -> str:
        """Lấy JWT từ session cookie, chỉ login thật khi cần."""
        ctx  = await self._browser.start()
        page = await ctx.new_page()
        try:
            if not force and self._browser.has_saved_session:
                # Kiểm tra session còn sống không mà không login lại
                self._emit("🔍 Kiểm tra session cũ…")
                await page.goto("https://hoadondientu.gdt.gov.vn", 
                                wait_until="networkidle", timeout=30_000)
                for c in await page.context.cookies():
                    if c["name"].lower() == "jwt" and c.get("value"):
                        self._emit("✅ Dùng lại JWT từ session cũ.")
                        return c["value"]
                self._emit("⚠️ Session cũ không có JWT — tiến hành đăng nhập…")

            # Cần login thật
            ok = await ensure_logged_in(
                page=page, browser_manager=self._browser,
                username=self.username, password=self.password,
                emit_fn=self.emit_fn, emit_captcha_fn=self.emit_captcha_fn,
                captcha_event=self.captcha_event, get_captcha_answer=self.get_captcha_answer,
            )
            if not ok:
                return ""
            for c in await page.context.cookies():
                if c["name"].lower() == "jwt":
                    return c["value"]
            return ""
        finally:
            await self._browser.close()

    def request_stop(self) -> None:
        self._stop = True

    async def _gather_all(self, client: GdtApiClient) -> List[Tuple[Dict[str, Any], str]]:
        self._emit("📡 Phase 1 — Thu thập danh sách hóa đơn (tuần tự từng segment)…")

        segment_counts: Dict[str, int] = {}
        seen: set = set()
        all_mapped: List[Tuple[Dict[str, Any], str]] = []

        for sd, ed in self.chunks:
            for cat, path, ttxly, label in _CRAWL_PLAN:
                if self._stop:
                    break
                log_buffer: List[str] = []
                try:
                    items = await client.list_invoices(
                        path=path, start_date=sd, end_date=ed,
                        ttxly=ttxly, label=label, log_buffer=log_buffer,
                    )
                except PermissionError:
                    self._emit("  🔑 JWT hết hạn — re-login…")
                    self._jwt = await self._get_or_refresh_jwt(force=True)
                    client.update_jwt(self._jwt)
                    items = await client.list_invoices(
                        path=path, start_date=sd, end_date=ed,
                        ttxly=ttxly, label=label, log_buffer=log_buffer,
                    )
                except Exception as exc:
                    logger.error("Segment [{}/{}] lỗi: {}", label, sd, exc)
                    items = []

                for line in log_buffer:
                    logger.debug(line)

                segment_counts[label] = segment_counts.get(label, 0) + len(items)
                for raw in items:
                    inv = _map_inv(raw)
                    if not inv["invoice_no"]:
                        continue
                    key = _dedup_key(inv, cat)
                    if key not in seen:
                        seen.add(key)
                        all_mapped.append((inv, cat))

        # --- phần còn lại giữ nguyên ---
        self._emit("─" * 64)
        for lbl, cnt in segment_counts.items():
            self._emit(f"  {lbl:<48} {cnt:>8,}")
        self._emit(f"  {'Tổng':<48} {sum(segment_counts.values()):>8,}")
        self._emit("─" * 64)

        # Log theo tháng
        month_counts: Dict[str, int] = {}
        for inv, _ in all_mapped:
            d = _parse_gdt_date(inv.get("issue_date"))
            key = f"{d.month:02d}/{d.year}" if d else "??"
            month_counts[key] = month_counts.get(key, 0) + 1
        if month_counts:
            self._emit("📅 Phân bổ theo tháng:")
            for ym in sorted(month_counts, key=lambda s: (s[3:], s[:2])):
                self._emit(f"  Tháng {ym}: {month_counts[ym]:,} hóa đơn")
        self._emit("─" * 64)

        new_items: List[Tuple[Dict[str, Any], str]] = []
        for inv, category in all_mapped:
            if self._is_already_crawled(inv, category):
                self._total_skipped += 1
            else:
                new_items.append((inv, category))

        self._total_to_process = len(new_items)
        self._emit(f"📋 Cần tải mới: {self._total_to_process:,}  |  Đã có: {self._total_skipped:,}")
        self._emit_progress()
        self._update_job(total_invoices=self._total_to_process + self._total_skipped)
        return new_items

    async def _process_all(self, client: GdtApiClient,
                           work_items: List[Tuple[Dict[str, Any], str]]) -> None:
        self._emit(f"⚡ Phase 2 — Tải {len(work_items):,} hóa đơn (concurrency={_CONCURRENCY})…")
        sem   = asyncio.Semaphore(_CONCURRENCY)
        total = len(work_items)

        async def _worker(idx: int, inv: Dict[str, Any], category: str) -> None:
            if self._stop:
                return
            async with sem:
                result = await self._process_invoice(client, inv, category)
                with self._app_context():
                    if result:
                        try:
                            self._persist_invoice(result)
                            self._total_processed += 1
                        except Exception as exc:
                            logger.error("persist #{}: {}", inv["invoice_no"], exc)
                            self._total_failed += 1
                    else:
                        self._total_failed += 1
                self._emit_progress()
                self._update_counters()
                await asyncio.sleep(_DELAY)

        await asyncio.gather(
            *[asyncio.create_task(_worker(i + 1, inv, cat))
              for i, (inv, cat) in enumerate(work_items)],
            return_exceptions=True,
        )

    async def _process_invoice(self, client: GdtApiClient, inv: Dict[str, Any],
                                invoice_category: str) -> Optional[Dict[str, Any]]:
        invoice_no = inv["invoice_no"]
        issue_date = _parse_gdt_date(inv["issue_date"])
        inv_dir    = ensure_invoice_dir(invoice_no, issue_date, suffix=invoice_category)

        nbmst    = inv["seller_tax_code"]
        khhdon   = inv["invoice_symbol"]
        shdon    = invoice_no
        khmshdon = inv["invoice_form"] or "1"

        xml_meta: Dict[str, Any]          = {}
        line_items: List[Dict[str, Any]]  = []
        vat_breakdown: List[Dict[str, Any]] = []
        zip_path: Optional[Path]          = None
        extract  = ZipExtractResult()
        view_html_path: Optional[Path]    = None
        has_pdf  = False

        zip_bytes = await client.export_xml(nbmst, khhdon, shdon, khmshdon)
        if zip_bytes:
            zip_path = inv_dir / "invoice.zip"
            zip_path.write_bytes(zip_bytes)
            extract = extract_invoice_zip(zip_path, inv_dir / "extracted")

            if extract.data_xml_path and extract.data_xml_path.exists():
                raw = extract.data_xml_path.read_bytes()
                xml_meta      = XmlService.parse_metadata(raw)
                line_items    = XmlService.parse_line_items(raw)
                vat_breakdown = xml_meta.pop("vat_breakdown", [])

        if not extract.data_xml_path:
            detail = await client.get_detail(nbmst, khhdon, shdon, khmshdon)
            if detail:
                detail_json = json.loads(detail.decode("utf-8"))
                line_items = detail_json.get("hdhhdvu", [])

        if extract.view_html_path:
            view_html_path = extract.view_html_path

        def x(xml_key: str, inv_key: str = "", fallback: Any = "") -> Any:
            return xml_meta.get(xml_key) or (inv.get(inv_key) if inv_key else None) or fallback

        result: Dict[str, Any] = {
            "invoice_no":       x("invoice_no",     "invoice_no"),
            "invoice_symbol":   x("invoice_symbol",  "invoice_symbol"),
            "invoice_form":     x("invoice_form",    "invoice_form"),
            "invoice_type":     xml_meta.get("invoice_type_gdt") or inv.get("invoice_type"),
            "invoice_category": invoice_category,
            "issue_date":       x("issue_date",      "issue_date"),
            "status":           "downloaded",
            "currency":         x("currency",        "currency"),
            "exchange_rate":    xml_meta.get("exchange_rate") or inv.get("exchange_rate"),
            "payment_method":   xml_meta.get("payment_method") or inv.get("payment_method"),
            "xml_version":      xml_meta.get("xml_version"),
            "software_tax_code":xml_meta.get("software_tax_code"),
            "is_adjustment":    xml_meta.get("is_adjustment"),
            "portal_link":      xml_meta.get("portal_link") or inv.get("portal_link"),
            "fkey":             xml_meta.get("fkey")         or inv.get("fkey"),
            "seller_signing_time": xml_meta.get("seller_signing_time") or inv.get("seller_signing_time"),
            "tax_signing_time":    xml_meta.get("tax_signing_time")    or inv.get("tax_signing_time"),
            "seller_name":      x("seller_name",     "seller_name"),
            "seller_tax_code":  x("seller_tax_code", "seller_tax_code"),
            "seller_address":   x("seller_address",  "seller_address"),
            "seller_phone":     x("seller_phone",    "seller_phone"),
            "seller_email":     x("seller_email",    "seller_email"),
            "seller_bank":      x("seller_bank",     "seller_bank"),
            "seller_bank_name": x("seller_bank_name","seller_bank_name"),
            "seller_fax":       x("seller_fax",      "seller_fax"),
            "seller_website":   x("seller_website",  "seller_website"),
            "buyer_name":       x("buyer_name",      "buyer_name"),
            "buyer_tax_code":   x("buyer_tax_code",  "buyer_tax_code"),
            "buyer_address":    x("buyer_address",   "buyer_address"),
            "buyer_phone":      x("buyer_phone",     "buyer_phone"),
            "buyer_email":      x("buyer_email",     "buyer_email"),
            "buyer_bank":       x("buyer_bank",      "buyer_bank"),
            "buyer_bank_name":  x("buyer_bank_name", "buyer_bank_name"),
            "amount":           xml_meta.get("amount")       or inv["amount"],
            "vat_rate":         xml_meta.get("vat_rate"),
            "vat_amount":       xml_meta.get("vat_amount")   or inv["vat_amount"],
            "total_amount":     xml_meta.get("total_amount") or inv["total_amount"],
            "total_in_words":   xml_meta.get("total_in_words") or inv.get("total_in_words"),
            "discount_amount":  xml_meta.get("discount_amount") or inv.get("discount_amount"),
            "non_taxable_amount": xml_meta.get("non_taxable_amount"),
            "other_amount":     xml_meta.get("other_amount"),
            "vat_breakdown":    vat_breakdown or inv.get("vat_breakdown", []),
            "tax_authority_code": xml_meta.get("tax_authority_code") or inv.get("tax_authority_code"),
            "qr_data":          xml_meta.get("qr_data"),
            "line_items":       line_items,
            "zip_path":         str(zip_path)               if zip_path              else None,
            "xml_data_path":    str(extract.data_xml_path)  if extract.data_xml_path else None,
            "view_html_path":   str(view_html_path)         if view_html_path        else None,
            "pdf_path":         str(inv_dir / "invoice.pdf") if has_pdf              else None,
            "invoice_dir":      str(inv_dir),
            "has_zip":  zip_path is not None and zip_path.exists(),
            "has_xml":  extract.data_xml_path is not None,
            "has_html": view_html_path is not None,
            "has_pdf":  has_pdf,
        }

        try:
            meta_path = inv_dir / "metadata.json"
            meta_path.write_text(
                json.dumps(result, default=str, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result["metadata_path"] = str(meta_path)
        except Exception:
            result["metadata_path"] = None

        return result

    async def _login_and_get_jwt(self) -> str:
        ctx  = await self._browser.start()
        page = await ctx.new_page()
        try:
            ok = await ensure_logged_in(
                page=page, browser_manager=self._browser,
                username=self.username, password=self.password,
                emit_fn=self.emit_fn, emit_captcha_fn=self.emit_captcha_fn,
                captcha_event=self.captcha_event, get_captcha_answer=self.get_captcha_answer,
            )
            if not ok:
                return ""
            for c in await page.context.cookies():
                if c["name"].lower() == "jwt":
                    return c["value"]
            return ""
        finally:
            await self._browser.close()

    def _is_already_crawled(self, inv: Dict[str, Any], category: str) -> bool:
        with self._app_context():
            try:
                existing = InvoiceRepository.exists_by_composite_key(
                    invoice_no=inv["invoice_no"],
                    invoice_symbol=inv["invoice_symbol"] or None,
                    invoice_form=inv["invoice_form"]     or None,
                    invoice_category=category,
                    total_amount=inv["total_amount"],
                )
                return bool(existing and existing.status not in ("failed", "error"))
            except Exception as exc:
                logger.warning("DB check #{}: {}", inv["invoice_no"], exc)
        return False

    def _persist_invoice(self, result: Dict[str, Any]) -> None:
        issue_date = result.get("issue_date")
        if isinstance(issue_date, str) and issue_date:
            issue_date = _parse_gdt_date(issue_date)

        line_items    = result.get("line_items",    [])
        vat_breakdown = result.get("vat_breakdown", [])

        db_data: Dict[str, Any] = {k: result.get(k) for k in _DB_FIELDS}
        db_data.update({
            "issue_date":          datetime.combine(issue_date, datetime.min.time()) if isinstance(issue_date, date) else None,
            "seller_signing_time": _parse_gdt_datetime(result.get("seller_signing_time")),
            "tax_signing_time":    _parse_gdt_datetime(result.get("tax_signing_time")),
            "vat_breakdown_json":  json.dumps(vat_breakdown, ensure_ascii=False) if vat_breakdown else None,
            "line_items_json":     json.dumps(line_items,    ensure_ascii=False) if line_items    else None,
            "account_id":          self.account_id,
        })
        InvoiceRepository.upsert(db_data)

    def _emit_progress(self) -> None:
        if self.emit_fn:
            self.emit_fn(json.dumps({
                "__progress__": True,
                "total":   self._total_to_process,
                "done":    self._total_processed,
                "failed":  self._total_failed,
                "skipped": self._total_skipped,
            }))

    def _update_job(self, **kwargs: Any) -> None:
        with self._app_context():
            try:
                CrawlJobRepository.update_status(self.job_id, kwargs.pop("status", "running"), **kwargs)
            except Exception as exc:
                logger.warning("Job update: {}", exc)

    def _update_counters(self) -> None:
        self._update_job(
            total_invoices=self._total_to_process + self._total_skipped,
            downloaded_invoices=self._total_processed,
            failed_invoices=self._total_failed,
        )

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


def _to_gdt_dt(date_str: str, time_part: str) -> str:
    if "T" in date_str:
        return date_str
    if "-" in date_str and len(date_str) == 10:
        p = date_str.split("-")
        date_str = f"{p[2]}/{p[1]}/{p[0]}"
    return f"{date_str}T{time_part}"


def _parse_gdt_date(raw: Any) -> Optional[date]:
    if not raw:
        return None

    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw

    if isinstance(raw, datetime):
        return raw.date()

    raw = str(raw).strip()

    try:
        return datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        ).date()
    except Exception:
        pass

    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass

    return None


def _parse_gdt_datetime(raw: Any) -> Optional[datetime]:
    if not raw:
        return None

    if isinstance(raw, datetime):
        return raw

    raw = str(raw).strip()

    try:
        return datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
    except Exception:
        pass

    for fmt in (
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass

    return None