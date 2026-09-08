"""Known vendor signatures for scripts, cookies, and consent-management platforms.

Illustrative starter set, not exhaustive — extend the lists below as new vendors are
observed. Matching is substring-based on purpose, so entries should be specific enough
to avoid false positives (e.g. "google-analytics.com", not "google").

`default_category` is a *starting point* for rules/consent_rules.py's classification —
it is descriptive pattern-matching (which vendor is this?), not the legal judgment
(is this appropriately consented?) that consent_rules.py and the LLM layer produce.
"""

from dataclasses import dataclass
from typing import Literal

Category = Literal["analytics", "marketing", "functional", "other"]


@dataclass(frozen=True)
class VendorSignature:
    vendor: str
    default_category: Category
    domain_substrings: tuple[str, ...] = ()
    cookie_name_prefixes: tuple[str, ...] = ()


TRACKER_CATALOG: tuple[VendorSignature, ...] = (
    VendorSignature(
        vendor="Google Analytics",
        default_category="analytics",
        domain_substrings=("google-analytics.com", "analytics.google.com"),
        cookie_name_prefixes=("_ga", "_gid", "_gat"),
    ),
    VendorSignature(
        vendor="Google Tag Manager",
        default_category="analytics",
        domain_substrings=("googletagmanager.com",),
    ),
    VendorSignature(
        vendor="Google Ads",
        default_category="marketing",
        # /ads/ga-audiences (real live remarketing-audience beacon, e.g.
        # google.co.in/ads/ga-audiences?...) is a distinct real-world signature from the
        # googleadservices.com/googlesyndication.com scripts below -- caught unclassified
        # on a live scan, added on that direct evidence, not guessed.
        domain_substrings=(
            "googleadservices.com", "googlesyndication.com", "doubleclick.net", "/ads/ga-audiences",
        ),
        cookie_name_prefixes=("_gcl", "IDE", "test_cookie"),
    ),
    VendorSignature(
        # Real, live third-party web analytics vendor (getclicky.com / in.getclicky.com
        # / static.getclicky.com) -- found unclassified on a live scan, not a guess.
        vendor="Clicky Analytics",
        default_category="analytics",
        domain_substrings=("getclicky.com",),
    ),
    VendorSignature(
        vendor="Meta Pixel",
        default_category="marketing",
        domain_substrings=("connect.facebook.net", "facebook.com/tr"),
        cookie_name_prefixes=("_fbp", "_fbc"),
    ),
    VendorSignature(
        vendor="LinkedIn Insight Tag",
        default_category="marketing",
        domain_substrings=("snap.licdn.com", "px.ads.linkedin.com"),
        cookie_name_prefixes=("li_",),
    ),
    VendorSignature(
        vendor="Hotjar",
        default_category="analytics",
        domain_substrings=("hotjar.com",),
        cookie_name_prefixes=("_hj",),
    ),
    VendorSignature(
        vendor="Microsoft Clarity",
        default_category="analytics",
        domain_substrings=("clarity.ms",),
        cookie_name_prefixes=("_clck", "_clsk"),
    ),
    VendorSignature(
        vendor="HubSpot",
        default_category="marketing",
        domain_substrings=("js.hs-scripts.com", "js.hsforms.net", "hubspot.com"),
        cookie_name_prefixes=("hubspotutk", "__hstc", "__hssc"),
    ),
    VendorSignature(
        vendor="Intercom",
        default_category="functional",
        domain_substrings=("widget.intercom.io", "intercomcdn.com"),
        cookie_name_prefixes=("intercom-",),
    ),
    VendorSignature(
        vendor="Stripe",
        default_category="functional",
        domain_substrings=("js.stripe.com",),
        cookie_name_prefixes=("__stripe",),
    ),
    VendorSignature(
        vendor="YouTube Embed",
        default_category="functional",
        domain_substrings=("youtube.com/embed", "ytimg.com"),
        cookie_name_prefixes=("YSC", "VISITOR_INFO1_LIVE"),
    ),
    VendorSignature(
        vendor="Cloudflare",
        default_category="functional",
        domain_substrings=("cloudflareinsights.com", "cloudflare.com"),
        cookie_name_prefixes=("__cf",),
    ),
    VendorSignature(
        # A live scan found api.consiva.ai/api/v1/banner/{id}, /api/v1/consent/record,
        # and /sdk.js on the scanned site -- this is a first-party consent-management
        # SDK (banner rendering + consent recording), not a tracker in the adversarial
        # sense: its entire purpose is the consent mechanism itself. "functional" is the
        # right category (it's infrastructure, not analytics/marketing/ad targeting).
        # Flagging it distinctly matters: without this signature it silently fell into
        # the same "unclassified, needs review" bucket as a genuine unknown tracker.
        vendor="Consiva Consent SDK",
        default_category="functional",
        domain_substrings=("consiva.ai",),
    ),
    VendorSignature(
        # "functional" (rendering asset), not "analytics"/"marketing" -- Google Fonts
        # doesn't set cookies or track user behavior for ads/analytics purposes when
        # loaded from Google's CDN in the normal way. Still a real, documented privacy
        # consideration (the request transmits the visitor's IP/UA to Google) which is
        # exactly why it's worth classifying rather than leaving unclassified -- a real
        # live scan found 100% of a test site's detected trackers were Google Fonts CSS/
        # font-file requests with no catalog match, all landing in "unclassified".
        vendor="Google Fonts",
        default_category="functional",
        domain_substrings=("fonts.googleapis.com", "fonts.gstatic.com"),
    ),
)


@dataclass(frozen=True)
class CmpSignature:
    cmp_vendor: str
    domain_substrings: tuple[str, ...] = ()
    global_js_vars: tuple[str, ...] = ()
    # CSS selectors for the banner's own Accept/Reject controls — used by
    # scanner/consent_interactor.py for the three-pass consent-state scan. None where
    # not (yet) catalogued; the generic text-match fallback handles those.
    accept_selector: str | None = None
    reject_selector: str | None = None


CMP_CATALOG: tuple[CmpSignature, ...] = (
    CmpSignature(
        cmp_vendor="OneTrust", domain_substrings=("cdn.cookielaw.org", "onetrust.com"),
        global_js_vars=("OneTrust", "OptanonWrapper"),
        accept_selector="#onetrust-accept-btn-handler",
        reject_selector="#onetrust-reject-all-handler",
    ),
    CmpSignature(
        cmp_vendor="Cookiebot", domain_substrings=("consent.cookiebot.com",), global_js_vars=("Cookiebot",),
        accept_selector="#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll, #CybotCookiebotDialogBodyButtonAccept",
        reject_selector="#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll, #CybotCookiebotDialogBodyButtonDecline",
    ),
    CmpSignature(cmp_vendor="Termly", domain_substrings=("app.termly.io",), global_js_vars=("Termly",)),
    CmpSignature(cmp_vendor="Osano", domain_substrings=("cmp.osano.com",), global_js_vars=("Osano",)),
    CmpSignature(cmp_vendor="Usercentrics", domain_substrings=("app.usercentrics.eu",), global_js_vars=("UC_UI",)),
    CmpSignature(
        # Didomi's documented default widget element ids (developers.didomi.io) --
        # stable across sites using the default (non-custom-styled) widget.
        cmp_vendor="Didomi", domain_substrings=("sdk.privacy-center.org", "didomi.io"), global_js_vars=("Didomi",),
        accept_selector="#didomi-notice-agree-button", reject_selector="#didomi-notice-disagree-button",
    ),
    CmpSignature(
        # CookieYes's documented default banner classes -- stable across the free/pro
        # WordPress-plugin default widget.
        cmp_vendor="CookieYes", domain_substrings=("cdn-cookieyes.com", "cookieyes.com"), global_js_vars=("CookieYes",),
        accept_selector=".cky-btn-accept", reject_selector=".cky-btn-reject",
    ),
    CmpSignature(
        # Complianz (WordPress plugin) documented default banner classes.
        cmp_vendor="Complianz", domain_substrings=("complianz.io",), global_js_vars=("complianz", "cmplz_settings"),
        accept_selector=".cmplz-accept", reject_selector=".cmplz-deny",
    ),
    CmpSignature(
        # Iubenda's documented default consent-solution button classes.
        cmp_vendor="Iubenda", domain_substrings=("iubenda.com", "cdn.iubenda.com"), global_js_vars=("_iub",),
        accept_selector=".iubenda-cs-accept-btn", reject_selector=".iubenda-cs-reject-btn",
    ),
    CmpSignature(
        # Cookie-Script.com's documented default banner element ids.
        cmp_vendor="Cookie-Script", domain_substrings=("cookie-script.com",), global_js_vars=("CookieScript",),
        accept_selector="#cookiescript_accept", reject_selector="#cookiescript_reject",
    ),
    CmpSignature(
        # IAB TCF v2 (Transparency & Consent Framework) generic signature -- covers
        # Quantcast Choice and the many other TCF-compliant vendors that share the
        # __tcfapi global and consensu.org iframe hosting, but whose banner markup
        # varies enough by implementation that a specific accept/reject selector isn't
        # safe to assume here -- left to the generic text-match fallback (now
        # iframe-aware, see consent_interactor.py), which still benefits from the
        # cmp_vendor label this signature provides once __tcfapi is detected.
        cmp_vendor="IAB TCF v2 (generic)", domain_substrings=("consensu.org", "quantcast.mgr.consensu.org"),
        global_js_vars=("__tcfapi",),
    ),
    CmpSignature(
        # TrustArc's older/common banner button id -- TrustArc has run several banner
        # versions over the years, so this specific selector may miss newer ones; the
        # domain/global-var match still labels the vendor correctly either way, and
        # the generic text fallback covers what the selector doesn't.
        cmp_vendor="TrustArc", domain_substrings=("consent.trustarc.com", "trustarc.com"), global_js_vars=("truste",),
        accept_selector="#truste-consent-button",
    ),
    CmpSignature(
        # Transcend Consent Management -- added from a real, live scan (scanner-
        # hardening: real cookie consent banner test, klaviyo.com) that surfaced it:
        # its banner renders inside an open shadow root (#transcend-shadow-root), which
        # is why it was previously missed entirely (BeautifulSoup's static-HTML parse
        # of page.content() cannot see into a shadow root; see the shadow-DOM-aware
        # text collection this same fix adds in crawler.py). Script src and global var
        # confirmed live; reject_selector is deliberately None -- klaviyo.com's
        # Transcend config genuinely has no one-click "reject all" (only "Accept All"
        # and a "More Choices" preferences modal), confirmed by enumerating every
        # element id inside the shadow root, not assumed.
        cmp_vendor="Transcend", domain_substrings=("transcend-cdn.com",), global_js_vars=("transcend",),
        accept_selector="#AcceptAllAndClose",
    ),
)


def match_vendor(*, domain: str | None = None, script_src: str | None = None,
                  cookie_name: str | None = None) -> VendorSignature | None:
    haystack = " ".join(filter(None, [domain, script_src])).lower()
    for sig in TRACKER_CATALOG:
        if haystack and any(sub in haystack for sub in sig.domain_substrings):
            return sig
        if cookie_name and any(cookie_name.startswith(p) for p in sig.cookie_name_prefixes):
            return sig
    return None


def match_cmp(*, script_src: str | None = None, global_js_vars: tuple[str, ...] = ()) -> CmpSignature | None:
    src = (script_src or "").lower()
    for sig in CMP_CATALOG:
        if src and any(sub in src for sub in sig.domain_substrings):
            return sig
        if global_js_vars and any(v in global_js_vars for v in sig.global_js_vars):
            return sig
    return None
