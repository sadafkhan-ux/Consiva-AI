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
