"""The /consent-agent integration API.

These cover the layer's own contract -- auth, tenant isolation, idempotency, quota,
SSRF on both the scan target and the webhook, the error envelope, and the derived
status/progress. The pipeline underneath has its own suites and is not re-tested here.

Two bugs found by exercising this live rather than by reading it, both now held by
tests below:

  * `request_scan` enqueues its own "scan" job, so the API's chain job crawled the site
    a SECOND time -- one call produced two crawls, 17 stages and 6 findings for a
    3-finding site.
  * The self-hosted model is configured by filesystem path, and that path went out in
    `token_metrics.model`, publishing the inference host's directory layout and an OS
    username to any API consumer.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.api.v1.routes import consent_agent as route
from app.api.v1.schemas.consent_agent import CreateScanRequest, ScanOptions
from app.services import consent_agent_api_service as api_service
from app.services import webhook_service


class _Stage:
    def __init__(self, stage, status, metadata=None, error=None, duration_ms=0):
        self.stage = stage
        self.status = status
        self.stage_metadata = metadata or {}
        self.error = error
        self.duration_ms = duration_ms


class _Scan:
    def __init__(self, status="completed", error=None):
        self.id = uuid.uuid4()
        self.url = "https://example.test"
        self.status = status
        self.error = error
        self.started_at = datetime.now(UTC)
        self.completed_at = None
        self.scanner_version = "1.0"
        self.scan_options = {}


class _Run:
    def __init__(self, status):
        self.status = status


# ── Derived status: one field from several ──────────────────────────────────────

@pytest.mark.parametrize("scan_status,expected", [
    ("pending", "queued"), ("running", "running"), ("cancelled", "cancelled"),
    ("failed", "failed"),
])
def test_scan_state_maps_to_one_api_status(scan_status, expected):
    assert api_service._api_status(_Scan(scan_status), None, []) == expected


def test_a_failed_analysis_that_still_produced_findings_reads_completed():
    """The rule-derived fallback means a failed llm_analysis is not a failed scan.

    An external validation of this product flagged a screen showing "Failed" beside
    "completed" for exactly this reason. One status is exposed, and it answers the only
    question a poller has: is there a result?
    """
    stages = [_Stage("llm_analysis", "failed"), _Stage("rule_findings_generated", "completed")]
    assert api_service._api_status(_Scan(), _Run("failed"), stages) == "completed"


def test_a_failed_analysis_that_produced_nothing_reads_failed():
    """The distinction has to cut both ways or it is just relabelling failures."""
    stages = [_Stage("llm_analysis", "failed")]
    assert api_service._api_status(_Scan(), _Run("failed"), stages) == "failed"


def test_awaiting_human_review_reads_completed():
    """`paused` is the review gate: every finding is present and waiting on a person,
    which is a finished result to an API consumer."""
    assert api_service._api_status(_Scan(), _Run("paused"), []) == "completed"


# ── Progress comes from stages, never from a clock ──────────────────────────────

def test_progress_is_zero_before_anything_runs():
    assert api_service._progress([], "running") == 0


def test_progress_advances_with_completed_stages():
    early = api_service._progress([_Stage("url_validation", "completed")], "running")
    later = api_service._progress(
        [_Stage("url_validation", "completed"), _Stage("website_scan", "completed")], "running"
    )
    assert 0 < early < later


def test_progress_never_reads_100_while_still_running():
    """100% on a scan that is still working invites a caller to stop polling."""
    stages = [_Stage(name, "completed") for name, _ in api_service._STAGE_WEIGHTS]
    assert api_service._progress(stages, "running") == 99


def test_a_terminal_scan_always_reads_100():
    """A failed scan is not 40% done; it is finished. Leaving the bar mid-way means
    polling something that will never move again."""
    for status in ("completed", "failed", "cancelled"):
        assert api_service._progress([], status) == 100


# ── Nothing internal goes out ───────────────────────────────────────────────────

def test_the_model_filesystem_path_is_not_exposed():
    """Found live: token_metrics.model carried the inference host's directory layout
    and an OS username."""
    leaked = "/home/gignaati/dbeaver/MODELS_LLAMA_CPP/models/qwen3.6-35b-a3b/Qwen3.6-35B-A3B-Q4_K_M.gguf"
    public = api_service._public_model_name(leaked)
    assert public == "Qwen3.6-35B-A3B-Q4_K_M.gguf"
    assert "gignaati" not in public
    assert "/" not in public


def test_a_hosted_model_id_passes_through_unchanged():
    """"openai/gpt-oss-120b" is an identifier, not a path -- truncating it to
    "gpt-oss-120b" is fine, but it must stay recognisable."""
    assert api_service._public_model_name("openai/gpt-oss-120b") == "gpt-oss-120b"
    assert api_service._public_model_name(None) is None


def test_token_metrics_are_absent_rather_than_zero_before_analysis():
    """total_tokens=0 would read as "this scan was free", which is not the same as
    "this scan has not been billed yet"."""
    assert api_service._token_metrics([]) is None
    assert api_service._token_metrics([_Stage("llm_analysis", "running")]) is None


def test_token_metrics_are_summed_from_real_recorded_usage():
    stage = _Stage("llm_analysis", "completed", metadata={
        "provider": "groq", "model": "openai/gpt-oss-120b",
        "llm_usage": {"usage_by_attempt": [
            {"prompt_tokens": 3120, "completion_tokens": 536, "total_tokens": 3656},
        ]},
    })
    metrics = api_service._token_metrics([stage, _Stage("rag_retrieval", "completed")])
    assert metrics["llm_calls"] == 1
    assert metrics["rag_calls"] == 1
    assert metrics["input_tokens"] == 3120
    assert metrics["total_tokens"] == 3656


def test_a_repair_attempt_counts_as_another_llm_call():
    stage = _Stage("llm_analysis", "completed", metadata={"llm_usage": {"usage_by_attempt": [
        {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140},
    ]}})
    metrics = api_service._token_metrics([stage])
    assert metrics["llm_calls"] == 2
    assert metrics["total_tokens"] == 250


def test_a_findings_recommendation_is_returned_when_one_exists():
    """The pipeline stores recommendations in their own table, and this API reported
    `recommendation: null` on every finding while rows sat in consent_recommendations
    for the very scan being returned."""
    class _Row:
        id = uuid.uuid4()
        finding_text = "Trackers fired before consent."
        risk_level = "high"
        category = "marketing"
        evidence = []
        dpdp_reference = []
        requires_human_review = True
        status = "pending"
        created_at = datetime.now(UTC)

    out = api_service._finding_out(_Row(), "Block analytics until consent is given.")
    assert out["recommendation"] == "Block analytics until consent is given."
    assert api_service._finding_out(_Row())["recommendation"] is None


def test_recommendations_are_fetched_in_one_query_not_per_finding():
    """This endpoint is polled, and a scan can carry a dozen findings."""
    import inspect
    body = inspect.getsource(api_service._recommendations_by_finding)
    assert "in_(finding_ids)" in body


# ── The scan list ───────────────────────────────────────────────────────────────

def test_the_api_exposes_a_scan_list():
    """Agent 1 was the ONLY agent in the platform without one -- ROPA, DSR, Incidents
    and Regulatory Watch all have a list endpoint. A caller who lost a scan_id had no
    way to find it again, and a frontend could not show scan history at all."""
    from app.main import app
    spec = app.openapi()
    assert "get" in spec["paths"]["/api/v1/consent-agent/scans"]


def test_the_list_is_summary_rows_not_full_results():
    """Inlining evidence would make one page heavier than every other endpoint
    combined."""
    from app.api.v1.schemas.consent_agent import ScanListItem
    fields = set(ScanListItem.model_fields)
    assert fields == {"scan_id", "website_url", "status", "findings_count",
                      "started_at", "completed_at", "created_at"}
    for heavy in ("cookies", "trackers", "findings", "evidence_counts", "stages"):
        assert heavy not in fields


def test_the_list_pages_and_reports_a_total():
    """Without `total` a caller cannot tell "no more pages" from "no more data"."""
    from app.api.v1.schemas.consent_agent import ScanListResponse
    assert {"scans", "total", "limit", "offset"} == set(ScanListResponse.model_fields)


def test_list_rows_derive_status_the_same_way_the_detail_view_does():
    """A list row and a detail view disagreeing about whether a scan is finished is
    the exact confusion an external validation already flagged on this product."""
    import inspect
    body = inspect.getsource(api_service.list_scans)
    assert "_api_status(" in body


def test_finding_counts_are_one_query_for_the_whole_page():
    """A 50-row page must not become 51 round trips."""
    import inspect
    assert "in_([s.id for s in scans])" in inspect.getsource(api_service.list_scans)


# ── Options are clamped, not obeyed ─────────────────────────────────────────────

def test_max_pages_is_clamped_to_the_server_ceiling():
    """An integrator asking for more than the deployment allows wants a scan, not a
    400 -- but they must not get a 100-page crawl either."""
    body = CreateScanRequest(
        website_url="https://example.test", authorized=True,
        scan_options=ScanOptions(max_pages=100),
    )
    from app.config import get_settings
    assert route._clamp_options(body)["max_pages"] == get_settings().scanner_max_pages


def test_turning_off_consent_state_testing_turns_off_its_sub_options():
    """"don't test consent states, but do test after accept" is contradictory; it is
    resolved here rather than left for the scanner to interpret."""
    body = CreateScanRequest(
        website_url="https://example.test", authorized=True,
        scan_options=ScanOptions(scan_consent_states=False, scan_after_accept=True,
                                 scan_after_reject=True),
    )
    options = route._clamp_options(body)
    assert options["scan_after_accept"] is False
    assert options["scan_after_reject"] is False


def test_the_effective_options_are_what_get_stored():
    """The response says what really ran, not what was asked for."""
    body = CreateScanRequest(website_url="https://example.test", authorized=True)
    options = route._clamp_options(body)
    assert set(options) == {
        "max_pages", "scan_consent_states", "scan_before_consent",
        "scan_after_accept", "scan_after_reject",
    }


# ── Request validation ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["not-a-url", "ftp://x.test", "file:///etc/passwd", "javascript:alert(1)"])
def test_non_http_urls_are_rejected_at_the_schema(url):
    """Shape only. Whether an http URL is SAFE is url_safety's job, not a string
    check -- see the SSRF tests below."""
    with pytest.raises(ValueError):
        CreateScanRequest(website_url=url, authorized=True)


def test_authorization_attestation_defaults_to_false():
    """It must be opted into explicitly; a missing field cannot mean "yes, I own this
    domain"."""
    assert CreateScanRequest(website_url="https://example.test").authorized is False


# ── SSRF, on the webhook as well as the scan target ─────────────────────────────

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://127.0.0.1:8010/internal",             # loopback
    "http://localhost/admin",
    "http://10.0.0.5/",                           # private range
])
async def test_unsafe_webhook_targets_are_refused(url):
    """A webhook URL is exactly as dangerous as a scan URL: a request this server makes
    to an address the caller chose."""
    with pytest.raises(Exception):
        await webhook_service.validate_target(url)


@pytest.mark.parametrize("url", ["ftp://x.test/hook", "file:///etc/passwd", "gopher://x.test"])
async def test_non_http_webhook_schemes_are_refused(url):
    with pytest.raises(ValueError):
        await webhook_service.validate_target(url)


# ── Webhook signing ─────────────────────────────────────────────────────────────

def test_deliveries_are_signed_over_timestamp_and_body():
    """Signing the body alone lets anyone who captures one delivery replay it forever,
    since every byte they need is in the copy they hold."""
    body = b'{"event":"consent_scan.completed"}'
    first = webhook_service.sign(body, "1790000000", "secret")
    later = webhook_service.sign(body, "1790000060", "secret")
    assert first.startswith("v1=")
    assert first != later, "the timestamp is not part of the signed material"


def test_a_different_secret_produces_a_different_signature():
    body = b"{}"
    assert webhook_service.sign(body, "1", "a") != webhook_service.sign(body, "1", "b")


def test_the_payload_carries_no_findings():
    """A webhook is a notification. The result is fetched back over the authenticated
    API, so a mis-typed webhook URL leaks that a scan happened -- not what it found."""
    payload = webhook_service.build_payload(
        scan_id=uuid.uuid4(), status="completed",
        website_url="https://example.test", event="consent_scan.completed",
    )
    assert set(payload) == {"event", "scan_id", "status", "website_url", "result_url", "completed_at"}
    blob = str(payload).lower()
    for leaked in ("finding", "cookie", "tracker", "evidence"):
        assert leaked not in blob


# ── The duplicate-crawl regression ──────────────────────────────────────────────

def test_the_api_does_not_let_request_scan_queue_its_own_crawl():
    """Found on the first live call: request_scan enqueues a "scan" job AND the chain
    job crawls, so one API request produced two crawls, 17 stages and 6 findings for a
    3-finding site. Everything was correct -- just done twice, at double the browser
    time and double the LLM spend."""
    import inspect
    body = inspect.getsource(route.create_scan)
    assert "enqueue_job=False" in body


def test_the_chain_does_not_let_trigger_analysis_queue_its_own_run():
    """The same bug one layer down: trigger_analysis enqueues an "analyze" job, and the
    chain then runs the analysis inline, so it happened twice -- rules_check,
    rag_retrieval, llm_analysis, output_validation and findings_generated each x2, and
    6 findings on a 3-finding site."""
    import inspect
    from app.services import consent_api_chain_service
    assert "enqueue_job=False" in inspect.getsource(consent_api_chain_service.run)


def test_trigger_analysis_still_queues_by_default():
    import inspect
    from app.services import analysis_service
    assert inspect.signature(analysis_service.trigger_analysis).parameters["enqueue_job"].default is True


def test_request_scan_still_queues_its_own_crawl_by_default():
    """The console path must be unaffected -- it has no chain job to do it instead."""
    import inspect
    from app.services import scan_service
    signature = inspect.signature(scan_service.request_scan)
    assert signature.parameters["enqueue_job"].default is True


# ── The chain job is one unit ───────────────────────────────────────────────────

def test_the_chain_notifies_on_failure_as_well_as_success():
    """A crawl that fails must still fire the webhook. Chaining two jobs has nowhere to
    put that -- the scan job is gone by the time anyone knows the run is over."""
    import inspect
    from app.services import consent_api_chain_service
    body = inspect.getsource(consent_api_chain_service.run)
    assert body.count("_notify(") >= 2


def test_the_chain_re_raises_so_the_queue_still_sees_the_failure():
    import inspect
    from app.services import consent_api_chain_service
    assert "raise" in inspect.getsource(consent_api_chain_service.run)


def test_cancellation_is_checked_between_phases():
    """queue.cancel_jobs_for_scan cannot stop a job already running, so this is what
    makes a mid-run cancel actually take effect."""
    import inspect
    from app.services import consent_api_chain_service
    assert inspect.getsource(consent_api_chain_service.run).count("_is_cancelled") >= 2


# ── Error envelope ──────────────────────────────────────────────────────────────

def test_every_error_code_maps_to_a_real_exception_class():
    """A code table naming an exception that no longer exists silently degrades every
    one of those errors to INTERNAL_ERROR."""
    import app.core.exceptions as exceptions
    from app.main import _API_ERROR_CODES
    # Collected from BOTH modules rather than a hand-maintained extra: the first
    # version listed ScanConflictError by hand and immediately went stale when
    # WebhookNotConfiguredError was added on the same router. A guard that has to be
    # edited alongside the thing it guards does not guard it.
    known = {name for name in dir(exceptions) if name.endswith("Error")}
    known |= {name for name in dir(route) if name.endswith("Error")}
    unknown = set(_API_ERROR_CODES) - known
    assert not unknown, f"error-code table names classes that do not exist: {sorted(unknown)}"


def test_the_envelope_only_applies_to_the_integration_prefix():
    """The console reads {"detail": ...} and would break if that changed globally."""
    import inspect
    from app.main import consiva_error_handler
    assert '"/api/v1/consent-agent"' in inspect.getsource(consiva_error_handler)
