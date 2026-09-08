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
