"""The PDF report, printed from the web report page.

HOW IT WORKS
------------
The report is one page of the web app: /?report=<scan id>&print=1 (frontend/src/report/print/). It loads the scan exactly as the console does, builds
the report model with the same code, and draws every section of the report as ordinary HTML and CSS. This module opens that page in headless
Chromium -- the browser the scanner already ships in this image -- waits until the page says it has finished drawing, and prints it to PDF. The web
report and the PDF are therefore the same document, built from the same values: there is no second copy of the report to keep in step.

What the print engine adds around the page is the part a web page cannot repeat by itself: the header on every page (logo, domain, when the scan
ran) and the footer (the informational disclaimer and "Page X of Y"). Both are plain HTML built here from words the page itself reports
(`window.__REPORT_META__`), so the header matches the report it heads.

WHAT THE BROWSER MAY DO
-----------------------
It signs in with the CALLER'S OWN access token, passed into the page's session storage -- nothing is minted and nothing is elevated, so the page can
read exactly what that user could read through the API and no more. Every request the page makes is checked against the one web origin it is
configured to use (`report_frontend_url`, from settings, never from a request); anything else is refused. The token is never logged.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pypdf import PdfReader, PdfWriter

from app.config import Settings
from app.core.exceptions import ReportGenerationError
from app.scoring.dpdp_score import DISCLAIMER

logger = logging.getLogger(__name__)

_LOGO_PATH = Path(__file__).resolve().parents[1] / "static" / "report" / "consiva-logo.png"

# The page margins, in CSS pixels. The top holds the header band (a 5px gradient line over a 52px dark strip); the bottom holds the footer. The sides are
# zero because the header band runs the full width of the page: the report's own content keeps its side padding.
HEADER_BAND_PX = 57
MARGIN_TOP = "66px"
MARGIN_BOTTOM = "58px"

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

# Which report is printed. "free" is the two-page Free Scan report and "full" the eight-page Full Scan report followed by its evidence appendix (both are
# fixed A4 pages the web app draws, with their own header and footer); "long" is the single-flow evidence report: the Free report's first sheet as its cover,
# then its own pages with the print engine's header and footer.
VARIANTS = ("free", "full", "long")


@dataclass(frozen=True)
class RenderedReport:
    pdf: bytes
    filename: str


# --- the page header and footer -------------------------------------------------------------------------------------------------------------------


def _logo_data_uri() -> str:
    try:
        return "data:image/png;base64," + base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    except OSError:  # a missing logo costs the picture, never the report
        logger.warning("The report logo %s could not be read; the header is printed without it", _LOGO_PATH)
        return ""


def build_header_template(domain: str, scanned: str, logo_uri: str) -> str:
    """The header of every page: the brand's gradient line over a dark strip with the logo at the left and the domain and scan time at the right."""
    logo = f'<img src="{logo_uri}" style="height:34px;display:block"/>' if logo_uri else '<b style="color:#fff;font-size:12px">Consiva.ai</b>'
    return (
        # The print engine places a header template about 20px below the top edge of the page; the band is lifted by that much so it starts at the edge,
        # as the brand's header does, and ends above the content (which starts at MARGIN_TOP). Measured in a real PDF; see the tests.
        '<div style="position:relative;top:-20px;width:100%;margin:0;padding:0;font-family:Arial,Liberation Sans,sans-serif;-webkit-print-color-adjust:exact;print-color-adjust:exact">'
        '<div style="height:5px;background:linear-gradient(90deg,#2f6bff,#7a4dff,#12c2e9)"></div>'
        '<div style="height:52px;background:#03070f;display:flex;align-items:center;justify-content:space-between;padding:0 36px;box-sizing:border-box">'
        f"{logo}"
        '<div style="text-align:right;color:#fff">'
        f'<div style="font-size:9px;font-weight:700">{html.escape(domain)}</div>'
        f'<div style="font-size:7.5px;margin-top:3px">{html.escape(scanned)}</div>'
        "</div></div></div>"
    )


def build_footer_template(page_label: str = "Page") -> str:
    """The footer of every page: a rule, the informational disclaimer, and the page number out of the page count."""
    return (
        '<div style="width:100%;box-sizing:border-box;padding:0 36px;font-family:Arial,Liberation Sans,sans-serif;font-size:6.5px;color:#5d6086;'
        '-webkit-print-color-adjust:exact;print-color-adjust:exact">'
        '<div style="border-top:1px solid #e3e6f4;padding-top:6px;display:flex;justify-content:space-between;gap:24px">'
        f'<span style="flex:1">{html.escape(DISCLAIMER)}</span>'
        f'<span style="white-space:nowrap;font-size:7px">{html.escape(page_label)} <span class="pageNumber"></span> of <span class="totalPages"></span></span>'
        "</div></div>"
    )


def _storage_script(token: str, user_id: str, org_id: str, role: str | None) -> str:
    profile = json.dumps({"user_id": user_id, "org_id": org_id, "role": role or "member", "email": ""})
    return (
        "try { sessionStorage.setItem('consiva.access_token', " + json.dumps(token) + "); "
        "sessionStorage.setItem('consiva.profile', " + json.dumps(profile) + "); } catch (e) {}"
    )


def safe_filename(name: str | None, scan_id: str) -> str:
    """A file name the page suggested, kept to plain characters and a .pdf ending; otherwise one made from the scan id."""
    cleaned = _SAFE_FILENAME.sub("-", (name or "").strip()).strip("-.")[:120]
    if not cleaned:
        return f"consiva-privacy-report-{scan_id[:8]}.pdf"
    return cleaned if cleaned.lower().endswith(".pdf") else f"{cleaned}.pdf"


# --- rendering --------------------------------------------------------------------------------------------------------------------------------------


async def _render(scan_id: str, token: str, user_id: str, org_id: str, role: str | None, settings: Settings, variant: str = "long") -> RenderedReport:
    base = settings.report_frontend_url.rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ReportGenerationError("The PDF report is not configured on this server.")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    base_url = f"{base}/?report={scan_id}&print=1"
    timeout_ms = settings.report_pdf_timeout_seconds * 1000

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 794, "height": 1123}, locale="en-GB")

            async def only_the_web_app(route) -> None:
                # A request to anywhere but the configured web origin is refused: the report page has no reason to reach elsewhere, and the browser holds
                # the caller's token.
                if route.request.url.startswith(origin) or route.request.url.startswith("data:"):
                    await route.continue_()
                else:
                    await route.abort()

            await context.route("**/*", only_the_web_app)
            await context.add_init_script(_storage_script(token, user_id, org_id, role))
            async def draw(query: str):
                """Open one part of the report page, wait until it says it is drawn, and settle its fonts and pictures."""
                page = await context.new_page()
                await page.goto(base_url + query, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_function("window.__REPORT_READY__ === true || !!window.__REPORT_ERROR__", timeout=timeout_ms)
                problem = await page.evaluate("window.__REPORT_ERROR__ || null")
                if problem:
                    logger.warning("The report page for scan %s could not be drawn: %s", scan_id, problem)
                    raise ReportGenerationError("The report could not be prepared for this scan.")
                found = await page.evaluate("window.__REPORT_META__ || null") or {}
                # Fonts and pictures settle before the page is printed, so the layout the print engine measures is the final one.
                await page.evaluate("document.fonts ? document.fonts.ready.then(() => true) : true")
                await page.wait_for_function("Array.from(document.images).every(i => i.complete)", timeout=timeout_ms)
                return page, found

            async def fixed_pages(query: str, page=None) -> tuple[bytes, dict]:
                # The fixed pages are A4 sheets with their own header and footer, so the print engine adds nothing and uses the page size the CSS sets.
                page, found = (page, {}) if page is not None else await draw(query)
                pdf_bytes = await page.pdf(
                    format="A4", print_background=True, display_header_footer=False, prefer_css_page_size=True,
                    margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                )
                return pdf_bytes, found

            async def flowing(query: str, page_label: str = "Page", page=None, loaded: dict | None = None) -> tuple[bytes, dict]:
                page, found = (page, loaded or {}) if page is not None else await draw(query)
                pdf_bytes = await page.pdf(
                    format="A4", print_background=True, display_header_footer=True, prefer_css_page_size=False,
                    margin={"top": MARGIN_TOP, "bottom": MARGIN_BOTTOM, "left": "0", "right": "0"},
                    header_template=build_header_template(str(found.get("domain") or ""), str(found.get("scanned") or ""), _logo_data_uri()),
                    footer_template=build_footer_template(page_label),
                )
                return pdf_bytes, found

            if variant == "free":
                pdf, meta = await fixed_pages("&variant=free&part=pages")
            else:
                # The long report opens with the Free report's first sheet as its cover, and the Full report is its eight sheets followed by the appendix. The page is
                # loaded ONCE (loading it reads the scan from the API again) and printed twice: first with only the fixed sheets showing, then with only the flowing
                # pages showing and the sheets' page rule switched off (see print_part in report.css and SheetPageStyle).
                query = "&variant=full" if variant == "full" else ""
                page, meta = await draw(query)
                await page.evaluate("document.documentElement.dataset.printPart = 'sheets'")
                head, _ = await fixed_pages("", page=page)
                await page.evaluate(
                    "document.documentElement.dataset.printPart = 'flow'; "
                    "document.querySelectorAll('style[data-sheet-page]').forEach((e) => { e.media = 'not all'; })"
                )
                tail, _ = await flowing("", "Appendix page" if variant == "full" else "Page", page=page, loaded=meta)
                pdf = _join(head, tail)
        finally:
            await browser.close()
    if not pdf.startswith(b"%PDF"):
        raise ReportGenerationError("The report could not be prepared for this scan.")
    name = safe_filename(meta.get("filename"), scan_id)
    if variant != "long":
        name = f"{name[:-4]}-{variant}.pdf"
    return RenderedReport(pdf=pdf, filename=name)


def _join(*parts: bytes) -> bytes:
    """The parts of one report, in order, as a single PDF."""
    writer = PdfWriter()
    for part in parts:
        for pg in PdfReader(BytesIO(part)).pages:
            writer.add_page(pg)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _run_in_proactor_thread(coro_factory) -> RenderedReport:
    """Windows only: Playwright needs the Proactor event loop and the API runs on the Selector one (the database driver needs it), so the render runs in
    a thread of its own with its own loop. Linux, where this is deployed, runs it in the API's own loop."""
    result: dict = {}

    def target() -> None:
        loop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
        try:
            result["value"] = loop.run_until_complete(coro_factory())
        except BaseException as exc:  # noqa: BLE001 -- handed back to the awaiting caller
            result["error"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=target, name="report-pdf", daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


# --- the public entry point, with a short-lived cache and a cap on browsers ---------------------------------------------------------------------------

_cache: dict[tuple[str, str, str], tuple[float, RenderedReport]] = {}
_slots: dict[int, asyncio.Semaphore] = {}


def _slot(settings: Settings) -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _slots:
        _slots[loop_id] = asyncio.Semaphore(settings.report_pdf_max_concurrent)
    return _slots[loop_id]


def clear_cache() -> None:
    _cache.clear()


async def render_report_pdf(
    scan_id: uuid.UUID, *, version: str, token: str, user_id: str, org_id: str, role: str | None, settings: Settings, variant: str = "long",
) -> RenderedReport:
    """The PDF for one completed scan. The caller has ALREADY checked that this user may read the scan; this only prints it.

    `version` names the state of the scan the report describes (when it completed), so a re-run is never answered from a stale copy. A finished PDF
    is reused for `report_pdf_cache_seconds`: it is the same document for anyone who may read the scan, and a browser per click would be wasteful.
    """
    if variant not in VARIANTS:
        raise ReportGenerationError("Unknown report type.")
    key = (str(scan_id), version, variant)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    for k in [k for k, (expires, _) in _cache.items() if expires <= now]:
        _cache.pop(k, None)

    async with _slot(settings):
        started = time.monotonic()
        try:
            factory = lambda: _render(str(scan_id), token, user_id, org_id, role, settings, variant)
            rendered = await asyncio.to_thread(_run_in_proactor_thread, factory) if sys.platform == "win32" else await factory()
        except ReportGenerationError:
            raise
        except (PlaywrightError, TimeoutError, OSError) as exc:
            logger.exception("PDF report for scan %s failed (%s)", scan_id, type(exc).__name__)
            raise ReportGenerationError("The PDF report could not be prepared. Please try again in a moment.") from exc
        except Exception as exc:
            logger.exception("PDF report for scan %s failed unexpectedly", scan_id)
            raise ReportGenerationError("The PDF report could not be prepared. Please try again in a moment.") from exc
        logger.info("PDF report for scan %s: %d bytes in %.1fs", scan_id, len(rendered.pdf), time.monotonic() - started)

    if settings.report_pdf_cache_seconds > 0:
        _cache[key] = (time.monotonic() + settings.report_pdf_cache_seconds, rendered)
    return rendered
