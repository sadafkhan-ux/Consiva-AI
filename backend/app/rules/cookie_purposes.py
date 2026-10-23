"""What a well-known cookie is for, in one plain sentence.

The report's cookie inventory gives each cookie's purpose beside its name, provider, party, lifetime and what it did in each consent state.
A purpose is stated ONLY where the cookie is a recognised one, from its provider's own published description, in words that do not claim more
than the provider does. For any other cookie the report says what the scan did establish (its category, if it has one) and that the purpose
could not be identified from the name: never an invented description, and never "unclassified tracker" for something that is simply unknown.

Matching is by cookie NAME (a pattern), because a name is what the scan has. The order matters: the first match wins.
"""

from __future__ import annotations

import re
from datetime import datetime

# (pattern, provider, purpose)
_CATALOGUE: list[tuple[re.Pattern, str, str]] = [(re.compile(p, re.IGNORECASE), prov, why) for p, prov, why in [
    (r"^_ga$", "Google Analytics", "Gives each browser a random identifier so Google Analytics can count visitors and sessions."),
    (r"^_ga_[A-Z0-9]+$", "Google Analytics", "Keeps the state of a visit for a Google Analytics 4 property."),
    (r"^_gid$", "Google Analytics", "Gives each browser a random identifier so Google Analytics can tell visitors apart for 24 hours."),
    (r"^_gat(_.*)?$", "Google Analytics", "Limits how often Google Analytics collects data."),
    (r"^_gcl_(au|aw|dc|gb|gf|ha)$", "Google Ads", "Stores which ad click led to the visit, so conversions can be measured."),
    (r"^(IDE|DSID|test_cookie)$", "Google (DoubleClick)", "Used by Google advertising services to deliver and measure ads."),
    (r"^NID$", "Google", "Stores preferences and, where used, ad personalisation for Google services."),
    (r"^(SOCS|CONSENT)$", "Google", "Remembers the visitor's cookie consent choice for Google services."),
    (r"^_dc_gtm_.*$", "Google Tag Manager", "Limits how often a Google Tag Manager tag fires."),
    (r"^_fbp$", "Meta (Facebook)", "Identifies the browser so the Meta Pixel can measure ads and deliver them."),
    (r"^_fbc$", "Meta (Facebook)", "Stores the ad click identifier from a Facebook ad, for conversion measurement."),
    (r"^fr$", "Meta (Facebook)", "Used by Meta to deliver and measure advertising."),
    (r"^(lastExternalReferrer|lastExternalReferrerTime)$", "Meta (Facebook)", "Remembers where the visitor came from, for ad conversion attribution."),
    (r"^MUID$", "Microsoft", "A browser identifier used across Microsoft sites for advertising and analytics."),
    (r"^MR$", "Microsoft", "Refreshes the MUID browser identifier."),
    (r"^SRM_B$", "Microsoft (Bing)", "A browser identifier used by Microsoft Advertising and Bing."),
    (r"^ANONCHK$", "Microsoft (Clarity / Bing)", "Checks whether the session is anonymous for Microsoft Advertising."),
    (r"^_uet(sid|vid)$", "Microsoft Advertising", "Identifies the visit and the visitor for Microsoft Advertising conversion tracking."),
    (r"^_clck$", "Microsoft Clarity", "Keeps the Clarity visitor identifier and settings for this site."),
    (r"^_clsk$", "Microsoft Clarity", "Joins page views in a visit so Clarity can replay the session."),
    (r"^CLID$", "Microsoft Clarity", "Records the first time Clarity saw this visitor on any site that uses it."),
    (r"^SM$", "Microsoft Clarity", "Keeps Clarity's session state while the browser is open."),
    (r"^__cf_bm$", "Cloudflare", "Tells human visitors from bots, for Cloudflare Bot Management."),
    (r"^_cfuvid$", "Cloudflare", "Lets Cloudflare apply rate limits to a visitor."),
    (r"^cf_clearance$", "Cloudflare", "Remembers that the visitor passed a Cloudflare challenge."),
    (r"^__cflb$|^__cfruid$", "Cloudflare", "Cloudflare load balancing and request routing."),
    (r"^_hjSessionUser_.*$|^_hjFirstSeen$", "Hotjar", "Identifies the visitor so Hotjar can record behaviour on the site."),
    (r"^_hjSession_.*$|^_hjIncludedIn.*$|^_hjAbsoluteSessionInProgress$", "Hotjar", "Keeps the state of a Hotjar recording session."),
    (r"^__hstc$|^hubspotutk$", "HubSpot", "Identifies the visitor for HubSpot tracking and form attribution."),
    (r"^__hssc$|^__hssrc$", "HubSpot", "Tracks sessions for HubSpot analytics."),
    (r"^(li_sugr|bcookie|bscookie|lidc|UserMatchHistory|AnalyticsSyncHistory|li_gc)$", "LinkedIn", "Used by LinkedIn for ad measurement, security and routing."),
    (r"^_pin_unauth$|^_pinterest_.*$", "Pinterest", "Identifies the browser for Pinterest ad measurement."),
    (r"^_ttp$|^_tt_.*$|^ttwid$", "TikTok", "Identifies the browser for TikTok ad measurement."),
    (r"^(YSC|VISITOR_INFO1_LIVE|VISITOR_PRIVACY_METADATA)$", "YouTube", "Used by embedded YouTube videos to estimate bandwidth and keep playback state."),
    (r"^ajs_(user_id|anonymous_id|group_id)$", "Segment", "Stores the visitor identifier used by Segment analytics."),
    (r"^_ym_.*$|^_ym$", "Yandex Metrica", "Identifies the visitor for Yandex Metrica analytics."),
    (r"^mp_.*_mixpanel$", "Mixpanel", "Stores the visitor identifier and properties used by Mixpanel analytics."),
    (r"^amplitude_id.*$|^AMP_.*$", "Amplitude", "Stores the visitor identifier used by Amplitude analytics."),
    (r"^__obref$|^obuid$|^ob_.*$", "Outbrain", "Used by Outbrain to measure content-recommendation ads."),
    (r"^(PHPSESSID|JSESSIONID|ASP\.NET_SessionId|connect\.sid|sessionid|_session_id)$", "The website", "Keeps a visitor's session so the site can respond to the same visit across pages."),
    (r"^(csrftoken|XSRF-TOKEN|_csrf|csrf_token|__RequestVerificationToken)$", "The website", "Protects forms against cross-site request forgery."),
    (r"^wordpress_.*$|^wp-settings.*$|^wordpress_logged_in.*$", "WordPress", "Keeps a WordPress user signed in and remembers their interface settings."),
    (r"^woocommerce_.*$|^wp_woocommerce_session_.*$", "WooCommerce", "Keeps the shopping cart and session for an online store."),
    (r"^_shopify_.*$|^shopify_.*$|^cart(_.*)?$", "Shopify", "Keeps the shopping cart and measures a Shopify store's traffic."),
    (r"^(OptanonConsent|OptanonAlertBoxClosed)$", "OneTrust", "Records the visitor's cookie consent choice."),
    (r"^(CookieConsent|cookieconsent_status|euconsent-v2|cookie_notice_accepted|cookielawinfo-.*)$", "Consent tool", "Records the visitor's cookie consent choice."),
    (r"^(cmp_.*|consiva_.*|__oaiq_consent|oaiq_consent|oaiq_cs.*)$", "Consent tool", "Records the visitor's consent choice and the settings of the consent banner."),
    (r"^(lang|locale|i18n.*|NEXT_LOCALE|language)$", "The website", "Remembers the visitor's language choice."),
]]

_BY_CATEGORY = {
    "necessary": "Needed for the site to work; the exact purpose could not be identified from the cookie's name.",
    "functional": "Supports a function of the site or a service it uses; the exact purpose could not be identified from the cookie's name.",
    "analytics": "Classified as an analytics cookie; the exact purpose could not be identified from the cookie's name.",
    "marketing": "Classified as an advertising or marketing cookie; the exact purpose could not be identified from the cookie's name.",
}
UNKNOWN_PURPOSE = "The purpose could not be identified from the cookie's name."

#: A cookie that lives longer than this after it was set is called long-lived in the report. This is not a DPDP rule: the Act and Rules set no cookie lifetime.
LONG_LIVED_DAYS = 365


def describe_cookie(name: str, category: str | None = None) -> dict:
    """{provider, purpose, purpose_source}: purpose_source is "catalogue" (a recognised cookie), "category" (only its classification is known) or "unknown"."""
    for pattern, provider, purpose in _CATALOGUE:
        if pattern.match(name or ""):
            return {"provider": provider, "purpose": purpose, "purpose_source": "catalogue"}
    if category in _BY_CATEGORY:
        return {"provider": None, "purpose": _BY_CATEGORY[category], "purpose_source": "category"}
    return {"provider": None, "purpose": UNKNOWN_PURPOSE, "purpose_source": "unknown"}


def lifetime_days(expiry: datetime | None, first_seen_iso: str | None) -> float | None:
    """Days from when the cookie was first seen to its expiry, or None for a session cookie or when the sighting time was not recorded."""
    if expiry is None or not first_seen_iso:
        return None
    try:
        seen = datetime.fromisoformat(first_seen_iso)
    except ValueError:
        return None
    if seen.tzinfo is None and expiry.tzinfo is not None:
        seen = seen.replace(tzinfo=expiry.tzinfo)
    return round(max(0.0, (expiry - seen).total_seconds() / 86400), 3)
