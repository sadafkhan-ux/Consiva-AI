"""Prints the report HTML to PDF with headless Chromium (Playwright): A4, margins, backgrounds, and a running header and footer with the page number.

The page is loaded from a string and every request is blocked except about: and data:, so rendering can reach nothing on the network. A timeout bounds the whole step. The page count is read back
from the finished PDF with pypdf, not assumed. This does not use the 5-page report's print service.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
from html import escape

from playwright.async_api import async_playwright
from pypdf import PdfReader

PDF_TIMEOUT_S = 120.0
MARGIN = {"top": "22mm", "bottom": "20mm", "left": "16mm", "right": "16mm"}
_META = re.compile(r'<meta name="consiva-report-(scan|site)" content="([^"]*)"')


class PdfRenderError(RuntimeError):
    """The report could not be printed to PDF."""


def _identity(html: str) -> tuple[str, str]:
    found = {m.group(1): m.group(2) for m in _META.finditer(html)}      # the content is already HTML-escaped by the renderer
    return found.get("scan", ""), found.get("site", "")


def header_footer(scan_id: str, site: str) -> tuple[str, str]:
    """The Chromium header and footer templates. Their text is HTML, so it is escaped here as well."""
    style = "font-family:Arial,Helvetica,sans-serif;font-size:7.5px;color:#566074;width:100%;padding:0 16mm;display:flex;justify-content:space-between;-webkit-print-color-adjust:exact;"
    sid, host = escape(scan_id, quote=True), escape(site, quote=True)
    header = f'<div style="{style}border-bottom:0.5px solid #d3d9e3;padding-bottom:3px;"><span>Consiva Enterprise | DPDP readiness report | {host}</span><span>Scan {sid}</span></div>'
    footer = (f'<div style="{style}"><span>Website-observable readiness assessment, not a certification of compliance | Scan {sid}</span>'
              '<span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>')
    return header, footer


async def _print(html: str, header: str, footer: str, margin: dict | None = None) -> bytes:
    args = ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=args)
        try:
            context = await browser.new_context(java_script_enabled=False)

            async def only_local(route):
                if route.request.url.startswith(("about:", "data:")):
                    await route.continue_()
                else:
                    await route.abort()

            await context.route("**/*", only_local)
            page = await context.new_page()
            await page.set_content(html, wait_until="load")
            await page.emulate_media(media="print")
            return await page.pdf(format="A4", margin=margin or MARGIN, print_background=True, display_header_footer=True, header_template=header, footer_template=footer, prefer_css_page_size=False)
        finally:
            await browser.close()


async def render_pdf(html: str, *, timeout_s: float = PDF_TIMEOUT_S, header: str | None = None, footer: str | None = None, margin: dict | None = None) -> tuple[bytes, int]:
    """(the PDF bytes, its page count). Raises PdfRenderError if Chromium fails or the time limit passes. A report with its own running header and footer (the method 3 report)
    passes them, and its margins; otherwise the shared ones are used."""
    scan_id, site = _identity(html)
    # The identity was escaped for HTML attributes; unescape once so the template escaping is not doubled.
    from html import unescape
    if header is None or footer is None:
        header, footer = header_footer(unescape(scan_id), unescape(site))
    try:
        data = await asyncio.wait_for(_print(html, header, footer, margin), timeout_s)
    except asyncio.TimeoutError as exc:
        raise PdfRenderError(f"printing the report took longer than {timeout_s:g} seconds") from exc
    except Exception as exc:
        raise PdfRenderError(f"{type(exc).__name__}: {exc}"[:500]) from exc
    if not data.startswith(b"%PDF"):
        raise PdfRenderError("the browser did not return a PDF")
    return data, len(PdfReader(io.BytesIO(data)).pages)
