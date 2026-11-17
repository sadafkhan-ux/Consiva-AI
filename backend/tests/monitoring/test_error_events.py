"""Error events: classification of HTTP statuses and exceptions, bot-block detection, scrubbing, deduplication keys and the email's content.

What needs a real database and a mail server (recording, cooldowns, recovery, the SMTP exchange) is checked by backend/tools/error_monitor_integration.py inside the Docker network.
"""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app.monitoring import blocks, classify, events
from app.monitoring.config import MonitoringSettings

# ---- HTTP statuses are not all the same failure --------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status,category,emails", [
    (400, "bad_request", False), (404, "not_found", False), (409, "conflict", False), (422, "validation_error", False),
    (408, "request_timeout", False), (429, "rate_limited", False),
    (500, "internal_error", True), (502, "bad_gateway", True), (503, "service_unavailable", True), (504, "gateway_timeout", True),
])
def test_each_status_has_its_own_category_and_only_server_failures_email(status, category, emails):
    c = classify.classify_status(status, service="policy_kb", upstream=False)
    assert c.category == category
    assert classify.should_email(c, status=status) is emails


def test_the_same_401_means_different_things_from_our_api_and_from_a_provider():
    ours = classify.classify_status(401, upstream=False)
    theirs = classify.classify_status(401, service="claude_api", upstream=True)
    assert ours.category == "unauthorized" and not classify.should_email(ours) and ours.severity == classify.INFO
    assert theirs.category == "api_key_invalid" and classify.should_email(theirs) and not theirs.temporary and "claude_api" in theirs.steps
    assert classify.classify_status(403, service="x", upstream=True).severity == classify.CRITICAL and classify.classify_status(403, upstream=False).severity == classify.INFO


def test_a_status_the_operator_lists_can_be_emailed_too():
    c = classify.classify_status(429, service="embedding_api", upstream=True)
    assert not classify.should_email(c, status=429) and classify.should_email(c, extra_statuses=(429,), status=429)


def test_every_category_says_what_to_do():
    for s in (400, 401, 403, 404, 408, 409, 422, 429, 500, 502, 503, 504, 599):
        assert classify.classify_status(s, service="svc", upstream=True).steps.strip(), s


# ---- exceptions ----------------------------------------------------------------------------------------------------------------------------------------------------------

class Boom(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status_code = status


@pytest.mark.parametrize("exc,service,category,group", [
    (Boom("Error code: 401 - invalid x-api-key"), "claude_api", "api_key_invalid", classify.CONFIG),
    (Boom("authentication_error: invalid api key", 401), "embedding_api", "api_key_invalid", classify.CONFIG),
    (Boom("rate limit", 429), "claude_api", "rate_limited", classify.LLM),
    (Boom("bad gateway", 502), "policy_kb", "bad_gateway", classify.RAG),
    (httpx.ConnectError("All connection attempts failed"), "embedding_api", "connection_failed", classify.EMBEDDING),
    (httpx.ReadTimeout("timed out"), "claude_api", "timeout", classify.LLM),
    (asyncio.TimeoutError(), "postgres", "timeout", classify.DATABASE),
    (Boom("could not connect to server: Connection refused"), "postgres", "database_unavailable", classify.DATABASE),
    (Boom("operator does not exist: vector <=> text"), "policy_kb", "vector_db_error", classify.DATABASE),
    (Boom("different vector dimensions 1024 and 768"), "policy_kb", "vector_db_error", classify.DATABASE),
    (Boom("ANTHROPIC_API_KEY is not set"), "claude_api", "config_missing", classify.CONFIG),
    (Boom("Target closed: browser has been closed"), "crawler", "browser_failure", classify.CRAWLER),
    (Boom("could not extract text from the encrypted PDF"), "documents", "extraction_failure", classify.CRAWLER),
])
def test_exceptions_are_classified_by_what_went_wrong_not_by_their_class(exc, service, category, group):
    c = classify.classify_exception(exc, service=service)
    assert (c.category, c.grouping) == (category, group), c


def test_an_embedding_failure_and_a_knowledge_base_failure_are_named_by_the_module():
    assert classify.classify_exception(RuntimeError("EmbeddingFailed: provider unavailable"), module="enterprise.policykb.sync").category == "embedding_failure"
    assert classify.classify_exception(RuntimeError("something odd"), module="enterprise.policykb.retrieve", service="policy_kb").category == "rag_failure"


def test_an_unknown_exception_is_unexpected_and_critical():
    c = classify.classify_exception(RuntimeError("???"))
    assert c.category == "unexpected_error" and c.severity == classify.CRITICAL


# ---- the website blocked the scanner ---------------------------------------------------------------------------------------------------------------------------------------

CLOUDFLARE = ("Just a moment...", "Checking your browser before accessing example.com. Enable JavaScript and cookies to continue. Ray ID: 8a1b2c3d")
AKAMAI = ("Access Denied", "You don't have permission to access \"http://shop.example.com/\" on this server. Reference #18.2d3f1b02.1700000000.1a2b3c4d")
CAPTCHA = ("Verify you are human", "Please complete the CAPTCHA below to continue. g-recaptcha")


def test_a_cloudflare_challenge_is_a_bot_block_even_with_a_normal_status():
    v = blocks.detect_block(200, {"server": "cloudflare", "cf-ray": "x"}, *CLOUDFLARE)
    assert v and v.kind == blocks.BOT_BLOCKED and v.vendor == "cloudflare"
    assert blocks.detect_block(403, {"cf-mitigated": "challenge"}, "", "").kind == blocks.BOT_BLOCKED


def test_a_waf_access_denied_page_and_a_captcha_page_are_bot_blocks():
    assert blocks.detect_block(403, {"server": "AkamaiGHost"}, *AKAMAI).vendor == "akamai"
    assert blocks.detect_block(200, {}, *CAPTCHA).kind == blocks.BOT_BLOCKED
    assert blocks.detect_block(200, {"x-datadome": "protected"}, "Home", "welcome").vendor == "datadome"


@pytest.mark.parametrize("status", [401, 403, 429, 451])
def test_a_refusing_status_without_a_page_is_an_access_block(status):
    v = blocks.detect_block(status, {}, "", "")
    assert v and v.kind == blocks.ACCESS_BLOCKED and str(status) in v.signal


@pytest.mark.parametrize("status", [500, 502, 503, 504, 404, 408])
def test_server_errors_and_missing_pages_are_not_blocks(status):
    assert not blocks.detect_block(status, {"server": "nginx"}, "Error", "The server had a problem.")


def test_a_normal_page_with_a_captcha_form_is_not_a_block():
    page = "Contact us. " + "We are a company that builds software for customers around the world. " * 120 + " <div class='g-recaptcha'></div> I'm not a robot"
    assert len(page) > blocks.SHORT_PAGE_CHARS
    assert not blocks.detect_block(200, {"server": "cloudflare"}, "Contact | Acme", page)
    assert not blocks.detect_block(200, {}, "Privacy Policy", "We collect names. " * 400)


def test_a_cloudflare_503_challenge_is_a_block_but_a_plain_503_is_not():
    assert blocks.detect_block(503, {"server": "cloudflare"}, "Just a moment...", "Checking your browser").kind == blocks.BOT_BLOCKED
    assert not blocks.detect_block(503, {"server": "cloudflare"}, "Service Unavailable", "Temporarily unavailable. Please try again later.")


def test_a_stored_failure_message_is_recognised_as_a_block():
    five = "Could not reach the site at all: https://x.test answered with HTTP 403 instead of a page. The site is refusing the scanner (access denied), which is how the bot protection of some sites turns automated visitors away."
    assert blocks.is_block_message(five) and blocks.is_block_message("BOT_BLOCKED: Website blocked our scanner.")
    assert blocks.detect_block_from_error("https://x.test closed the connection to the scanner on every one of 3 attempts").kind == blocks.BOT_BLOCKED
    for other in ("The domain name of https://x.test did not resolve (DNS)", "HTTP 503 kept answering", "no page could be scanned", "timed out after 30000 ms", "certificate has expired"):
        assert not blocks.is_block_message(other), other


def test_the_block_messages_say_what_happened_and_that_nothing_was_bypassed():
    assert blocks.USER_MESSAGE == "Website blocked our scanner."
    assert "does not try to get round" in blocks.EXPLANATION and "No compliance score" in blocks.EXPLANATION


# ---- nothing secret is stored or sent --------------------------------------------------------------------------------------------------------------------------------------

def test_urls_lose_their_query_string_and_credentials():
    assert events.clean_url("https://user:pass@example.com:8443/privacy?token=abc123&x=1#frag") == "https://example.com:8443/privacy"
    assert events.clean_url(None) is None


def test_the_fingerprint_separates_websites_for_blocks_but_not_scans_for_other_errors():
    a = events.fingerprint("enterprise", "crawl", "website", "bot_blocked", None, "https://a.example/x")
    b = events.fingerprint("enterprise", "crawl", "website", "bot_blocked", None, "https://b.example/x")
    assert a != b and a == events.fingerprint("enterprise", "crawl", "website", "bot_blocked", None, "https://a.example/other")
    assert events.fingerprint("five_page", "m", "s", "bad_gateway", 502) == events.fingerprint("five_page", "m", "s", "bad_gateway", 502)
    assert events.fingerprint("five_page", "m", "s", "bad_gateway", 502) != events.fingerprint("enterprise", "m", "s", "bad_gateway", 502)


def test_error_ids_are_readable_and_unique():
    ids = {events.error_id() for _ in range(200)}
    assert len(ids) == 200 and all(i.startswith("ERR-") and len(i) == 19 for i in ids)


# ---- the email --------------------------------------------------------------------------------------------------------------------------------------------------------------

def _ev(**o):
    now = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)
    ev = {"error_id": "ERR-20261116-ABC123", "agent": "enterprise", "module": "GET /api/v1/enterprise/policy-kb/metrics", "service": "policy_kb", "category": "bad_gateway", "severity": "critical",
          "http_status": 502, "message": "HTTP 502 from GET /api/v1/enterprise/policy-kb/metrics", "website_url": None, "scan_id": None, "request_id": "req123", "first_seen_at": now, "last_seen_at": now,
          "occurrences": 3, "temporary": True, "suggested_steps": "Check that the backend container is running.", "title": "Bad gateway", "resolved_at": None}
    ev.update(o)
    return ev


def _settings():
    return MonitoringSettings(email_to="sadaf.khan@swaransoft.com", smtp_host="h", from_address="alerts@swaransoft.com", dashboard_url="http://localhost:8100/#/system")


def test_the_email_carries_every_field_the_administrator_needs():
    subject, body = events.compose(events.NEW, _ev(), _settings())
    assert subject == "[Consiva] CRITICAL: Bad gateway (Enterprise agent)"
    for fragment in ("Application / agent: Enterprise agent", "Affected module: GET /api/v1/enterprise/policy-kb/metrics", "API / service: policy_kb", "Error type: bad_gateway", "HTTP status: 502",
                     "Error message: HTTP 502", "Request ID: req123", "Error ID: ERR-20261116-ABC123", "16 Nov 2026 12:00:00 UTC", "Temporary: yes, it may clear by itself", "What to do: Check that the backend"):
        assert fragment in body, fragment


def test_an_error_that_needs_an_administrator_says_so():
    _, body = events.compose(events.NEW, _ev(category="api_key_invalid", temporary=False, service="claude_api", http_status=401), _settings())
    assert "Temporary: NO, an administrator has to act" in body


def test_a_website_block_email_says_the_website_blocked_the_scanner_and_names_the_site():
    ev = _ev(category="bot_blocked", service="website", http_status=None, website_url="https://shop.example.com/", scan_id="9663089e", severity="warning", temporary=False, title=blocks.USER_MESSAGE,
             message="Website blocked our scanner. a Cloudflare challenge page.")
    subject, body = events.compose(events.NEW, ev, _settings())
    assert body.startswith("Website blocked our scanner.") and "Website URL: https://shop.example.com/" in body and "Scan ID: 9663089e" in body
    assert "does not try to get round" in body and "WARNING" in subject


def test_a_recovery_email_says_what_recovered_and_for_how_long():
    later = datetime(2026, 11, 16, 12, 30, 0, tzinfo=timezone.utc)
    subject, body = events.compose(events.RECOVERED, _ev(resolved_at=later), _settings())
    assert subject.startswith("[Consiva] RECOVERED") and "has recovered" in body and "Recovered: 16 Nov 2026 12:30:00 UTC" in body and "Times it occurred: 3" in body


def test_no_email_contains_a_secret_even_if_the_message_did(monkeypatch):
    from app.monitoring.scrub import scrub
    leaked = scrub("failed with Authorization: Bearer abcdefghijklmnop and x-api-key=sk-ant-abcdef1234567890 and password=hunter2 at https://u:pw@host/", 500)
    for secret in ("abcdefghijklmnop", "sk-ant-abcdef1234567890", "hunter2", ":pw@"):
        assert secret not in leaked
    _, body = events.compose(events.NEW, _ev(message=leaked), _settings())
    assert "hunter2" not in body and "sk-ant-abcdef" not in body


def test_the_embedding_check_asks_for_the_stored_vector_size_like_the_application_does(monkeypatch):
    """Without `dimensions` the 3.5 model answers at its own default size (2048) and the check reported a mismatch that did not exist."""
    from app.monitoring import checks
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        body = _json.loads(request.content)
        seen.update(body)
        size = body.get("dimensions", 2048)
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * size}]})

    class S:
        nvidia_api_key = "k"
        nvidia_api_base_url = "https://example.test/v1"
        nvidia_embed_model = "nvidia/nemotron-3.5-embed-1b"
        nvidia_embed_dimensions = 1024

    monkeypatch.setattr(checks, "get_settings", lambda: S())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await checks.check_embedding_api(c)

    result = asyncio.run(run())
    assert seen.get("dimensions") == 1024
    assert result.ok, result.detail
