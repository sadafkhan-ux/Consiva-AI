from app.rules.tracker_catalog import match_cmp, match_vendor


def test_match_vendor_by_domain():
    sig = match_vendor(script_src="https://www.googletagmanager.com/gtm.js?id=X")
    assert sig is not None
    assert sig.vendor == "Google Tag Manager"


def test_match_vendor_by_cookie_prefix():
    sig = match_vendor(cookie_name="_fbp")
    assert sig is not None
    assert sig.vendor == "Meta Pixel"


def test_match_vendor_no_match_returns_none():
    assert match_vendor(script_src="https://mysite.example/app.js") is None


def test_match_cmp_by_domain():
    sig = match_cmp(script_src="https://cdn.cookielaw.org/consent.js")
    assert sig is not None
    assert sig.cmp_vendor == "OneTrust"


def test_match_cmp_by_global_var():
    sig = match_cmp(global_js_vars=("Cookiebot",))
    assert sig is not None
    assert sig.cmp_vendor == "Cookiebot"


def test_match_cmp_transcend_by_domain():
    """Transcend was added from a real, live scan (klaviyo.com) whose banner renders
    inside an open shadow root -- domain_substrings/global_js_vars confirmed live
    against the real transcend-cdn.com script src and window.transcend global."""
    sig = match_cmp(script_src="https://transcend-cdn.com/cm/f3c1005b/airgap.js")
    assert sig is not None
    assert sig.cmp_vendor == "Transcend"
    assert sig.accept_selector == "#AcceptAllAndClose"
    # Confirmed live: this deployment's Transcend config has no one-click reject-all,
    # only "Accept All" and a "More Choices" preferences modal -- reject_selector
    # staying None here is an honest reflection of that, not an oversight.
    assert sig.reject_selector is None


def test_match_cmp_transcend_by_global_var():
    sig = match_cmp(global_js_vars=("transcend",))
    assert sig is not None
    assert sig.cmp_vendor == "Transcend"


# ── Vendors found unclassified on a live scan ───────────────────────────────────

def test_bing_uet_is_recognised_as_a_marketing_tracker():
    """bat.bing.com is Microsoft Advertising's conversion/remarketing tag -- a different
    product from Microsoft Clarity (clarity.ms), which the catalog already had. It was
    filed "unclassified / low" while firing before any consent interaction, so a real
    pre-consent marketing tracker was left out of the risk count."""
    from app.rules.tracker_catalog import match_vendor

    sig = match_vendor(script_src="https://bat.bing.com/p/action/16005611.js")
    assert sig is not None and sig.default_category == "marketing"


def test_an_asset_cdn_classifies_the_same_with_and_without_a_query_string():
    """Without a vendor match these fall through to the static-asset heuristic, which
    requires no query string -- so one scan classified ik.imagekit.io images two
    different ways at once depending on whether the URL carried ?tr= transform params."""
    from app.rules.consent_rules import classify_tracker
    from app.scanner.schemas import TrackerRecord

    def categorise(src: str) -> str | None:
        return classify_tracker(TrackerRecord(
            local_id="t1", script_src=src, resource_type="image",
            consent_states=["pre_consent"],
        )).category

    plain = categorise("https://ik.imagekit.io/aktvd2sxx/img/9305f8e0.png")
    transformed = categorise("https://ik.imagekit.io/aktvd2sxx/img/PANDA99.png?tr=w-400")
    assert plain == transformed, "the same CDN classified two ways within one scan"


def test_public_cdns_maps_and_bot_checks_are_known_and_functional():
    """Caught unclassified on live scans (poolscapes.in, groupsnp.com): while one request is unclassified, whether a banner is needed cannot be decided."""
    for url, vendor in (
        ("https://cdn.jsdelivr.net/npm/swiper@11/swiper-bundle.min.js", "Public script and style CDN"),
        ("https://unpkg.com/react@18/umd/react.production.min.js", "Public script and style CDN"),
        ("https://code.jquery.com/jquery-3.7.1.min.js", "Public script and style CDN"),
        ("https://maps.googleapis.com/maps/api/js?key=K", "Google Maps"),
        ("https://maps.gstatic.com/maps-api-v3/api/js/66/13d/main.js", "Google Maps"),
        ("https://www.google.com/maps/embed?pb=x", "Google Maps"),
        ("https://www.google.com/recaptcha/api.js?render=K", "reCAPTCHA / hCaptcha"),
        ("https://www.gstatic.com/recaptcha/releases/x/recaptcha__en.js", "reCAPTCHA / hCaptcha"),
    ):
        sig = match_vendor(script_src=url)
        assert sig is not None and sig.vendor == vendor and sig.default_category == "functional", url


def test_the_new_entries_do_not_swallow_analytics_or_advertising():
    for url, vendor in (
        ("https://www.google-analytics.com/g/collect?v=2", "Google Analytics"),
        ("https://www.googletagmanager.com/gtm.js?id=X", "Google Tag Manager"),
        ("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js", "Google Ads"),
        ("https://fonts.googleapis.com/css2?family=Inter", "Google Fonts"),
        ("https://www.google.com/ads/ga-audiences?x=1", "Google Ads"),
    ):
        sig = match_vendor(script_src=url)
        assert sig is not None and sig.vendor == vendor, url


def test_google_ads_beacons_on_google_com_are_marketing_not_unclassified():
    """Seen on 150 stored scans as plain google.com requests filed 'unclassified', which hid them from the rule for advertising before consent."""
    for url in (
        "https://www.google.com/ccm/collect?en=page_view", "https://www.google.com/rmkt/collect/123/456", "https://www.google.co.in/pagead/1p-user-list/123",
        "https://www.google.com/ads/measurement/l?ver=1", "https://www.google.com/measurement/conversion/?x=1", "https://ep1.adtrafficquality.google/getconfig/sodar",
    ):
        sig = match_vendor(script_src=url)
        assert sig is not None and sig.vendor == "Google Ads" and sig.default_category == "marketing", url


def test_other_vendors_seen_unclassified_are_named_with_the_right_category():
    for url, vendor, cat in (
        ("https://c.bing.com/c.gif?x=1", "Bing Ads", "marketing"), ("https://dpm.demdex.net/id?x=1", "Adobe Audience Manager", "marketing"),
        ("https://id5-sync.com/i/1/0.gif", "ID5", "marketing"), ("https://assets.adobedtm.com/launch-x.min.js", "Adobe Launch", "analytics"),
        ("https://sb.scorecardresearch.com/beacon.js", "Comscore (Scorecard Research)", "analytics"), ("https://static.chartbeat.com/js/chartbeat.js", "Chartbeat", "analytics"),
        ("https://assets.calendly.com/assets/external/widget.js", "Calendly Embed", "functional"), ("https://m.stripe.network/inner.html", "Stripe", "functional"),
        ("https://cdn.privacy-mgmt.com/unified/wrapperMessagingWithoutDetection.js", "Sourcepoint (consent management)", "functional"),
    ):
        sig = match_vendor(script_src=url)
        assert sig is not None and sig.vendor == vendor and sig.default_category == cat, url
