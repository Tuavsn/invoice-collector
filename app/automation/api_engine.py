from __future__ import annotations

import asyncio
import json
import shutil
import zipfile
from datetime import datetime, date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import httpx
from loguru import logger

from app.automation.browser import BrowserManager
from app.automation.login import ensure_logged_in
from app.db.repository import CrawlJobRepository, InvoiceRepository
from app.services.xml_service import XmlService
from app.utils.paths import ensure_invoice_dir

GDT_BASE   = "https://hoadondientu.gdt.gov.vn"
_PAGE_SIZE = 50
_RETRY     = 2
_DELAY     = 0.5

_CRAWL_PLAN: List[Tuple[str, str, Optional[int], str]] = [
    ("sale_einvoice",       "/api/query/invoices/sold",         None, "Bán ra - HĐ điện tử"),
    ("sale_pos",            "/api/sco-query/invoices/sold",     None, "Bán ra - HĐ máy tính tiền"),
    ("purchase_einvoice_5", "/api/query/invoices/purchase",     5,    "Mua vào - Đã cấp HĐ - điện tử"),
    ("purchase_pos_5",      "/api/sco-query/invoices/purchase", 5,    "Mua vào - Đã cấp HĐ - máy tính tiền"),
    ("purchase_einvoice_6", "/api/query/invoices/purchase",     6,    "Mua vào - Cục thuế không nhận mã - điện tử"),
    ("purchase_pos_6",      "/api/sco-query/invoices/purchase", 6,    "Mua vào - Cục thuế không nhận mã - máy tính tiền"),
    ("purchase_einvoice_8", "/api/query/invoices/purchase",     8,    "Mua vào - Cục thuế đã nhận có mã - điện tử"),
    ("purchase_pos_8",      "/api/sco-query/invoices/purchase", 8,    "Mua vào - Cục thuế đã nhận có mã - máy tính tiền"),
]

_DB_FIELDS = [
    "invoice_no", "invoice_symbol", "invoice_form", "invoice_type", "invoice_category", "account_id",
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
        attempt = 0
        while attempt < _RETRY:
            try:
                r = await self._client.get(url)
            except Exception as exc:
                attempt += 1
                logger.warning("[{}] GET error (attempt {}): {}", label, attempt, exc)
                await asyncio.sleep(1)
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 401:
                raise PermissionError("401")
            if r.status_code == 429:
                logger.warning("[{}] HTTP 429 Too Many Requests", label)
                await asyncio.sleep(30)
                continue
            if r.status_code >= 400:
                attempt += 1
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

            new_items = [it for it in items if _raw_item_key(it) not in seen_keys]
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

    async def export_xml(
        self,
        nbmst: str,
        khhdon: str,
        shdon: str,
        khmshdon: str,
        path_prefix: str = "/api/query",
    ) -> Optional[bytes]:
        params = {"nbmst": nbmst, "khhdon": khhdon, "shdon": shdon, "khmshdon": khmshdon}
        url = GDT_BASE + f"{path_prefix}/invoices/export-xml?" + urlencode(params)
        return await self._get(url)

    async def get_detail(
        self,
        nbmst: str,
        khhdon: str,
        shdon: str,
        khmshdon: str,
        path_prefix: str = "/api/query",
    ) -> Optional[bytes]:
        params = {"nbmst": nbmst, "khhdon": khhdon, "shdon": shdon, "khmshdon": khmshdon}
        url = GDT_BASE + f"{path_prefix}/invoices/detail?" + urlencode(params)
        return await self._get(url)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _raw_item_key(raw: Dict[str, Any]) -> tuple:
    return (raw.get("shdon"), raw.get("khhdon"), raw.get("khmshdon"),
            raw.get("nbmst"), raw.get("nmmst"), raw.get("ttxly"))


def _dedup_key(inv: Dict[str, Any], category: str) -> tuple:
    return (inv["invoice_no"], inv["invoice_symbol"] or "", inv["invoice_form"] or "",
            category, inv["total_amount"])


def _normalize_xml_line_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Chuẩn hóa XML schema → detail JSON schema để thống nhất lưu DB và render."""
    return {
        "stt":     item.get("stt"),
        "tchat":   item.get("tinh_chat"),
        "ten":     item.get("ten_hhdvu"),
        "dvtinh":  item.get("don_vi_tinh"),
        "sluong":  item.get("so_luong"),
        "dgia":    item.get("don_gia"),
        "tlckhau": item.get("ty_le_ck"),
        "stckhau": item.get("so_tien_ck"),
        "thtien":  item.get("thanh_tien"),
        "tsuat":   item.get("thue_suat"),
        "tthue":   item.get("tien_thue"),
    }

def _normalize_detail_line_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Chuẩn hóa hdhhdvu từ detail JSON API → normalized keys (giống XML path)."""
    tsuat_raw = item.get("ltsuat") or item.get("tsuat")
    return {
        "stt":     item.get("stt"),
        "tchat":   item.get("tchat"),
        "ten":     item.get("ten"),
        "dvtinh":  item.get("dvtinh"),
        "sluong":  item.get("sluong"),
        "dgia":    item.get("dgia"),
        "tlckhau": item.get("tlckhau"),
        "stckhau": item.get("stckhau"),
        "thtien":  item.get("thtien"),
        "tsuat":   _render_tsuat(tsuat_raw),
        "tthue":   item.get("tthue"),
    }


def _render_tsuat(tsuat_raw: Any) -> str:
    """Render thuế suất: float 0.08 → '8%', string '8%' → '8%'."""
    if tsuat_raw is None:
        return ""
    if isinstance(tsuat_raw, (int, float)):
        try:
            return f"{int(round(float(tsuat_raw) * 100))}%"
        except (ValueError, TypeError):
            return str(tsuat_raw)
    return str(tsuat_raw)


def _esc(s: Any) -> str:
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _fmt_number(v: Any) -> str:
    if v is None:
        return ""
    try:
        return f"{int(round(float(v))):,}".replace(",", ".")
    except (ValueError, TypeError):
        return str(v)


def _fmt_qty(v: Any) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
        s = f"{f:.2f}".rstrip("0").rstrip(".")
        parts = s.split(".")
        parts[0] = f"{int(parts[0]):,}".replace(",", ".")
        return ",".join(parts) if len(parts) > 1 else parts[0]
    except (ValueError, TypeError):
        return str(v)


def _parse_issue_date(raw: Any) -> Tuple[str, str, str]:
    d = _parse_gdt_date(raw)
    if d:
        return str(d.day).zfill(2), str(d.month).zfill(2), str(d.year)
    return "??", "??", "????"


def _tchat_label(tchat: Any) -> str:
    mapping = {1: "Hàng hóa, dịch vụ", 2: "Khuyến mại", 3: "Chiết khấu thương mại", 4: "Ghi chú"}
    try:
        return mapping.get(int(tchat), str(tchat or ""))
    except (ValueError, TypeError):
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# ZIP extract
# ─────────────────────────────────────────────────────────────────────────────

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
                        elif local_tag == "TDiep":
                            hdon_node = root.find(".//{*}HDon") or root.find(".//HDon")
                            if hdon_node is not None:
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


# ─────────────────────────────────────────────────────────────────────────────
# Map raw API item
# ─────────────────────────────────────────────────────────────────────────────

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
        "invoice_no":        _s("shdon"),
        "invoice_symbol":    _s("khhdon"),
        "invoice_form":      str(inv.get("khmshdon") or ""),
        "invoice_type":      _s("thdon") or _s("tlhdon"),
        "issue_date":        _s("tdlap"),
        "currency":          _s("dvtte"),
        "exchange_rate":     _f("tgia"),
        "payment_method":    _s("thtttoan"),
        "xml_version":       _s("pban"),
        "software_tax_code": _s("msttcgp"),
        "is_adjustment":     inv.get("hdcttchinh"),
        "qr_data":           _s("qrcode"),
        "seller_tax_code":   _s("nbmst"),
        "seller_name":       _s("nbten"),
        "seller_address":    _s("nbdchi"),
        "seller_phone":      _s("nbsdthoai"),
        "seller_email":      _s("nbdctdtu"),
        "seller_bank":       _s("nbstkhoan"),
        "seller_bank_name":  _s("nbtnhang"),
        "seller_fax":        _s("nbfax"),
        "seller_website":    _s("nbwebsite"),
        "seller_signing_time": _cks_field("nbcks", "SigningTime"),
        "buyer_tax_code":    _s("nmmst"),
        "buyer_name":        _s("nmten"),
        "buyer_address":     _s("nmdchi"),
        "buyer_phone":       _s("nmsdthoai"),
        "buyer_email":       _s("nmdctdtu"),
        "buyer_bank":        _s("nmstkhoan"),
        "buyer_bank_name":   _s("nmtnhang"),
        "amount":            _f("tgtcthue"),
        "vat_amount":        _f("tgtthue"),
        "total_amount":      _f("tgtttbso"),
        "total_in_words":    _s("tgtttbchu"),
        "discount_amount":   _f("ttcktmai"),
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


# ─────────────────────────────────────────────────────────────────────────────
# Fallback XML stub
# ─────────────────────────────────────────────────────────────────────────────

def build_fallback_xml(inv: Dict[str, Any], detail_json: Dict[str, Any]) -> str:
    def _xs(key: str) -> str:
        v = detail_json.get(key)
        return _esc(v) if v else ""

    def _xi(key: str) -> str:
        v = detail_json.get(key)
        return str(int(v)) if v is not None else ""

    def _xf(key: str) -> str:
        v = detail_json.get(key)
        return str(v) if v is not None else "0"

    issue_date = _parse_gdt_date(detail_json.get("tdlap"))
    nlap = issue_date.strftime("%Y-%m-%d") if issue_date else ""

    items_xml = ""
    for item in (detail_json.get("hdhhdvu") or []):
        def _is(k: str) -> str:
            v = item.get(k)
            return _esc(v) if v else ""
        def _if(k: str) -> str:
            v = item.get(k)
            return str(v) if v is not None else ""

        items_xml += (
            f"<HHDVu>"
            f"<TChat>{_if('tchat')}</TChat>"
            f"<STT>{_if('stt')}</STT>"
            f"<THHDVu>{_is('ten')}</THHDVu>"
            f"<DVTinh>{_is('dvtinh')}</DVTinh>"
            f"<SLuong>{_if('sluong')}</SLuong>"
            f"<DGia>{_if('dgia')}</DGia>"
            f"<TLCKhau>{_if('tlckhau')}</TLCKhau>"
            f"<STCKhau>{_if('stckhau')}</STCKhau>"
            f"<TSuat>{_esc(_render_tsuat(item.get('tsuat')))}</TSuat>"
            f"<ThTien>{_if('thtien')}</ThTien>"
            f"</HHDVu>"
        )

    ltsuat_xml = ""
    for t in (detail_json.get("thttltsuat") or []):
        ltsuat_xml += (
            f"<LTSuat>"
            f"<TSuat>{_esc(str(t.get('tsuat') or ''))}</TSuat>"
            f"<TThue>{t.get('tthue') or 0}</TThue>"
            f"<ThTien>{t.get('thtien') or 0}</ThTien>"
            f"</LTSuat>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<!-- BẢN TÁI TẠO TỪ DỮ LIỆU TRA CỨU — KHÔNG THAY THẾ HÓA ĐƠN GỐC CÓ CHỮ KÝ SỐ -->"
        "<HDon><DLHDon>"
        "<TTChung>"
        f"<PBan>{_xs('pban')}</PBan>"
        f"<THDon>{_xs('thdon') or _xs('tlhdon')}</THDon>"
        f"<KHMSHDon>{_xi('khmshdon')}</KHMSHDon>"
        f"<KHHDon>{_xs('khhdon')}</KHHDon>"
        f"<SHDon>{_xs('shdon')}</SHDon>"
        f"<NLap>{_esc(nlap)}</NLap>"
        f"<HDCTTChinh>{detail_json.get('hdcttchinh') or 0}</HDCTTChinh>"
        f"<DVTTe>{_xs('dvtte')}</DVTTe>"
        f"<TGia>{_xf('tgia')}</TGia>"
        f"<HTTToan>{_xs('thtttoan')}</HTTToan>"
        f"<MSTTCGP>{_xs('msttcgp')}</MSTTCGP>"
        "</TTChung>"
        "<NDHDon>"
        "<NBan>"
        f"<Ten>{_xs('nbten')}</Ten>"
        f"<MST>{_xs('nbmst')}</MST>"
        f"<DChi>{_xs('nbdchi')}</DChi>"
        f"<SDThoai>{_xs('nbsdthoai')}</SDThoai>"
        f"<DCTDTu>{_xs('nbdctdtu')}</DCTDTu>"
        "</NBan>"
        "<NMua>"
        f"<Ten>{_xs('nmten')}</Ten>"
        f"<MST>{_xs('nmmst')}</MST>"
        f"<DChi>{_xs('nmdchi')}</DChi>"
        f"<SDThoai>{_xs('nmsdthoai')}</SDThoai>"
        f"<DCTDTu>{_xs('nmdctdtu')}</DCTDTu>"
        "</NMua>"
        f"<DSHHDVu>{items_xml}</DSHHDVu>"
        "<TToan>"
        f"<THTTLTSuat>{ltsuat_xml}</THTTLTSuat>"
        f"<TgTCThue>{_xf('tgtcthue')}</TgTCThue>"
        f"<TgTThue>{_xf('tgtthue')}</TgTThue>"
        f"<TgTTTBSo>{_xf('tgtttbso')}</TgTTTBSo>"
        f"<TgTTTBChu>{_xs('tgtttbchu')}</TgTTTBChu>"
        "</TToan>"
        "</NDHDon>"
        "</DLHDon></HDon>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fallback HTML
# ─────────────────────────────────────────────────────────────────────────────

def build_fallback_html(
    inv: Dict[str, Any],
    detail_json: Dict[str, Any],
    line_items: Optional[List[Dict[str, Any]]] = None,
    static_url_root: str = "/static",
) -> str:
    bg_url   = "viewinvoice-bg.jpg"
    sign_url = "sign-check.jpg"

    day, month, year = _parse_issue_date(detail_json.get("tdlap"))

    thdon         = detail_json.get("thdon") or "HÓA ĐƠN"
    invoice_title = thdon.upper()
    khmshdon      = detail_json.get("khmshdon") or ""
    khhdon        = _esc(detail_json.get("khhdon") or "")
    shdon         = _esc(detail_json.get("shdon") or "")
    qrcode        = _esc(detail_json.get("qrcode") or "")

    nb_ten   = _esc(detail_json.get("nbten") or "")
    nb_mst   = _esc(detail_json.get("nbmst") or "")
    nb_dchi  = _esc(detail_json.get("nbdchi") or "")
    nb_sdt   = _esc(detail_json.get("nbsdthoai") or "")
    nb_stk   = _esc(detail_json.get("nbstkhoan") or "")
    nb_chma  = _esc(detail_json.get("chma") or "")
    nb_chten = _esc(detail_json.get("chten") or "")

    nm_ten    = _esc(detail_json.get("nmten") or "")
    nm_mst    = _esc(detail_json.get("nmmst") or "")
    nm_dchi   = _esc(detail_json.get("nmdchi") or "")
    nm_stk    = _esc(detail_json.get("nmstkhoan") or "")
    nm_cmnd   = _esc(detail_json.get("nmcmnd") or "")
    nm_hchieu = _esc(detail_json.get("nmshchieu") or "")
    nm_dvqhns = _esc(detail_json.get("nmmdvqhnsach") or "")
    thtttoan  = _esc(detail_json.get("thtttoan") or "")

    nbhdktso   = _esc(detail_json.get("nbhdktso") or "")
    nbhdktngay = _esc(detail_json.get("nbhdktngay") or "")

    nbcks_raw = detail_json.get("nbcks")
    nbcks: Dict[str, Any] = {}
    if nbcks_raw:
        try:
            nbcks = json.loads(nbcks_raw) if isinstance(nbcks_raw, str) else nbcks_raw
        except Exception:
            pass
    cks_subject      = _esc(nbcks.get("Subject") or "")
    cks_signing_time = _esc(nbcks.get("SigningTime") or "")

    # ── Hàng hóa / dịch vụ ──────────────────────────────────────────────────
    items_to_render = line_items if line_items is not None else (detail_json.get("hdhhdvu") or [])

    items_rows = ""
    for idx, item in enumerate(items_to_render, start=1):
        tchat     = _esc(_tchat_label(item.get("tchat") or item.get("tinh_chat")))
        thhhdvu   = _esc(item.get("ten") or item.get("ten_hhdvu") or "")
        dvtinh    = _esc(item.get("dvtinh") or item.get("don_vi_tinh") or "")
        loai_hhdt = _esc(item.get("mhhhdvudt") or item.get("ma_hhdvu") or "")

        sluong_val = item.get("sluong") if item.get("sluong") is not None else item.get("so_luong")
        sluong     = _fmt_qty(sluong_val)

        dgia_val = item.get("dgia") if item.get("dgia") is not None else item.get("don_gia")
        dgia     = _fmt_number(dgia_val)

        tlckhau_val = item.get("tlckhau") if item.get("tlckhau") is not None else item.get("ty_le_ck")
        tlckhau     = _fmt_number(tlckhau_val) if tlckhau_val else ""

        thtien_val = item.get("thtien") if item.get("thtien") is not None else item.get("thanh_tien")
        thtien     = _fmt_number(thtien_val)

        tsuat_raw     = item.get("tsuat") if item.get("tsuat") is not None else item.get("thue_suat")
        tsuat_display = _render_tsuat(tsuat_raw)

        items_rows += (
            f'<tr>'
            f'<td class="tx-center">{idx}</td>'
            f'<td class="tx-left"><span>{tchat}</span></td>'
            f'<td class="tx-left" style="max-width:200px;word-wrap:break-word;">{loai_hhdt}</td>'
            f'<td class="tx-left">{thhhdvu}</td>'
            f'<td class="tx-left">{dvtinh}</td>'
            f'<td class="tx-center">{sluong}</td>'
            f'<td class="tx-center">{dgia}</td>'
            f'<td class="tx-center">{tlckhau}</td>'
            f'<td class="tx-center">{_esc(tsuat_display)}</td>'
            f'<td class="tx-center">{thtien}</td>'
            f'</tr>'
        )

    # ── Bảng thuế suất ───────────────────────────────────────────────────────
    ltsuat_rows = ""
    for t in (detail_json.get("thttltsuat") or []):
        ts     = _esc(str(t.get("tsuat") or ""))
        thtien = _fmt_number(t.get("thtien"))
        tthue  = _fmt_number(t.get("tthue"))
        ltsuat_rows += (
            f'<tr>'
            f'<td class="tx-center">{ts}</td>'
            f'<td class="tx-center">{thtien}</td>'
            f'<td class="tx-center">{tthue}</td>'
            f'</tr>'
        )

    tgt_cthue  = _fmt_number(detail_json.get("tgtcthue"))
    tgt_thue   = _fmt_number(detail_json.get("tgtthue"))
    tgt_ttbso  = _fmt_number(detail_json.get("tgtttbso"))
    tgt_ttbchu = _esc(detail_json.get("tgtttbchu") or "")
    discount   = _fmt_number(detail_json.get("ttcktmai")) if detail_json.get("ttcktmai") else ""

    if cks_subject:
        sign_block = (
            f'<div class="sign-box">'
            f'<span>Signature Valid</span>'
            f'<span class="span-sign-box">Ký bởi&nbsp;</span>'
            f'<span id="cks" class="span-sign-box">{cks_subject}</span>'
            f'<span></span>'
            f'<span class="span-sign-box">Ký ngày:&nbsp;</span>'
            f'<span class="span-sign-box">{cks_signing_time}</span>'
            f'</div>'
        )
    else:
        sign_block = (
            '<div style="color:#888;font-size:11pt;margin-top:10px;">'
            '(Không có thông tin chữ ký số)'
            '</div>'
        )

    return f"""<html>
<head>
<META http-equiv="Content-Type" content="text/html; charset=UTF-8">
<title>Hóa Đơn Điện Tử</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=Edge">
<script type="text/javascript" src="details.js"></script>
<style>
* {{ box-sizing: border-box; -moz-box-sizing: border-box; }}
body {{ width: 100%; height: 100%; margin: 0 auto; padding: 0; font-size: 13pt; }}
.print-page {{ width: 210mm; min-height: 297mm; margin: 0mm auto; }}
.main-page {{
    font-family: "Times New Roman";
    max-width: 210mm;
    padding: 20px 20px 10px;
    margin: auto;
    background-image: url({bg_url});
    background-repeat: no-repeat;
    background-position: center center;
    background-size: 180%;
    border: 3px double rgba(145, 87, 21, 0.69);
    line-height: 1.5;
    box-shadow: rgb(222 226 230 / 70%) 0px 0px 9px 2px;
    position: relative;
    z-index: 1;
}}
canvas {{ display: inline-block }}
html {{ font-size: 100% }}
.heading-content .main-title {{
    font-size: 20pt; text-align: center; display: block;
    font-weight: bold; text-transform: uppercase;
}}
.heading-content p {{ font-size: 13pt; text-align: right; }}
.heading-content p.day {{ text-align: center; display: block; }}
.day .mg-bottom {{ margin-bottom: 8px !important; text-align: center; }}
.heading-content .top-content {{ display: flex; justify-content: space-between; }}
.heading-content .code-content {{ display: inline-block; }}
.vip-divide {{ width: 100%; height: 0; border-bottom: 1px solid rgba(145, 87, 21, 0.69); }}
.flex-li {{ display: flex; }}
.content-info {{ padding-top: 5px; }}
.content-info .list-fill-out {{ list-style: none; padding-inline-start: 0; margin-top: 5px; margin-bottom: 5px; }}
.content-info .list-fill-out li {{ font-size: 13pt; }}
.table-horizontal-wrapper {{ display: flex; justify-content: space-between; }}
.res-tb {{
    border-collapse: collapse; border-spacing: 0px;
    width: 100%; overflow-x: auto; margin: 10px 0px; min-width: 250px;
}}
.res-tb tr td {{ border: 1px solid black; padding: 6px 4px; vertical-align: baseline; }}
.res-tb tr td.tx-center {{ text-align: center; }}
.res-tb tr td.tx-left {{ text-align: left; }}
.res-tb tr td.tx-right {{ text-align: right; }}
.res-tb thead tr th {{ border: 1px solid black; vertical-align: middle; padding: 6px 4px; }}
.res-tb thead tr th.tb-stt {{ width: 70px; text-align: center; }}
.res-tb thead tr th.tb-thh {{ width: 200px; text-align: center; }}
.res-tb thead tr th.tb-dvt {{ width: 100px; text-align: center; }}
.res-tb thead tr th.tb-sl  {{ width: 80px;  text-align: center; }}
.res-tb thead tr th.tb-dg  {{ width: 80px;  text-align: center; }}
.res-tb thead tr th.tb-ts  {{ width: 80px;  text-align: center; }}
.res-tb thead tr th.tb-ttct {{ width: 250px; text-align: center; }}
.ft-sign {{ padding-top: 20px; }}
.ft-sign .sign-dx {{ display: flex; flex-wrap: wrap; justify-content: space-around; align-items: flex-start; }}
.ft-sign .sign-dx h3 p {{ text-align: center; font-size: 13pt; font-weight: 100; }}
.ft-sign .sign-dx h3 p:nth-child(2) {{ font-size: 14px; font-weight: normal; }}
.ft-sign .fd-end {{ padding-top: 120px; text-align: center; }}
.sign-box {{
    width: 260px !important; padding: 5px !important;
    border: 2px solid #23b709 !important;
    background-image: url("{sign_url}") !important;
    background-repeat: no-repeat !important;
    background-position: right 45px bottom 10px !important;
    background-size: 70px 60px !important;
    margin-top: 10px !important; font-weight: 500;
}}
.sign-box span {{ color: #23b709 !important; font-size: 13pt !important; text-align: left !important; display: block; }}
.span-sign-box {{ display: inline !important; }}
.data-item {{ width: 100%; display: flex; justify-content: left; align-items: flex-start; font-size: 13pt; color: rgba(0,0,0,0.85); }}
.data-item .di-label {{ min-height: 25px; height: auto; border-bottom: 1px dashed transparent; display: flex; align-items: flex-start; }}
.data-item .di-value {{ box-sizing: border-box; flex: 1; min-height: 25px; display: flex; align-items: flex-start; padding-left: 5px; height: auto; }}
@page {{ size: A4; margin: 0 !important; }}
@media print {{
    * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    body {{ width: auto; height: auto; margin: 0 auto; }}
    table tr, td {{ page-break-inside: avoid; }}
    table thead {{ display: table-row-group !important; }}
    .table-horizontal-wrapper {{ page-break-inside: avoid; padding-top: 5px; }}
    .main-page {{ margin: 0; width: initial; min-height: 296mm; background: none; border: none; }}
    .ft-sign {{ page-break-inside: avoid !important; page-break-after: auto; }}
    .fd-end {{ padding-top: 0 !important; }}
}}
</style>
</head>
<body>
<div class="main-page">
<div class="heading-content">
<div class="top-content">
<div style="width:80px;min-height:20px"><div id="qrcodeTable"></div></div>
<div class="code-content">
<b>Mẫu số: {_esc(str(khmshdon))}</b><br>
<b>Ký hiệu: {khhdon}</b><br>
<b>Số: {shdon}</b>
</div>
</div>
<div class="title-heading">
<h2 class="main-title">{_esc(invoice_title)}</h2>
<p class="day"><div class="day"><p class="day">Ngày {day} tháng {month} năm {year}</p></div></p>
</div>
</div>
<div class="vip-divide"></div>
<div class="content-info">
<ul class="list-fill-out">
<li><div class="data-item"><div class="di-label"><span>Tên người bán:</span></div><div class="di-value"><div>{nb_ten}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Mã số thuế:</span></div><div class="di-value"><div>{nb_mst}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Mã cửa hàng:</span></div><div class="di-value"><div>{nb_chma}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Tên cửa hàng:</span></div><div class="di-value"><div>{nb_chten}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Địa chỉ:</span></div><div class="di-value"><div>{nb_dchi}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Điện thoại:</span></div><div class="di-value"><div>{nb_sdt}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Số tài khoản:</span></div><div class="di-value"><div>{nb_stk if nb_stk else "&nbsp;&nbsp;&nbsp;"}</div></div></div></li>
<li><div class="vip-divide" style="margin:5px 0;"></div></li>
<li><div class="data-item"><div class="di-label"><span>Tên người mua:</span></div><div class="di-value"><div>{nm_ten}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Mã số thuế:</span></div><div class="di-value"><div>{nm_mst}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Mã ĐVCQHVNSNN:</span></div><div class="di-value"><div>{nm_dvqhns}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>CCCD người mua:</span></div><div class="di-value"><div>{nm_cmnd}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Số hộ chiếu:</span></div><div class="di-value"><div>{nm_hchieu}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Địa chỉ:</span></div><div class="di-value"><div>{nm_dchi}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Số tài khoản:</span></div><div class="di-value"><div>{nm_stk if nm_stk else "&nbsp;&nbsp;&nbsp;"}</div></div></div></li>
<li><div class="data-item"><div class="di-label"><span>Hình thức thanh toán:</span></div><div class="di-value"><div>{thtttoan}</div></div></div></li>
<li class="flex-li">
<div class="data-item" style="width:50%"><div class="di-label"><span>Số bảng kê:</span></div><div class="di-value"><div>{nbhdktso}</div></div></div>
<div class="data-item" style="width:50%"><div class="di-label"><span>Ngày bảng kê:</span></div><div class="di-value"><div>{nbhdktngay}</div></div></div>
</li>
</ul>
<table class="res-tb">
<thead style="text-align:center;">
<tr>
<th class="tb-stt">STT</th>
<th class="tb-stt">Tính chất</th>
<th class="tb-stt">Loại hàng hoá đặc trưng</th>
<th class="tb-thh">Tên hàng hóa, dịch vụ</th>
<th class="tb-dvt">Đơn vị tính</th>
<th class="tb-sl">Số lượng</th>
<th class="tb-dg">Đơn giá</th>
<th class="tb-dg">Chiết khấu</th>
<th class="tb-ts">Thuế suất</th>
<th class="tb-ttct">Thành tiền chưa có thuế GTGT</th>
</tr>
</thead>
<tbody>{items_rows}</tbody>
</table>
<div class="table-horizontal-wrapper">
<div style="margin-right:10px;">
<table class="res-tb">
<thead style="text-align:center"><tr><th>Thuế suất</th><th>Tổng tiền chưa thuế</th><th>Tổng tiền thuế</th></tr></thead>
<tbody>{ltsuat_rows}</tbody>
</table>
</div>
<div style="flex:1">
<table class="res-tb">
<tbody>
<tr><td class="tx-center">Tổng tiền chưa thuế<br>(Tổng cộng thành tiền chưa có thuế)</td><td class="tx-center" style="min-width:200px;max-width:300px;">{tgt_cthue}</td></tr>
<tr><td class="tx-center">Tổng tiền thuế (Tổng cộng tiền thuế)</td><td class="tx-center" style="min-width:200px;max-width:300px;">{tgt_thue}</td></tr>
<tr><td class="tx-center">Tổng tiền phí</td><td class="tx-center" style="min-width:200px;max-width:300px;">0</td></tr>
<tr><td class="tx-center">Tổng tiền chiết khấu thương mại</td><td class="tx-center" style="min-width:200px;max-width:300px;">{discount}</td></tr>
<tr><td class="tx-center">Tổng tiền thanh toán bằng số</td><td class="tx-center" style="min-width:200px;max-width:300px;">{tgt_ttbso}</td></tr>
<tr><td class="tx-center">Tổng tiền thanh toán bằng chữ</td><td class="tx-center" style="min-width:200px;max-width:300px;">{tgt_ttbchu}</td></tr>
</tbody>
</table>
</div>
</div>
</div>
<div class="vip-divide"></div>
<div class="ft-sign">
<div class="sign-dx">
<h3>
<p>NGƯỜI MUA HÀNG</p>
<p><i>(Chữ ký số (nếu có))</i></p>
</h3>
<h3>
<p>NGƯỜI BÁN HÀNG</p>
<p><i>(Chữ ký điện tử, chữ ký số)</i></p>
{sign_block}
</h3>
</div>
<div class="fd-end"><p><i>(Cần kiểm tra, đối chiếu khi lập, nhận hóa đơn)</i></p></div>
</div>
</div>
<input type="hidden" id="qrcodeContent" value="{qrcode}">
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# ApiCrawlerEngine
# ─────────────────────────────────────────────────────────────────────────────

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
        self._browser           = BrowserManager(username=self.username)
        self._jwt: str          = ""
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
        ctx  = await self._browser.start()
        page = await ctx.new_page()
        try:
            if not force and self._browser.has_saved_session:
                self._emit("🔍 Kiểm tra session cũ…")
                await page.goto("https://hoadondientu.gdt.gov.vn",
                                wait_until="networkidle", timeout=30_000)
                for c in await page.context.cookies():
                    if c["name"].lower() == "jwt" and c.get("value"):
                        self._emit("✅ Dùng lại JWT từ session cũ.")
                        return c["value"]
                self._emit("⚠️ Session cũ không có JWT — tiến hành đăng nhập…")

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
        self._emit("📡 Phase 1 — Thu thập danh sách hóa đơn…")

        segment_counts: Dict[str, int] = {}
        seen: set = set()
        new_items: List[Tuple[Dict[str, Any], str]] = []
        month_counts: Dict[str, int] = {}

        for sd, ed in self.chunks:
            for cat, path, ttxly, label in _CRAWL_PLAN:
                if self._stop:
                    break

                # Extract "/api/query" hoặc "/api/sco-query" từ path
                path_prefix = "/" + "/".join(path.strip("/").split("/")[:2])

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

                    inv["_api_path_prefix"] = path_prefix

                    key = _dedup_key(inv, cat)
                    if key not in seen:
                        seen.add(key)

                        d  = _parse_gdt_date(inv.get("issue_date"))
                        mk = f"{d.month:02d}/{d.year}" if d else "??"
                        month_counts[mk] = month_counts.get(mk, 0) + 1

                        if self._is_already_crawled(inv, cat):
                            self._total_skipped += 1
                        else:
                            new_items.append((inv, cat))

        self._emit("─" * 64)
        for lbl, cnt in segment_counts.items():
            self._emit(f"  {lbl:<48} {cnt:>8,}")
        self._emit(f"  {'Tổng':<48} {sum(segment_counts.values()):>8,}")
        self._emit("─" * 64)

        if month_counts:
            self._emit("📅 Phân bổ theo tháng:")
            for ym in sorted(month_counts, key=lambda s: (s[3:], s[:2])):
                self._emit(f"  Tháng {ym}: {month_counts[ym]:,} hóa đơn")
        self._emit("─" * 64)

        self._total_to_process = len(new_items)
        self._emit(f"📋 Cần tải mới: {self._total_to_process:,}  |  Đã có: {self._total_skipped:,}")
        self._emit_progress()
        self._update_job(total_invoices=self._total_to_process + self._total_skipped)
        return new_items

    async def _process_all(self, client: GdtApiClient,
                           work_items: List[Tuple[Dict[str, Any], str]]) -> None:
        total = len(work_items)
        self._emit(f"⚡ Phase 2 — Tải {total:,} hóa đơn…")

        for idx, (inv, category) in enumerate(work_items, start=1):
            if self._stop:
                break

            try:
                result = await self._process_invoice(client, inv, category)
            except Exception as exc:
                logger.error("process #{}: {}", inv["invoice_no"], exc)
                result = None

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

    async def _process_invoice(self, client: GdtApiClient, inv: Dict[str, Any],
                                invoice_category: str) -> Optional[Dict[str, Any]]:
        if not invoice_category:
            logger.error("invoice_category is None cho #{}", inv.get("invoice_no"))
            return None

        invoice_no  = inv["invoice_no"]
        issue_date  = _parse_gdt_date(inv["issue_date"])
        inv_dir     = ensure_invoice_dir(invoice_no, issue_date, suffix=invoice_category)
        path_prefix = inv.get("_api_path_prefix", "/api/query")

        nbmst    = inv["seller_tax_code"]
        khhdon   = inv["invoice_symbol"]
        shdon    = invoice_no
        khmshdon = inv["invoice_form"] or "1"

        xml_meta: Dict[str, Any]            = {}
        line_items: List[Dict[str, Any]]    = []
        vat_breakdown: List[Dict[str, Any]] = []
        zip_path: Optional[Path]            = None
        extract  = ZipExtractResult()
        view_html_path: Optional[Path]      = None
        has_pdf  = False
        used_fallback = False

        # ── Fetch song song ──────────────────────────────────────────────────
        zip_bytes, detail_bytes = await asyncio.gather(
            client.export_xml(nbmst, khhdon, shdon, khmshdon, path_prefix=path_prefix),
            client.get_detail(nbmst, khhdon, shdon, khmshdon, path_prefix=path_prefix),
        )

        # ── Parse detail JSON ────────────────────────────────────────────────
        detail_json: Optional[Dict[str, Any]] = None
        if detail_bytes:
            try:
                detail_json = json.loads(detail_bytes.decode("utf-8"))
            except Exception as exc:
                logger.error("JSON parse detail #{}: {}", invoice_no, exc)

        # ── Xử lý ZIP / XML ─────────────────────────────────────────────────
        if zip_bytes:
            zip_path = inv_dir / "invoice.zip"
            zip_path.write_bytes(zip_bytes)
            extract = extract_invoice_zip(zip_path, inv_dir / "extracted")

            if extract.data_xml_path and extract.data_xml_path.exists():
                raw      = extract.data_xml_path.read_bytes()
                xml_meta = XmlService.parse_metadata(raw)
                vat_breakdown = xml_meta.pop("vat_breakdown", [])

                xml_items = XmlService.parse_line_items(raw)
                if xml_items:
                    line_items = [_normalize_xml_line_item(i) for i in xml_items]
                    logger.debug("line_items từ XML: {} dòng cho #{}", len(line_items), invoice_no)

        # ── Fallback line_items + vat_breakdown từ detail ────────────────────
        if detail_json:
            if not line_items:
                raw_items = detail_json.get("hdhhdvu") or []
                line_items = [_normalize_detail_line_item(i) for i in raw_items]
                if line_items:
                    logger.debug("line_items từ detail JSON: {} dòng cho #{}", len(line_items), invoice_no)

            if not vat_breakdown:
                vat_breakdown = [
                    {
                        "vat_rate":   str(t.get("tsuat") or ""),
                        "taxable":    float(t.get("thtien") or 0),
                        "vat_amount": float(t.get("tthue")  or 0),
                    }
                    for t in (detail_json.get("thttltsuat") or [])
                    if t.get("tsuat") is not None
                ]

        # ── Build fallback XML stub + HTML từ detail nếu cần ─────────────────
        if detail_json:
            extracted_dir = inv_dir / "extracted"
            extracted_dir.mkdir(parents=True, exist_ok=True)

            if not extract.data_xml_path:
                used_fallback = True
                try:
                    xml_stub      = build_fallback_xml(inv, detail_json)
                    xml_stub_path = extracted_dir / "invoice_stub.xml"
                    xml_stub_path.write_text(xml_stub, encoding="utf-8")
                    extract.data_xml_path = xml_stub_path
                    logger.info("Fallback XML stub created: #{}", invoice_no)
                except Exception as exc:
                    logger.error("build_fallback_xml #{}: {}", invoice_no, exc)

            if not extract.view_html_path:
                used_fallback = True
                try:
                    self._copy_template_images(extracted_dir)
                    html_content       = build_fallback_html(inv, detail_json, line_items=line_items)
                    html_path          = extracted_dir / "invoice_view.html"
                    html_path.write_text(html_content, encoding="utf-8")
                    extract.view_html_path = html_path
                    logger.info("Fallback HTML created: #{}", invoice_no)
                except Exception as exc:
                    logger.error("build_fallback_html #{}: {}", invoice_no, exc)

        if not detail_json and not extract.data_xml_path:
            logger.warning("Không lấy được cả detail lẫn XML: #{}", invoice_no)

        if extract.view_html_path:
            view_html_path = extract.view_html_path

        # ── Build result ──────────────────────────────────────────────────────
        def x(xml_key: str, inv_key: str = "", fallback: Any = "") -> Any:
            return xml_meta.get(xml_key) or (inv.get(inv_key) if inv_key else None) or fallback

        result: Dict[str, Any] = {
            "invoice_no":       normalize_invoice_no(x("invoice_no",     "invoice_no")),
            "invoice_symbol":   x("invoice_symbol",  "invoice_symbol"),
            "invoice_form":     x("invoice_form",    "invoice_form"),
            "invoice_type":     xml_meta.get("invoice_type_gdt") or inv.get("invoice_type"),
            "invoice_category": invoice_category,
            "issue_date":       x("issue_date",      "issue_date"),
            "status":           "fallback" if used_fallback else "downloaded",
            "currency":         x("currency",        "currency"),
            "exchange_rate":    xml_meta.get("exchange_rate")      or inv.get("exchange_rate"),
            "payment_method":   xml_meta.get("payment_method")    or inv.get("payment_method"),
            "xml_version":      xml_meta.get("xml_version")       or inv.get("xml_version"),
            "software_tax_code":xml_meta.get("software_tax_code") or inv.get("software_tax_code"),
            "is_adjustment":    xml_meta.get("is_adjustment")     or inv.get("is_adjustment"),
            "portal_link":      xml_meta.get("portal_link")       or inv.get("portal_link"),
            "fkey":             xml_meta.get("fkey")               or inv.get("fkey"),
            "seller_signing_time": xml_meta.get("seller_signing_time") or inv.get("seller_signing_time"),
            "tax_signing_time":    xml_meta.get("tax_signing_time")    or inv.get("tax_signing_time"),
            "seller_name":      x("seller_name",      "seller_name"),
            "seller_tax_code":  x("seller_tax_code",  "seller_tax_code"),
            "seller_address":   x("seller_address",   "seller_address"),
            "seller_phone":     x("seller_phone",     "seller_phone"),
            "seller_email":     x("seller_email",     "seller_email"),
            "seller_bank":      x("seller_bank",      "seller_bank"),
            "seller_bank_name": x("seller_bank_name", "seller_bank_name"),
            "seller_fax":       x("seller_fax",       "seller_fax"),
            "seller_website":   x("seller_website",   "seller_website"),
            "buyer_name":       x("buyer_name",       "buyer_name"),
            "buyer_tax_code":   x("buyer_tax_code",   "buyer_tax_code"),
            "buyer_address":    x("buyer_address",    "buyer_address"),
            "buyer_phone":      x("buyer_phone",      "buyer_phone"),
            "buyer_email":      x("buyer_email",      "buyer_email"),
            "buyer_bank":       x("buyer_bank",       "buyer_bank"),
            "buyer_bank_name":  x("buyer_bank_name",  "buyer_bank_name"),
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
            "qr_data":          xml_meta.get("qr_data") or inv.get("qr_data"),
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

    def _copy_template_images(self, dest_dir: Path) -> None:
        """Copy ảnh nền + ảnh chữ ký vào cùng thư mục với HTML fallback để render độc lập."""
        static_dir = getattr(self.app, "static_folder", None) if self.app else None
        if not static_dir:
            logger.warning("Không xác định được static_folder — bỏ qua copy ảnh template.")
            return

        src_dir = Path(static_dir) / "img"
        for name in ("viewinvoice-bg.jpg", "sign-check.jpg"):
            dst = dest_dir / name
            if dst.exists():
                continue
            src = src_dir / name
            if src.exists():
                try:
                    shutil.copy2(src, dst)
                except Exception as exc:
                    logger.warning("Copy ảnh template {} lỗi: {}", name, exc)
            else:
                logger.warning("Không tìm thấy ảnh template nguồn: {}", src)

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
                    account_id=self.account_id,
                )
                if not existing:
                    return False
                if existing.status in ("failed", "error"):
                    return False
                if not existing.line_items_json:
                    return False
                return True
            except Exception as exc:
                logger.warning("DB check #{}: {}", inv["invoice_no"], exc)
        return False

    def _persist_invoice(self, result: Dict[str, Any]) -> None:
        if not result.get("invoice_category"):
            logger.error("Bỏ qua persist: invoice_category is None cho #{}", result.get("invoice_no"))
            return

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


# ─────────────────────────────────────────────────────────────────────────────
# Date / datetime helpers
# ─────────────────────────────────────────────────────────────────────────────

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
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
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
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
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


def normalize_invoice_no(no: Any) -> Optional[str]:
    if no is None:
        return None
    no = str(no).strip()
    if not no:
        return None
    normalized = no.lstrip("0")
    return normalized if normalized else "0"