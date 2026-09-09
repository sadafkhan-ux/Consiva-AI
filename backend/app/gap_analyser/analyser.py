"""`analyse(url) -> dict` -- the pre-consent consent-gap analyser.

The measurement: open the page in a FRESH browser context and never click anything.
No "Accept", no banner dismissal. Whatever state exists after load is by definition
the pre-consent state. Clicking accept would make the data worthless, so this module
contains no interaction code at all -- that is a deliberate absence, not an omission.

Compliance findings here are exact string/DOM matching only. The single LLM touchpoint
(three soft descriptive fields) lives in soft_fields.py and can never fail the run.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from app.gap_analyser import detect
from app.gap_analyser.scoring import build_summary, score_consent_gap
from app.scanner._domain import registered_domain

logger = logging.getLogger(__name__)

# Gotcha 1: Hotjar and Clarity fire well after DOMContentLoaded. Waiting for
# networkidle and THEN settling is what makes the reading stable -- a 2.5s window
# missed two trackers on a real site and moved its score 16 points between runs.
_NAV_TIMEOUT_MS = 20_000
_NETWORKIDLE_CAP_MS = 8_000
_SETTLE_MS = 4_000

# Gotcha 4: page builders scatter <article> over small widgets. Take the LARGEST
# matching container, and fall back to the whole body when it holds less than this
# share of the body text -- one WordPress site returned 207 chars out of 10,016 and
# every extracted field came back null as a result.
_MAIN_TEXT_MIN_SHARE = 0.40

# Everything the analysis needs from the DOM, collected in a single evaluate() so the
# page is only walked once.
_EXTRACT_JS = r"""
() => {
  const scriptSrcs = Array.from(document.querySelectorAll('script[src]'))
    .map(s => s.getAttribute('src') || '');

  const links = Array.from(document.querySelectorAll('a[href]'))
    .slice(0, 800)
    .map(a => ({ href: a.getAttribute('href') || '', text: (a.textContent || '').trim().slice(0, 120) }));

  const descriptorFor = (el) => {
    const bits = [el.getAttribute('name'), el.getAttribute('id'), el.getAttribute('placeholder'),
                  el.getAttribute('type'), el.getAttribute('aria-label')];
    // Associated <label> text -- the `labels` property handles for=/wrapping for us.
    if (el.labels) { for (const l of el.labels) bits.push(l.textContent || ''); }
    return bits.filter(Boolean).join(' ');
  };

  const forms = Array.from(document.querySelectorAll('form')).map(form => {
    const fields = Array.from(form.querySelectorAll('input, textarea, select'))
      .filter(el => !['hidden', 'submit', 'button', 'image', 'reset'].includes((el.getAttribute('type') || '').toLowerCase()));

    const checkboxes = Array.from(form.querySelectorAll('input[type=checkbox]')).map(cb => {
      let ctx = descriptorFor(cb);
      if (cb.parentElement) ctx += ' ' + (cb.parentElement.textContent || '');
      return ctx;
    });

    const anchors = Array.from(form.querySelectorAll('a[href]'))
      .map(a => (a.getAttribute('href') || '') + ' ' + (a.textContent || ''));

    return {
      action: form.getAttribute('action') || '',
      field_descriptors: fields.map(descriptorFor),
      checkbox_contexts: checkboxes,
      anchor_contexts: anchors,
    };
  });

  // Largest main/article/#content container, plus the body total for the share check.
  const candidates = Array.from(document.querySelectorAll('main, article, #content, .content, .entry-content'));
  let best = '';
  for (const c of candidates) {
    const t = (c.innerText || '').trim();
    if (t.length > best.length) best = t;
  }
  const bodyText = (document.body ? document.body.innerText : '') || '';

  return {
    scriptSrcs, links, forms,
    mainText: best,
    bodyText: bodyText,
    title: document.title || '',
  };
}
"""


def _pick_page_text(main_text: str, body_text: str) -> str:
    """Gotcha 4 in one place: prefer the largest semantic container, but fall back to
    the full body whenever that container holds too little of the page."""
    if not body_text:
        return main_text
    if main_text and len(main_text) >= _MAIN_TEXT_MIN_SHARE * len(body_text):
        return main_text
    return body_text


def _empty_result(url: str, status: str, *, error: str | None = None) -> dict:
    """Shape-stable base record. EVERY key below is present on every returned result,
    success or failure -- including `error`, which is simply null on success. A caller
    batching thousands of URLs into a table or dataframe must not have the column set
    shift depending on whether a page happened to load.

    `error` is the one field beyond the documented output shape; it carries the real
    exception text for an unreachable page instead of discarding why it failed.

    A failed page is never scored: consent_gap_score stays null rather than 0, because
    "we could not measure this" and "this site is clean" must never look alike."""
    return {
        "url": url, "final_url": None, "final_domain": None,
        "http_status": None, "page_title": None,
        "has_cmp": False, "cmp_name": None,
        "has_cookie_banner": False, "has_privacy_policy": False,
        "cookies_total": 0, "tracking_cookie_count": 0,
        "third_party_count": 0, "third_party_domains": [],
        "max_cookie_lifetime_days": None, "long_lived_count": 0,
        "pre_consent_cookies": [],
        "has_data_form": False, "form_field_types": [],
        "form_consent_checkbox": False, "form_privacy_link": False,
        "form_third_party_hosts": [], "form_embed_provider": None,
        "tech_cms": None, "tech_ecommerce_platform": None, "tech_payment_gateway": None,
        "tech_analytics_tools": [], "tech_marketing_tools": [],
        "consent_gap_score": None, "consent_gap_summary": None,
        "company_tagline": None, "founder_name": None, "notable_clients": [],
        "page_status": status,
        "error": error,
    }


async def analyse_async(url: str, *, use_llm: bool = True) -> dict:
    """Analyse one URL's pre-consent state. Never raises for an unreachable or hostile
    page -- those come back as a shaped record with `page_status` set."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            # Fresh context per URL: no shared cookie jar, no state carried in from a
            # previous site. This is what makes the reading genuinely "pre-consent".
            context = await browser.new_context()
            page = await context.new_page()

            try:
                response = await page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            except PlaywrightError as exc:
                logger.info("Navigation failed for %s: %s", url, exc)
                return _empty_result(url, "unreachable", error=f"{type(exc).__name__}: {exc}")

            http_status = response.status if response else None

            # Gotcha 1. networkidle is best-effort: pages with long-poll/websocket
            # traffic never go idle, so the cap is a timeout we expect to hit
            # sometimes, not an error.
            try:
                await page.wait_for_load_state("networkidle", timeout=_NETWORKIDLE_CAP_MS)
            except PlaywrightError:
                pass
            await page.wait_for_timeout(_SETTLE_MS)

            try:
                html = await page.content()
                extracted = await page.evaluate(_EXTRACT_JS)
                raw_cookies = await context.cookies()
            except PlaywrightError as exc:
                logger.info("Extraction failed for %s: %s", url, exc)
                return _empty_result(url, "unreachable", error=f"{type(exc).__name__}: {exc}")

            final_url = page.url
            # Gotcha 2: classify party against the FINAL domain. Sites redirect, and
            # comparing against the requested host mislabels every first-party cookie.
            final_domain = registered_domain(urlparse(final_url).netloc)

            title = extracted.get("title") or ""
            body_text = extracted.get("bodyText") or ""
            page_text = _pick_page_text(extracted.get("mainText") or "", body_text)

            status = detect.detect_page_status(
                title=title, body_text=body_text,
                http_status=http_status, final_domain=final_domain,
            )
            if status != "ok":
                # A parked or blocked page is never scored -- its "zero trackers" would
                # otherwise read as a clean bill of health.
                result = _empty_result(url, status)
                result.update({
                    "final_url": final_url, "final_domain": final_domain,
                    "http_status": http_status, "page_title": title,
                })
                return result

            script_srcs = extracted.get("scriptSrcs") or []
            links = extracted.get("links") or []

            # Gotcha 3: values are dropped inside build_cookie_record and never stored.
            cookies = [detect.build_cookie_record(c, final_domain) for c in raw_cookies]

            has_cmp, cmp_name = detect.detect_cmp(html, script_srcs)
            forms_in = [
                {
                    "action": f.get("action"),
                    "field_descriptors": f.get("field_descriptors") or [],
                    "has_consent_checkbox": any(
                        detect.T.CONSENT_CHECKBOX_PATTERN.search(c)
                        for c in (f.get("checkbox_contexts") or [])
                    ),
                    "has_privacy_link": any(
                        detect.T.FORM_PRIVACY_LINK_PATTERN.search(a)
                        for a in (f.get("anchor_contexts") or [])
                    ),
                }
                for f in (extracted.get("forms") or [])
            ]

            result = _empty_result(url, "ok")
            result.update({
                "url": url, "final_url": final_url, "final_domain": final_domain,
                "http_status": http_status, "page_title": title,
                "has_cmp": has_cmp, "cmp_name": cmp_name,
                "has_cookie_banner": has_cmp or detect.detect_cookie_banner(html, page_text),
                "has_privacy_policy": detect.detect_privacy_policy(links),
                "pre_consent_cookies": cookies,
            })
            result.update(detect.summarise_cookies(cookies))
            # Tracker-only lifetime stats: the score weights long-lived TRACKERS, and
            # the summary must not describe a functional cookie as a tracker.
            max_tracker_life, long_lived_trackers = detect.tracker_lifetime_stats(cookies)
            result.update(detect.analyse_forms(forms_in, final_url))
            result["form_embed_provider"] = detect.detect_form_provider(html, script_srcs)
            result.update(detect.detect_tech(html, script_srcs))

            result["consent_gap_score"] = score_consent_gap(
                has_cmp=result["has_cmp"],
                has_cookie_banner=result["has_cookie_banner"],
                has_privacy_policy=result["has_privacy_policy"],
                cookies=cookies,
                third_party_count=result["third_party_count"],
                long_lived_count=long_lived_trackers,
                has_data_form=result["has_data_form"],
                form_consent_checkbox=result["form_consent_checkbox"],
            )
            result["consent_gap_summary"] = build_summary({
                **result,
                "max_tracker_lifetime_days": max_tracker_life,
                "long_lived_tracker_count": long_lived_trackers,
            })

            if use_llm:
                # Soft fields only, and strictly best-effort: an unreachable model
                # leaves them null and the analysis still stands.
                from app.gap_analyser.soft_fields import extract_soft_fields
                result.update(await extract_soft_fields(page_text, title))

            return result
        finally:
            await browser.close()


def analyse(url: str, *, use_llm: bool = True) -> dict:
    """Synchronous entry point -- the function the spec asks for."""
    return asyncio.run(analyse_async(url, use_llm=use_llm))
