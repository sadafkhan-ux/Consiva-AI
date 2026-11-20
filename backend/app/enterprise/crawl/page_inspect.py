"""Deep inspection of one rendered page: scroll it through, open its accordions, read what hides in shadow DOM and iframes, and turn what the browser saw into plain evidence.

Two parts. The pure part (cookies, requests, forms, headers, soft-404) takes plain data and returns plain data, so it is tested without a browser. The browser part drives a Playwright
page and is bounded everywhere: a page that never stops growing, or has a thousand collapsed sections, costs a fixed amount of time and says that it was cut short, instead of
holding a worker or pretending it was read in full.

Nothing here types into a field, submits a form, follows a link or clicks anything that is not a collapsed section. The values of fields are never read.
"""

from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import urlparse

from app.scanner._domain import registered_domain

#: The response headers the technical assessment looks at.
SECURITY_HEADERS = (
    "strict-transport-security", "content-security-policy", "x-content-type-options", "referrer-policy", "permissions-policy", "x-frame-options",
)
MAX_REQUESTS_KEPT = 5000
EXAMPLES_PER_HOST, EXAMPLE_HOSTS = 3, 60
SCROLL_MAX_STEPS, SCROLL_PAUSE_MS, SCROLL_DEADLINE_S = 80, 250, 30.0
EXPAND_LIMIT = 100                    # per page; sections opened inside a section need a further round, so the rounds below share this total
EXPAND_ROUNDS = 3
#: Most characters kept from one shadow root or one same-site iframe. Was 20,000, which cut a long embedded policy; reaching this limit is now recorded as a note on the page.
SHADOW_TEXT_LIMIT = 400_000


# ---- cookies --------------------------------------------------------------------------------------------------------------------------------------------------------

def cookie_identity(name: str, domain: str, path: str | None) -> str:
    """One cookie is its name, its domain without the leading dot, and its path. The same cookie seen on ten pages is one identity with ten observations."""
    return f"{name}|{(domain or '').lstrip('.').lower()}|{path or '/'}"


def normalize_cookies(raw: list[dict], site_url: str, consent_state: str = "pre_consent") -> list[dict]:
    """Browser cookies as evidence. The value itself is not kept (it can be a personal identifier): only its length and a short fingerprint, which is enough to tell
    whether a cookie changed between two observations."""
    site = registered_domain(site_url)
    out: dict[str, dict] = {}
    for c in raw:
        identity = cookie_identity(c.get("name", ""), c.get("domain", ""), c.get("path"))
        value = c.get("value") or ""
        expires = c.get("expires")
        out[identity] = {
            "identity": identity, "name": c.get("name", ""), "domain": (c.get("domain") or "").lstrip(".").lower(), "path": c.get("path") or "/",
            "expires": None if expires in (None, -1) else expires, "session": expires in (None, -1),
            "secure": bool(c.get("secure")), "http_only": bool(c.get("httpOnly")), "same_site": c.get("sameSite") or "unspecified",
            "first_party": registered_domain((c.get("domain") or "").lstrip(".")) == site, "consent_state": consent_state,
            "value_length": len(value), "value_fingerprint": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else None,
        }
    return sorted(out.values(), key=lambda c: c["identity"])


# ---- network requests -----------------------------------------------------------------------------------------------------------------------------------------

def summarise_requests(entries: list[dict], site_url: str, consent_state: str = "pre_consent") -> dict:
    """Every request the page made, counted by host: which hosts, how many, what kind, and whether the host is the site's own. Counts, not a list of every URL, so a page that
    loads two hundred images does not become two hundred evidence rows."""
    site = registered_domain(site_url)
    hosts: dict[str, dict] = {}
    for e in entries:
        host = (urlparse(e.get("url", "")).hostname or "").lower()
        if not host:
            continue
        h = hosts.setdefault(host, {"host": host, "third_party": registered_domain(host) != site, "requests": 0, "types": {}})
        h["requests"] += 1
        t = e.get("type") or "other"
        h["types"][t] = h["types"].get(t, 0) + 1
    rows = sorted(hosts.values(), key=lambda h: (not h["third_party"], -h["requests"], h["host"]))
    # Requests made in clear text (http://) by a page that was itself loaded over HTTPS: mixed content. The page's own scheme is decided by the caller.
    insecure = sorted({e["url"].split("#")[0][:300] for e in entries if str(e.get("url", "")).lower().startswith("http://")})
    # A few of the actual request URLs behind each third-party host (method 3.0.0 report: "the request URLs behind a finding"), capped so a page stays a small record.
    examples: dict[str, list[str]] = {}
    for e in entries:
        host = (urlparse(e.get("url", "")).hostname or "").lower()
        if host and registered_domain(host) != site:
            lst = examples.setdefault(host, [])
            u = str(e["url"]).split("#")[0][:300]
            if len(lst) < EXAMPLES_PER_HOST and u not in lst:
                lst.append(u)
    return {
        "consent_state": consent_state, "total_requests": sum(h["requests"] for h in rows), "hosts": rows, "insecure_requests": len(insecure), "insecure_examples": insecure[:5],
        "third_party_hosts": sum(1 for h in rows if h["third_party"]), "third_party_requests": sum(h["requests"] for h in rows if h["third_party"]),
        "examples": dict(list(examples.items())[:EXAMPLE_HOSTS]),
    }


# ---- headers --------------------------------------------------------------------------------------------------------------------------------------------------------

def security_headers(headers: dict[str, str], final_url: str) -> dict:
    """Which protective headers the page's own response carried, and which it did not. Absence is recorded as absence, not as a verdict."""
    lowered = {k.lower(): v for k, v in headers.items()}
    present = {h: lowered[h] for h in SECURITY_HEADERS if h in lowered}
    return {"https": urlparse(final_url).scheme == "https", "present": present, "missing": [h for h in SECURITY_HEADERS if h not in lowered]}


# ---- soft 404 -------------------------------------------------------------------------------------------------------------------------------------------------------

_NOT_FOUND = re.compile(r"\b(404|page not found|not found|no longer available|does not exist|can[’']?t be found|couldn[’']?t find)\b", re.I)


def is_soft_404(title: str | None, text: str, http_status: int | None) -> bool:
    """A page that answered 200 but says it is not there: a not-found phrase in its title or opening lines, and very little else. Deliberately conservative, because a
    wrongly excluded page is a page the assessment never sees, while a missed soft 404 only costs one extra page."""
    if http_status is not None and http_status != 200:
        return False
    opening = " ".join(filter(None, [title or "", text[:300]]))
    return bool(_NOT_FOUND.search(opening)) and len(text.strip()) < 1500


# ---- forms ----------------------------------------------------------------------------------------------------------------------------------------------------------

def normalize_forms(raw: list[dict], page_url: str) -> list[dict]:
    """Forms as the page described them, with what they collect named. Whether a form is a data collection is kept apart from whether it explains itself: both are recorded, and
    neither is judged here."""
    forms = []
    for i, f in enumerate(raw):
        fields = [x for x in f.get("fields", []) if x.get("type") != "hidden"]
        action = f.get("action") or page_url
        forms.append({
            "index": i, "page_url": page_url, "action": action, "action_host": (urlparse(action).hostname or "").lower(), "method": (f.get("method") or "get").lower(),
            "fields": fields, "hidden_field_count": len(f.get("fields", [])) - len(fields),
            "collects": sorted({k for k in (_collects(x) for x in fields) if k}),
            "consent_controls": [x for x in fields if x.get("type") == "checkbox"],
            # Checkboxes and radios the page ships already ticked, and the label text near the form: read by the text analysis (C3, N8), never judged here.
            "preticked": [x for x in fields if x.get("type") in ("checkbox", "radio") and x.get("checked")],
            "privacy_link": bool(f.get("privacy_link")), "text": (f.get("text") or "")[:600], "label_text": (f.get("label_text") or "")[:800],
        })
    return forms


_COLLECTS = (
    ("email", re.compile(r"e-?mail", re.I)), ("phone", re.compile(r"phone|mobile|tel\b", re.I)), ("name", re.compile(r"\bname\b|first.?name|last.?name|full.?name", re.I)),
    ("address", re.compile(r"address|city|pin.?code|zip|postal", re.I)), ("password", re.compile(r"password", re.I)), ("date of birth", re.compile(r"birth|dob\b", re.I)),
    ("message", re.compile(r"message|comment|enquiry|inquiry|query", re.I)), ("company", re.compile(r"company|organi[sz]ation", re.I)),
)


def _collects(field: dict) -> str | None:
    if field.get("type") == "email":
        return "email"
    if field.get("type") == "tel":
        return "phone"
    if field.get("type") == "password":
        return "password"
    label = " ".join(str(field.get(k) or "") for k in ("name", "id", "label", "placeholder", "autocomplete"))
    return next((kind for kind, rx in _COLLECTS if rx.search(label)), None)


# ---- the browser part -----------------------------------------------------------------------------------------------------------------------------------------

_DOM_JS = """
() => {
  const out = {forms: [], shadow: [], iframes: [], canonical: null, alternates: [], robots: null, lang: document.documentElement.lang || null};
  const txt = (el) => ((el && (el.innerText || el.textContent)) || '').trim();
  const c = document.querySelector('link[rel="canonical"]'); out.canonical = c ? c.href : null;
  document.querySelectorAll('link[rel="alternate"][hreflang]').forEach(l => out.alternates.push({lang: l.hreflang, href: l.href}));
  const r = document.querySelector('meta[name="robots"]'); out.robots = r ? r.content : null;
  const walk = (root, depth) => { if (depth > 6) return; root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) {
      const t = (el.shadowRoot.textContent || '').trim(); if (t) out.shadow.push(t.slice(0, 400000)); walk(el.shadowRoot, depth + 1); } }); };
  walk(document, 0);
  document.querySelectorAll('form').forEach(f => {
    const fields = [];
    f.querySelectorAll('input, select, textarea').forEach(el => {
      const lab = (el.labels && el.labels[0]) ? txt(el.labels[0]) : (el.getAttribute('aria-label') || '');
      fields.push({tag: el.tagName.toLowerCase(), type: (el.type || el.tagName).toLowerCase(), name: el.name || '', id: el.id || '', required: !!el.required,
                   autocomplete: el.autocomplete || '', placeholder: el.placeholder || '', label: lab.slice(0, 200),
                   checked: (el.type === 'checkbox' || el.type === 'radio') ? !!el.checked : false});
    });
    const labels = [...f.querySelectorAll('label, legend')].map(txt).filter(Boolean).join(' | ').slice(0, 600);
    const near = (f.getAttribute('aria-label') || '') + ' ' + txt(f.previousElementSibling).slice(0, 200);
    out.forms.push({action: f.action || '', method: f.method || 'get', fields, privacy_link: !!f.querySelector('a[href*="privacy" i]'), text: txt(f).slice(0, 600),
                    label_text: (labels + ' ' + near).trim().slice(0, 800)});
  });
  document.querySelectorAll('iframe').forEach(i => out.iframes.push({src: i.src || '', title: i.title || ''}));
  return out;
}
"""

_EXPAND_JS = """
(limit) => {
  // Only controls that LEAVE the page or change an account are skipped. Words like delete, remove, send or order appear in policy headings ("Your right to delete your data", "How we send data"), so they are not a reason to skip.
  const bad = /log ?out|sign ?out|check ?out|buy now|add to (cart|basket)|place (your )?order/i;
  const els = [...document.querySelectorAll('details:not([open]) > summary, button[aria-expanded="false"], [role="button"][aria-expanded="false"], [role="tab"][aria-selected="false"], [aria-expanded="false"][aria-controls]')];
  let n = 0, seen = els.length;
  for (const el of els) {
    if (n >= limit) break;
    if (el.closest('form') || el.tagName === 'A' || el.getAttribute('type') === 'submit') continue;
    const label = (el.innerText || el.getAttribute('aria-label') || '').slice(0, 80);
    if (bad.test(label)) continue;
    try { el.click(); n++; } catch (e) {}
  }
  return {opened: n, candidates: seen};
}
"""


async def wait_until_stable(page, *, max_ms: int = 8000, poll_ms: int = 500) -> dict:
    """Wait until the page has stopped changing: its height and the amount of text in it the same on two checks in a row. A single-page app or an intro animation can still be
    drawing when the load event fires, and scrolling a half-drawn page reads half a page. Gives up at `max_ms` and says so."""
    waited, last, same = 0, None, 0
    while waited < max_ms:
        state = await page.evaluate("[document.documentElement.scrollHeight, document.body ? document.body.innerText.length : 0]")
        same = same + 1 if state == last else 0
        if same >= 2:
            return {"stable": True, "waited_ms": waited}
        last = state
        await page.wait_for_timeout(poll_ms)
        waited += poll_ms
    return {"stable": False, "waited_ms": waited}


async def scroll_page(page, *, max_steps: int = SCROLL_MAX_STEPS, pause_ms: int = SCROLL_PAUSE_MS, deadline_s: float = SCROLL_DEADLINE_S) -> dict:
    """Scroll from top to bottom in screen-sized steps, waiting after each so lazy-loaded content can arrive, and stop when the page stops growing. A page that keeps growing
    (infinite scroll) stops at the step or time limit and says so: `stopped` is "bottom", "max_steps" or "deadline"."""
    started, steps, last_height, stable = time.monotonic(), 0, -1, 0
    stopped, height = "max_steps", 0
    while steps < max_steps:
        if time.monotonic() - started > deadline_s:
            stopped = "deadline"
            break
        await page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 0.9, 400))")
        await page.wait_for_timeout(pause_ms)
        height, position = await page.evaluate("[document.documentElement.scrollHeight, window.scrollY + window.innerHeight]")
        steps += 1
        if position >= height - 2:
            stable = stable + 1 if height == last_height else 0
            if stable >= 1:
                stopped = "bottom"
                break
        last_height = height
    await page.evaluate("window.scrollTo(0, 0)")
    return {"steps": steps, "height": height, "stopped": stopped}


async def expand_sections(page, *, limit: int = EXPAND_LIMIT) -> dict:
    """Open collapsed sections (details, accordions, tabs) so their text is on the page. Buttons inside forms, links, and anything labelled like a purchase or a sign-out are left
    alone. `candidates` against `opened` shows when the limit cut it short."""
    total, candidates, rounds = 0, 0, 0
    while rounds < EXPAND_ROUNDS and total < limit:
        result = await page.evaluate(_EXPAND_JS, limit - total)
        rounds += 1
        candidates = max(candidates, result["candidates"])
        total += result["opened"]
        if not result["opened"]:
            break
        await page.wait_for_timeout(300)                # a section opened inside a section shows up for the next round
    return {"opened": total, "candidates": candidates, "rounds": rounds}


async def collect_dom(page, site_url: str) -> dict:
    """Forms, canonical and alternate-language links, the robots meta, shadow DOM text, and the text of iframes on the same site."""
    raw = await page.evaluate(_DOM_JS)
    same_site = registered_domain(site_url)
    frames = []
    for frame in page.frames[1:]:
        if not frame.url or registered_domain(frame.url) != same_site:
            continue
        try:
            frames.append({"src": frame.url, "text": (await frame.evaluate("document.body ? document.body.innerText : ''"))[:SHADOW_TEXT_LIMIT]})
        except Exception:                               # a frame that detached or never loaded is not a reason to lose the page
            continue
    raw["frame_text"] = frames
    return raw
