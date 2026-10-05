"""The cookie inventory: every cookie's browser attributes, and the page and time it was
first seen in each consent state.

What these hold is the "never guess" rule. A page is named only when the scan has
evidence for it -- a Set-Cookie header the page loaded, or a step that loaded that page
alone. Otherwise the page is left unset and the pages that could have set it are
listed instead, so the report can say "Not available" honestly.
"""

import inspect

from app.db.repositories import scan_repository
from app.scanner.cookie_detector import (
    attribute_new_cookies,
    cookie_key,
    detect_cookies,
    parse_set_cookie_headers,
)
from app.scanner.crawler import _merge_cookie_pass
from app.scanner.schemas import CookieObservation, CookieRecord

_T0 = "2026-10-05T10:00:00+00:00"
_T1 = "2026-10-05T10:00:05+00:00"


# ── Browser attributes ─────────────────────────────────────────────────────────

def test_detect_cookies_keeps_the_browsers_flags():
    [cookie] = detect_cookies(
        [{"name": "_ga", "value": "x", "domain": ".example.com", "path": "/", "expires": 1893456000,
          "secure": True, "httpOnly": False, "sameSite": "Lax"}],
        "www.example.com",
    )
    assert (cookie.secure, cookie.http_only, cookie.same_site) == (True, False, "Lax")
    assert cookie.expiry.startswith("2030-01-01")


def test_missing_flags_stay_unknown_rather_than_defaulting():
    [cookie] = detect_cookies([{"name": "a", "domain": "example.com", "expires": -1}], "example.com")
    assert cookie.secure is None
    assert cookie.http_only is None
    assert cookie.same_site is None
    assert cookie.expiry is None  # -1 is the browser's "session cookie"


# ── Set-Cookie parsing ─────────────────────────────────────────────────────────

def test_set_cookie_uses_the_domain_attribute_when_present():
    found = parse_set_cookie_headers(["sid=abc; Path=/; Domain=.Example.com; Secure"], "https://www.example.com/x")
    assert found == [("sid", "example.com", "/")]


def test_set_cookie_without_domain_is_host_only():
    found = parse_set_cookie_headers(["sid=abc; Path=/"], "https://www.example.com/x")
    assert found == [("sid", "www.example.com", "/")]


def test_set_cookie_without_path_uses_the_default_path_of_the_request():
    # RFC 6265 default-path: the directory of the request path, not "/".
    found = parse_set_cookie_headers(["sid=abc"], "https://example.com/admin/users")
    assert found == [("sid", "example.com", "/admin")]


def test_set_cookie_with_no_more_than_one_slash_defaults_to_root():
    found = parse_set_cookie_headers(["sid=abc"], "https://example.com/users")
    assert found == [("sid", "example.com", "/")]


def test_newline_joined_set_cookie_values_are_split():
    found = parse_set_cookie_headers(["a=1; Path=/\nb=2; Domain=example.com"], "https://example.com/")
    assert found == [("a", "example.com", "/"), ("b", "example.com", "/")]


def test_malformed_set_cookie_lines_are_skipped():
    assert parse_set_cookie_headers(["", "novalue", "=nameless"], "https://example.com/") == []


# ── Cookie identity includes path ───────────────────────────────────────────────

def test_same_name_and_domain_but_different_paths_are_distinct_cookies():
    """`session` at `/` and `session` at `/admin` are two different browser cookies --
    identity must not collapse them into one just because name and domain match."""
    root = cookie_key("session", "example.com", "/")
    admin = cookie_key("session", "example.com", "/admin")
    assert root != admin

    jar = [
        {"name": "session", "domain": "example.com", "path": "/"},
        {"name": "session", "domain": "example.com", "path": "/admin"},
    ]
    result = attribute_new_cookies(jar, set(), ["https://example.com/"], [], _T1, "pre_consent")
    assert set(result) == {root, admin}


def test_merge_cookie_pass_keeps_same_name_and_domain_different_paths_separate():
    base = [CookieRecord(local_id="cookie-0", name="session", domain="example.com", path="/")]
    new = [CookieRecord(local_id="cookie-0", name="session", domain="example.com", path="/admin")]

    merged = _merge_cookie_pass(base, new)

    assert len(merged) == 2
    assert {c.path for c in merged} == {"/", "/admin"}


# ── Page attribution ───────────────────────────────────────────────────────────

def _jar(*names_domains):
    return [{"name": n, "domain": d} for n, d in names_domains]


def test_a_set_cookie_header_names_the_exact_page():
    events = [{"name": "sid", "domain": "example.com", "path": "/", "page_url": "https://example.com/b",
               "request_url": "https://example.com/api", "observed_at": _T0}]
    result = attribute_new_cookies(
        _jar(("sid", ".example.com")), set(), ["https://example.com/a", "https://example.com/b"],
        events, _T1, "pre_consent",
    )
    obs = result[("sid", "example.com", "/")]
    assert obs.method == "set_cookie_header"
    assert obs.page_url == "https://example.com/b"
    assert obs.source_request_url == "https://example.com/api"
    assert obs.observed_at == _T0


def test_the_earliest_header_wins_when_several_pages_set_it():
    events = [
        {"name": "sid", "domain": "example.com", "path": "/", "page_url": "https://example.com/late",
         "request_url": "r2", "observed_at": _T1},
        {"name": "sid", "domain": "example.com", "path": "/", "page_url": "https://example.com/early",
         "request_url": "r1", "observed_at": _T0},
    ]
    result = attribute_new_cookies(_jar(("sid", "example.com")), set(), ["a", "b"], events, _T1, "pre_consent")
    assert result[("sid", "example.com", "/")].page_url == "https://example.com/early"


def test_a_script_cookie_after_a_single_page_load_names_that_page():
    result = attribute_new_cookies(
        _jar(("_ga", ".example.com")), set(), ["https://example.com/"], [], _T1, "post_accept",
    )
    obs = result[("_ga", "example.com", "/")]
    assert obs.method == "single_page_load"
    assert obs.page_url == "https://example.com/"
    assert obs.observed_at == _T1
    assert obs.consent_state == "post_accept"


def test_a_script_cookie_after_concurrent_pages_is_not_guessed():
    pages = ["https://example.com/a", "https://example.com/b"]
    result = attribute_new_cookies(_jar(("_ga", "example.com")), set(), pages, [], _T1, "pre_consent")
    obs = result[("_ga", "example.com", "/")]
    assert obs.method == "concurrent_batch"
    assert obs.page_url is None
    assert obs.candidate_page_urls == pages


def test_cookies_seen_in_an_earlier_batch_are_not_re_attributed():
    result = attribute_new_cookies(
        _jar(("_ga", "example.com")), {("_ga", "example.com", "/")}, ["https://example.com/later"], [], _T1,
        "pre_consent",
    )
    assert result == {}


def test_a_header_for_another_domain_does_not_attribute():
    events = [{"name": "sid", "domain": "other.com", "path": "/", "page_url": "p", "request_url": "r",
               "observed_at": _T0}]
    result = attribute_new_cookies(_jar(("sid", "example.com")), set(), ["a", "b"], events, _T1, "pre_consent")
    assert result[("sid", "example.com", "/")].method == "concurrent_batch"


# ── Across consent states ──────────────────────────────────────────────────────

def test_merging_passes_keeps_every_states_observation():
    pre = CookieObservation(consent_state="pre_consent", page_url="https://example.com/a",
                            observed_at=_T0, method="single_page_load")
    rej = CookieObservation(consent_state="post_reject", page_url="https://example.com/",
                            observed_at=_T1, method="single_page_load")
    base = [CookieRecord(local_id="cookie-0", name="_ga", domain="example.com",
                         consent_states=["pre_consent"], observations=[pre])]
    new = [CookieRecord(local_id="cookie-0", name="_ga", domain="example.com",
                        consent_states=["post_reject"], observations=[rej])]

    [merged] = _merge_cookie_pass(base, new)

    assert merged.consent_states == ["pre_consent", "post_reject"]
    assert [o.consent_state for o in merged.observations] == ["pre_consent", "post_reject"]


# ── The agent's evidence is unchanged ──────────────────────────────────────────

class _Row:
    id = "00000000-0000-0000-0000-000000000001"
    name = "_ga"
    domain = "example.com"
    category = "analytics"
    vendor = "Google Analytics"
    is_first_party = True
    source = "rule"
    consent_states = ["pre_consent"]
    path = "/"
    expiry = None
    secure = True
    http_only = False
    same_site = "Lax"
    observations = []


def test_the_agents_evidence_dict_does_not_gain_the_audit_fields():
    """prompts.py's cookie compactor reads `expiry`; adding it to the agent's evidence
    would change what the model is shown. Only the evidence API asks for the detail."""
    plain = scan_repository._serialize_evidence([], [], [_Row()], [], [], [], [])["cookies"][0]
    assert set(plain) == {"id", "name", "domain", "category", "vendor", "is_first_party", "source", "consent_states"}

    detailed = scan_repository._serialize_evidence(
        [], [], [_Row()], [], [], [], [], include_cookie_detail=True,
    )["cookies"][0]
    assert {"path", "expiry", "secure", "http_only", "same_site", "observations"} <= set(detailed)


def test_only_the_evidence_route_asks_for_the_detail():
    from app.api.v1.routes import consent_scans
    body = inspect.getsource(consent_scans.get_scan_evidence)
    assert "include_cookie_detail=True" in body
