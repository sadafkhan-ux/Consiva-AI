"""A web server that hosts many invented websites, for load-testing the scanner without touching anyone else's site.

One process answers for every `site-NNN.loadtest.example` (and every third-party address those pages mention, such as the analytics and advertising hosts, which the
load-test launchers point here as well, so nothing leaves the machine). The site is told from the Host header; its character from `NNN % 10` (a profile), and the
last few numbers are sites that fail on purpose. Everything is deterministic: the same site gives the same pages every time, so a scan taken alone and the same
scan taken among a hundred others must give the same evidence, and any difference is the system's, not the site's.

TEST TOOLING ONLY. Nothing in the application imports this.
"""

import asyncio
import os
import re
from dataclasses import dataclass, field, replace

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

PIXEL = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")

GTAG = "https://www.googletagmanager.com/gtag/js?id=G-LOADTEST1"
FBQ = "https://connect.facebook.net/en_US/fbevents.js"


@dataclass(frozen=True)
class Profile:
    name: str
    pages: int = 4
    pre_trackers: tuple[str, ...] = ()        # scripts that load before any consent choice
    post_trackers: tuple[str, ...] = (GTAG,)  # scripts that load after "Accept all"
    policies: tuple[str, ...] = ("privacy-policy", "cookie-policy")
    banner: bool = True
    reject_button: bool = True
    forms: int = 1
    delay: float = 0.03                       # seconds before every response
    heavy: int = 0                            # extra images and a script that keeps a renderer busy
    extra: dict = field(default_factory=dict)


PROFILES: dict[int, Profile] = {
    0: Profile("clean_banner"),
    1: Profile("tracks_before_consent", pages=5, pre_trackers=(GTAG, FBQ), delay=0.05),
    2: Profile("no_banner", pages=3, banner=False, pre_trackers=(FBQ,), policies=("privacy-policy",)),
    3: Profile("no_policy", pages=4, policies=()),
    4: Profile("no_reject_button", pages=4, reject_button=False),
    5: Profile("many_pages", pages=8),
    6: Profile("slow_site", pages=5, delay=0.35),
    7: Profile("many_forms", pages=4, forms=3),
    8: Profile("heavy_pages", pages=5, heavy=120, delay=0.05),
    9: Profile("minimal", pages=2, policies=("privacy-policy",), forms=0),
}

# sites that fail on purpose, by number
# LOADTEST_HEAVY=1 makes EVERY site as heavy as the heavy profile, with LOADTEST_BUSY_MS of script work on every page (a real site's JavaScript costs far more CPU than a
# light fixture's, so this is the setting that tests whether the scanner protects the machine). It changes how much work a page costs, never what the page says.
HEAVY_ALL = os.environ.get("LOADTEST_HEAVY", "0") == "1"
BUSY_MS = int(os.environ.get("LOADTEST_BUSY_MS", "220"))

FAILING = {95: "slow_pages", 96: "forbidden", 97: "robots_block", 98: "flaky_503", 99: "blank_page"}
_flaky_seen: dict[str, int] = {}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def site_number(host: str) -> int | None:
    m = re.match(r"site-(\d{3})\.loadtest\.example", host.split(":")[0])
    return int(m.group(1)) if m else None


def profile_for(n: int) -> Profile:
    p = PROFILES[n % 10]
    return replace(p, heavy=max(p.heavy, 120)) if HEAVY_ALL else p


def banner_html(p: Profile) -> str:
    if not p.banner:
        return ""
    reject = '<button id="reject-all" class="btn">Reject all</button>' if p.reject_button else ""
    return (
        '<div id="cookie-banner" role="dialog" aria-label="Cookie consent" style="position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:2px solid #333;padding:12px;z-index:9999">'
        "<p>We use cookies to improve your experience and to analyse traffic. You can accept all cookies or change your preferences.</p>"
        '<button id="accept-all" class="btn">Accept all</button>' + reject +
        '<button id="manage" class="btn">Manage preferences</button></div>'
    )


def script_html(p: Profile) -> str:
    pre = "".join(f"load('{src}');" for src in p.pre_trackers)
    pre_cookie = "document.cookie='_ga=GA1.1.100.200; path=/; max-age=63072000';" if p.pre_trackers else ""
    post = "".join(f"load('{src}');" for src in p.post_trackers)
    heavy = f"var t=Date.now();while(Date.now()-t<{BUSY_MS}){{}}" if p.heavy else ""
    hide = "var b=document.getElementById('cookie-banner');if(b){b.style.display='none';}"
    wiring = ""
    if p.banner:
        wiring = (
            "var a=document.getElementById('accept-all');if(a){a.onclick=function(){" + hide +
            "document.cookie='consent=all; path=/';document.cookie='_ga=GA1.1.300.400; path=/; max-age=63072000';" + post + "};}"
            "var r=document.getElementById('reject-all');if(r){r.onclick=function(){" + hide + "document.cookie='consent=none; path=/';};}"
            "var m=document.getElementById('manage');if(m){m.onclick=function(){};}"
        )
    return (
        "<script>function load(s){var e=document.createElement('script');e.src=s;e.async=true;document.head.appendChild(e);}"
        + pre_cookie + pre + heavy + wiring + "</script>"
    )


def page_html(n: int, host: str, page: int, p: Profile) -> str:
    marker = f"site-{n:03d}"
    links = "".join(f'<a href="/page/{i}">Page {i}</a> ' for i in range(1, p.pages + 1))
    policy_links = "".join(f'<a href="/{x}">{x.replace("-", " ").title()}</a> ' for x in p.policies)
    forms = "".join(
        f'<form action="/submit/{i}" method="post"><label>Email <input type="email" name="email" required></label><button type="submit">Send</button></form>'
        for i in range(p.forms)
    )
    imgs = "".join(f'<img src="/asset/{i}.png" width="1" height="1" alt="">' for i in range(p.heavy))
    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{marker} {p.name} page {page}</title></head><body>"
        f"<h1>{marker} ({p.name})</h1><nav>{links}{policy_links}</nav><main><p>Content of page {page} of {marker}.</p>{forms}{imgs}</main>"
        f"{banner_html(p)}{script_html(p)}</body></html>"
    )


def policy_html(n: int, which: str) -> str:
    marker = f"site-{n:03d}"
    contact = f"Our Data Protection Officer can be reached at dpo@{marker}.loadtest.example. We respond to every grievance within 30 days." if which == "privacy-policy" else ""
    body = (
        "We collect your name, email address and usage data to provide our services. We process this data on the basis of your consent. You can withdraw consent at any time. "
        f"To exercise your rights to access, correct or erase your data, write to privacy@{marker}.loadtest.example. {contact} "
    ) * 3 if which == "privacy-policy" else (
        "This cookie policy lists the cookies we use: _ga (analytics, 2 years) and consent (functional, session). Cookies are set only as described here. "
    ) * 4
    return f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{marker} {which}</title></head><body><h1>{which.replace('-', ' ').title()}</h1><p>{body}</p></body></html>"


@app.api_route("/{path:path}", methods=["GET", "POST", "HEAD"])
async def serve(request: Request, path: str):
    host = request.headers.get("host", "")
    n = site_number(host)
    if n is None:
        # a third-party address the pages mention (analytics, advertising): answer quickly, as a real tracker does, and set nothing
        if path.endswith(".js") or "gtag" in path:
            return Response("/* stub */", media_type="application/javascript")
        return Response(PIXEL, media_type="image/png")

    p = profile_for(n)
    kind = FAILING.get(n)
    if kind == "blank_page" and path in ("", "index.html"):
        return Response("<!doctype html><html><head><title></title></head><body></body></html>", media_type="text/html")
    if kind == "forbidden":
        return Response("<html><head><title>Access Denied</title></head><body>Access Denied</body></html>", status_code=403, media_type="text/html")
    if path == "robots.txt":
        text = "User-agent: *\nDisallow: /\n" if kind == "robots_block" else f"User-agent: *\nAllow: /\nSitemap: http://{host}/sitemap.xml\n"
        return PlainTextResponse(text)
    if path == "sitemap.xml":
        urls = "".join(f"<url><loc>http://{host}/page/{i}</loc></url>" for i in range(1, 25))
        return Response(f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', media_type="application/xml")
    if kind == "flaky_503":
        count = _flaky_seen.get(host, 0) + 1
        _flaky_seen[host] = count
        if count <= 2:
            return Response("busy", status_code=503, headers={"Retry-After": "1"})
    await asyncio.sleep(p.delay)
    if path.startswith("asset/"):
        return Response(PIXEL, media_type="image/png")
    if path.startswith("submit/"):
        return Response("thanks", media_type="text/plain")
    if kind == "slow_pages" and path.startswith("page/"):
        await asyncio.sleep(45)            # longer than the scanner's navigation timeout: these pages must time out, the seed must not
    if path in ("privacy-policy", "cookie-policy") and path in p.policies:
        return Response(policy_html(n, path), media_type="text/html", headers={"Set-Cookie": "sid=1; Path=/"})
    if path in ("", "index.html"):
        return Response(page_html(n, host, 0, p), media_type="text/html", headers={"Set-Cookie": "sid=1; Path=/"})
    m = re.fullmatch(r"page/(\d+)", path)
    if m and 1 <= int(m.group(1)) <= p.pages:
        return Response(page_html(n, host, int(m.group(1)), p), media_type="text/html", headers={"Set-Cookie": "sid=1; Path=/"})
    return Response("not found", status_code=404, media_type="text/plain")
