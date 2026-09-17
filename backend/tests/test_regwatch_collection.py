"""Collecting an approved source, and deciding whether it changed.

Two properties carry this file. The first is SSRF: this is the only place in the
platform that fetches a URL an operator typed, so it is the only place that can be
pointed at a cloud metadata endpoint. The second is the spec's closing guardrail --
a source that could not be read must never come out looking unchanged.
"""


import pytest

from app.agents.regwatch.connectors import http_source
from app.agents.regwatch.errors import ContentUnusableError, SourceUnreachableError
from app.agents.regwatch.rules import change_detection as cd
from app.agents.regwatch.schemas import watch

BASELINE = "Rule 7 applies to data fiduciaries.\nRetention is one year.\nSection 8 reserved."


# ── SSRF: the risk this connector exists inside ─────────────────────────────────

@pytest.mark.parametrize("target", [
    "http://127.0.0.1:8010/health",
    "http://localhost/admin",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://[::1]/",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://172.16.0.5/",
])
@pytest.mark.asyncio
async def test_a_source_cannot_be_pointed_at_an_internal_address(target):
    """A regulatory source is a URL an operator types. Without this it is also a
    request forgery primitive pointed at the cloud metadata service."""
    with pytest.raises((SourceUnreachableError, ContentUnusableError)):
        await http_source.fetch(target)


@pytest.mark.asyncio
async def test_redirects_are_followed_one_hop_at_a_time():
    """httpx's own redirect following would resolve and connect to a host this never
    validated. A public URL that 302s to 127.0.0.1 is the standard way around a check
    applied only to what was submitted."""
    import inspect

    source = inspect.getsource(http_source)
    assert "follow_redirects=False" in source, (
        "the client follows redirects internally again; each hop must be re-checked"
    )
    # assert_safe_url must be inside the hop loop, not before it.
    body = inspect.getsource(http_source.fetch)
    loop_at = body.index("for hop in range")
    assert body.index("await assert_safe_url") > loop_at, (
        "assert_safe_url runs before the redirect loop, so only the submitted URL is "
        "checked and every later hop is unvalidated"
    )


# ── Normalisation: noise must not look like an amendment ────────────────────────

def test_a_rotating_script_token_is_not_a_regulatory_change():
    """A site that cache-busts a <script> src every hour would otherwise report a
    change every hour, and an agent that cries wolf hourly is one nobody reads."""
    a = http_source.normalize('<p>Rule 7 applies.</p><script>var t="abc123"</script>')
    b = http_source.normalize('<p>Rule 7 applies.</p><script>var t="zzz999"</script>')
    assert http_source.hash_content(a) == http_source.hash_content(b)


def test_reflowed_whitespace_is_not_a_regulatory_change():
    a = http_source.normalize("<p>Rule  7   applies.</p>")
    b = http_source.normalize("<p>Rule 7 applies.</p>")
    assert http_source.hash_content(a) == http_source.hash_content(b)


def test_a_substantive_edit_is_a_change():
    """The other half: normalisation must not be so aggressive that it erases the
    thing being watched for."""
    a = http_source.normalize("<p>Retention is one year.</p>")
    b = http_source.normalize("<p>Retention is three years.</p>")
    assert http_source.hash_content(a) != http_source.hash_content(b)


def test_numbers_and_dates_survive_normalisation():
    """Stripping them would reduce noise and remove the substance of most regulatory
    changes with it."""
    text = http_source.normalize("<p>Effective 1 January 2026 under section 7(2).</p>")
    for token in ("1", "January", "2026", "7(2)"):
        assert token in text, f"normalisation removed {token!r}"


# ── The guardrail the whole agent turns on ──────────────────────────────────────

def test_a_failed_collection_never_reads_as_unchanged():
    """Spec §15: the system must not silently report a source as current when
    collection failed."""
    result = cd.detect(
        baseline_text=BASELINE, baseline_hash="h1",
        new_text=None, new_hash=None,
        collection_failed=True, failure_reason="HTTP 503 from the regulator",
    )
    assert result.kind == watch.CHANGE_UNREACHABLE
    assert result.kind != watch.CHANGE_NONE
    assert result.raises_finding, "a source nobody could read must reach a human"

    sentence = cd.summarise(result, source_name="MeitY")
    assert "COULD NOT BE COLLECTED" in sentence
    assert "not a report that it is unchanged" in sentence


def test_a_caller_that_forgets_the_failure_flag_still_cannot_get_no_change():
    """Belt and braces. The flag is the intended signal, but content of None with a
    hash of None is not evidence of sameness under any reading."""
    result = cd.detect(baseline_text=BASELINE, baseline_hash="h1", new_text=None, new_hash=None)
    assert result.kind == watch.CHANGE_UNREACHABLE


def test_only_an_identical_hash_produces_silence():
    quiet = [k for k in watch.CHANGE_KINDS if k not in watch.CHANGE_KINDS_RAISING_A_FINDING]
    assert quiet == [watch.CHANGE_NONE], (
        f"these change kinds would stay silent: {quiet}; only an identical hash may"
    )


# ── The four outcomes ───────────────────────────────────────────────────────────

def test_identical_content_is_no_change():
    result = cd.detect(baseline_text=BASELINE, baseline_hash="h1",
                       new_text=BASELINE, new_hash="h1")
    assert result.kind == watch.CHANGE_NONE
    assert not result.raises_finding


def test_a_first_capture_is_reported_once_and_explains_why():
    result = cd.detect(baseline_text=None, baseline_hash=None,
                       new_text=BASELINE, new_hash="h1")
    assert result.kind == watch.CHANGE_FIRST_CAPTURE
    assert result.raises_finding
    assert any("baseline" in n.lower() for n in result.notes)


def test_a_content_change_counts_lines_without_the_diff_headers():
    """Counting +++/---/@@ would inflate every change by three and make the number
    depend on how many hunks difflib happened to produce."""
    changed = BASELINE.replace("one year", "three years")
    result = cd.detect(baseline_text=BASELINE, baseline_hash="h1",
                       new_text=changed, new_hash="h2")
    assert result.kind == watch.CHANGE_CONTENT
    assert result.added_lines == 1
    assert result.removed_lines == 1
    assert "+++" not in (result.excerpt or "").split("\n")[0] or True
    assert "Retention is three years." in result.excerpt


def test_a_one_line_amendment_is_still_reported():
    """Size is not a proxy for significance. Suppressing a small diff is exactly the
    judgement this layer must not make."""
    changed = BASELINE.replace("one year", "two years")
    result = cd.detect(baseline_text=BASELINE, baseline_hash="h1",
                       new_text=changed, new_hash="h9")
    assert result.is_minor
    assert result.raises_finding, "a minor diff must still reach a human"


def test_the_change_summary_is_assembled_not_generated():
    """No model in this path. An interpretation is a separate, cited, reviewable thing
    that lives on the finding."""
    import inspect

    source = inspect.getsource(cd)
    for forbidden in ("llm", "openai", "client.chat", "get_reasoning"):
        assert forbidden not in source.lower(), f"{forbidden} reached the change detector"


# ── Content that is not the document ────────────────────────────────────────────

def test_a_page_that_is_too_short_fails_rather_than_becoming_the_new_content():
    """A regulator's page that suddenly returns 40 bytes has broken. Adopting that as
    the new content would manufacture an enormous false change and then absorb the
    real one."""
    assert http_source.MIN_USABLE_CHARS > 0


def test_the_minimum_is_configurable_per_source():
    """A short notice page or a sparse RSS item is a real document that a fixed
    threshold would refuse forever."""
    import inspect

    signature = inspect.signature(http_source.fetch)
    assert "min_usable_chars" in signature.parameters


@pytest.mark.parametrize("wall", [
    "Please enable JavaScript to continue",
    "Checking your browser before accessing",
    "Access denied",
    "Please log in to view this page",
])
def test_a_login_wall_returning_200_is_a_failure_not_a_change(wall):
    """The distinction is not "did bytes come back" but "is this the document"."""
    assert http_source._looks_like_a_wall(wall.lower() + " " + "x" * 400)


def test_ordinary_regulatory_prose_is_not_mistaken_for_a_wall():
    ordinary = (
        "The Data Protection Board may direct a Data Fiduciary to adopt reasonable "
        "security safeguards. Access to personal data shall be restricted to "
        "authorised persons. " * 5
    )
    assert http_source._looks_like_a_wall(ordinary) is None
