"""The four things that were declared but never wired.

Each was findable only by asking what a person could actually DO with the agent, not
by reading whether the code ran:

  * baseline acceptance existed in the API client and no component called it, so
    every `first_capture` finding was unresolvable and re-reported forever;
  * an action could only go open -> completed, so one that turned out to be
    unnecessary kept its finding open with no way out;
  * `due_at` was stored, serialised and displayed, and nothing ever read it;
  * `manual_upload` and `rss` were registrable connectors that `collect()` ignored --
    a manual source was fetched at an empty URL and failed every sweep.
"""

import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.agents.regwatch.connectors import feed_source
from app.agents.regwatch.errors import (
    InvalidWatchTransitionError,
    SourceNotAuthorizedError,
    WatchNotReadyError,
)
from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import action_sla_service, collection_service, review_service
from app.api.v1.routes import regwatch as routes
from app.db.models import RegWatchAction, RegWatchSource


def _uuid():
    return uuid.uuid4()


def _action(status=watch.ACTION_OPEN_STATUS, due_at=None):
    return RegWatchAction(
        org_id=_uuid(), finding_id=_uuid(), title="t", rationale="r",
        expected_result="e", status=status, due_at=due_at,
    )


# ── 1. Baseline acceptance ──────────────────────────────────────────────────────

def test_accepting_a_baseline_is_one_act_not_two_buttons():
    """Splitting it would let a reviewer move the baseline and leave the finding open,
    which then re-reports the same first capture on every check."""
    body = inspect.getsource(review_service.accept_baseline_from_finding)
    assert "accept_as_baseline" in body
    assert "watch.CLOSED" in body
    assert "record_approval" in body


def test_the_approval_records_that_a_baseline_was_approved_not_a_finding():
    """The subject column exists so a reader can tell "we agreed this matters" from
    "we adopted this as the reference point"."""
    body = inspect.getsource(review_service.accept_baseline_from_finding)
    assert 'subject="baseline"' in body
    assert "baseline" in watch.APPROVAL_SUBJECTS


def test_it_closes_rather_than_dismisses():
    """DISMISSED means "this does not apply to us" -- a judgement nobody made by
    accepting a snapshot as the reference point."""
    body = inspect.getsource(review_service.accept_baseline_from_finding)
    assert "watch.DISMISSED" not in body
    assert "watch.CLOSED" in body


@pytest.mark.asyncio
async def test_a_baseline_cannot_be_accepted_without_a_reviewer():
    from app.agents.regwatch.errors import ApprovalRequiredError
    from app.db.models import RegWatchFinding

    finding = RegWatchFinding(
        org_id=_uuid(), change_id=_uuid(), source_id=_uuid(),
        reference="REG-X", status=watch.REVIEW_REQUIRED,
    )
    with pytest.raises(ApprovalRequiredError):
        await review_service.accept_baseline_from_finding(
            None, finding, None, None, reviewer_user_id=None
        )


def test_the_console_actually_calls_it():
    """The gap that made this phase necessary: the client method existed and no
    component referenced it."""
    from pathlib import Path

    console = Path(__file__).resolve().parents[2] / "frontend/src/components/RegWatchConsole.tsx"
    text = console.read_text(encoding="utf-8")
    assert "acceptBaselineFromFinding" in text
    assert "Accept as baseline" in text
    # And only where it applies -- a first capture is what is waiting on one.
    assert "first_capture" in text


# ── 2. Action lifecycle ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_action_can_be_started_blocked_and_cancelled():
    for target in (watch.ACTION_IN_PROGRESS, watch.ACTION_BLOCKED, watch.ACTION_CANCELLED):
        assert target in review_service._ACTION_TRANSITIONS[watch.ACTION_OPEN_STATUS]


@pytest.mark.asyncio
async def test_completion_is_not_reachable_through_the_status_endpoint():
    """Completion is an attestation, not a status change: it must carry a name and a
    description of the work, so it goes through the endpoint that demands both."""
    with pytest.raises(InvalidWatchTransitionError):
        await review_service.set_action_status(
            None, _action(), status=watch.ACTION_COMPLETED, actor_user_id=_uuid()
        )
    # And the API model refuses it before it reaches the service.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        routes.ActionStatus.model_validate({"status": watch.ACTION_COMPLETED})


@pytest.mark.asyncio
async def test_cancelling_and_blocking_require_a_reason():
    """Work that was dropped or stalled with no record of why is a gap nobody can
    explain later."""
    for status in (watch.ACTION_CANCELLED, watch.ACTION_BLOCKED):
        with pytest.raises(WatchNotReadyError):
            await review_service.set_action_status(
                None, _action(), status=status, actor_user_id=_uuid(), reason="no"
            )


@pytest.mark.asyncio
async def test_a_completed_action_is_terminal():
    assert review_service._ACTION_TRANSITIONS[watch.ACTION_COMPLETED] == frozenset()
    assert review_service._ACTION_TRANSITIONS[watch.ACTION_CANCELLED] == frozenset()
    with pytest.raises(InvalidWatchTransitionError):
        await review_service.set_action_status(
            None, _action(status=watch.ACTION_COMPLETED),
            status=watch.ACTION_IN_PROGRESS, actor_user_id=_uuid(),
        )


def test_cancelling_says_no_work_was_performed():
    """So a cancelled action is never mistaken for a completed one on a later read."""
    body = inspect.getsource(review_service.set_action_status)
    assert '"work_performed": False' in body


def test_a_cancelled_action_no_longer_blocks_closing_its_finding():
    """The reason this mattered: close_finding refuses while anything is outstanding,
    so before cancellation existed a moot action held its finding open forever."""
    body = inspect.getsource(review_service.close_finding)
    assert watch.ACTION_CANCELLED not in body
    assert watch.ACTION_COMPLETED not in body


# ── 3. Due dates that are actually checked ──────────────────────────────────────

def test_the_due_state_is_four_valued_not_a_boolean():
    """"No date was set" and "on track" are different facts. `overdue: false` would
    erase the first, and an action nobody dated would look comfortably on schedule."""
    now = datetime.now(UTC)
    assert action_sla_service.view(_action())["state"] == "no_date"
    assert action_sla_service.view(
        _action(due_at=now - timedelta(days=1)), now=now
    )["state"] == "overdue"
    assert action_sla_service.view(
        _action(due_at=now + timedelta(days=2)), now=now
    )["state"] == "due_soon"
    assert action_sla_service.view(
        _action(due_at=now + timedelta(days=90)), now=now
    )["state"] == "on_track"


def test_a_settled_action_is_not_reported_overdue():
    """Reporting on finished work as though it were outstanding."""
    past = datetime.now(UTC) - timedelta(days=30)
    for status in (watch.ACTION_COMPLETED, watch.ACTION_CANCELLED):
        view = action_sla_service.view(_action(status=status, due_at=past))
        assert view["state"] == "settled"
        assert view["overdue"] is False


@pytest.mark.parametrize("state", ["overdue", "due_soon", "on_track"])
def test_every_dated_state_says_it_is_not_a_statutory_deadline(state):
    """Read without the qualifier, "overdue" in a compliance tool implies a missed
    legal obligation. It measures the organisation's own target."""
    now = datetime.now(UTC)
    offsets = {"overdue": -1, "due_soon": 2, "on_track": 90}
    view = action_sla_service.view(
        _action(due_at=now + timedelta(days=offsets[state])), now=now
    )
    assert view["is_statutory_deadline"] is False
    assert "not a statutory deadline" in view["note"]


def test_the_sweep_marks_each_action_once():
    """It runs on every maintenance tick against an append-only log. Writing on each
    pass would bury every real decision under thousands of identical rows in a day."""
    body = inspect.getsource(action_sla_service.sweep_overdue)
    assert "_already_marked" in body
    assert "continue" in body


def test_the_sweep_does_not_escalate_or_reassign():
    """Marking and reporting is the whole job. Moving other people's work around
    because a date passed would be a decision this platform does not make."""
    body = inspect.getsource(action_sla_service.sweep_overdue)
    for verb in ("owner_label =", "status =", "assign", "escalat"):
        assert verb not in body


def test_the_sweep_rides_the_maintenance_loop():
    from app.jobs import worker

    body = inspect.getsource(worker._maintenance_loop)
    assert "regwatch_action_sla_service.sweep_overdue" in body


def test_the_summary_carries_the_caveat_with_the_number():
    body = inspect.getsource(routes.summary)
    assert "overdue_actions" in body
    assert "overdue_note" in body


# ── 4. Connectors that do what their label says ─────────────────────────────────

@pytest.mark.asyncio
async def test_a_manual_source_is_never_fetched():
    """It has no URL. Before this it was fetched at an empty string, failing every
    sweep and raising a fresh "unreachable" finding each time."""
    body = inspect.getsource(collection_service.collect)
    assert "watch.CONNECTOR_MANUAL" in body
    assert "watch.COLLECTION_SKIPPED" in body
    # And `skipped` is a state nothing can read as current.
    assert watch.COLLECTION_SKIPPED in watch.COLLECTION_NOT_CURRENT


@pytest.mark.asyncio
async def test_content_cannot_be_uploaded_to_an_automatically_fetched_source():
    """It would put unverified text alongside fetched evidence with no way to tell
    them apart."""
    source = RegWatchSource(
        org_id=_uuid(), name="s", url="https://example.gov.in",
        jurisdiction="India", connector=watch.CONNECTOR_HTTP,
    )
    with pytest.raises(SourceNotAuthorizedError):
        await collection_service.accept_manual_content(
            None, source, "text", uploaded_by_user_id=_uuid()
        )


def test_an_upload_is_recorded_as_having_arrived_by_hand():
    body = inspect.getsource(collection_service.accept_manual_content)
    assert '"fetched": False' in body
    assert '"uploaded_by_hand": True' in body


def test_a_feed_becomes_one_line_per_entry():
    """The point of the feed connector: a new advisory is a one-line diff, not a
    reflowed paragraph in which the one fact that matters is invisible."""
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
      <title>Advisories</title><lastBuildDate>Mon, 01 Jan 2029</lastBuildDate>
      <item><title>Advisory A</title><link>https://x/a</link></item>
      <item><title>Advisory B</title><link>https://x/b</link></item>
    </channel></rss>"""
    out = feed_source.normalize(rss)
    assert out is not None
    assert len(out.splitlines()) == 2
    assert "Advisory A" in out and "Advisory B" in out


def test_a_feed_that_only_reorders_itself_is_not_a_change():
    """Reordering is a presentation decision by the publisher; the set of items is
    the substance."""
    def feed(order):
        items = "".join(
            f"<item><title>{t}</title><link>https://x/{t}</link></item>" for t in order
        )
        return f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'

    assert feed_source.normalize(feed(["A", "B", "C"])) == \
        feed_source.normalize(feed(["C", "A", "B"]))


def test_a_feed_that_only_restamps_its_build_date_is_not_a_change():
    """A feed stamping lastBuildDate with "now" would otherwise report a regulatory
    change on every single poll."""
    def feed(stamp):
        return (
            f'<?xml version="1.0"?><rss version="2.0"><channel>'
            f"<lastBuildDate>{stamp}</lastBuildDate>"
            f"<item><title>A</title><link>https://x/a</link></item>"
            f"</channel></rss>"
        )

    assert feed_source.normalize(feed("Mon, 01 Jan 2029")) == \
        feed_source.normalize(feed("Tue, 02 Jan 2029"))


def test_an_unparseable_feed_degrades_rather_than_failing_the_collection():
    """A feed we cannot read is still a document we can diff."""
    assert feed_source.normalize("<html><body>not a feed</body></html>") is None
    assert feed_source.normalize("<?xml version='1.0'?><broken") is None
    body = inspect.getsource(collection_service._normalise_for)
    assert "if not parsed:" in body
    assert "return fetched.text, fetched.content_hash" in body


def test_the_raw_body_reaches_the_connector_that_needs_its_structure():
    """`normalize` strips the very tags a feed parser needs, so the fetcher carries
    the undecorated body alongside the flattened text."""
    from app.agents.regwatch.connectors import http_source

    assert "raw" in {f.name for f in http_source.Fetched.__dataclass_fields__.values()}
    body = inspect.getsource(http_source.fetch)
    assert "raw=decoded" in body


def test_collect_now_refuses_a_manual_source_rather_than_queuing_a_no_op():
    """Found live: it returned 202 and queued a job that would deliberately do
    nothing -- a control reporting success for work it never intended to perform.
    The console hides the button, but the console is not the only caller."""
    body = inspect.getsource(routes.collect_now)
    assert "watch.CONNECTOR_MANUAL" in body
    assert "never fetched" in body
    # And the refusal comes BEFORE anything is enqueued.
    assert body.index("CONNECTOR_MANUAL") < body.index("queue.enqueue")
