"""The human decision, the work that follows, and what the API will not disclose.

Agent 5 produces nothing binding on its own, and this file is where that is held
down: every decision needs a named user, the ones that shut something down need a
reason, an action is attested rather than executed, and a finding cannot be closed
over work that is still open.

The last section covers the two disclosure rules -- no secret value ever leaves the
API, and no surface may present a source as current when its collection failed.
"""

import inspect

import pytest

from app.agents.regwatch.errors import ApprovalRequiredError, WatchNotReadyError
from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import lifecycle, review_service, source_service
from app.api.v1.routes import regwatch as routes

# ── A decision is made by a person, with a reason ───────────────────────────────

@pytest.mark.asyncio
async def test_a_decision_without_a_reviewer_is_refused():
    with pytest.raises(ApprovalRequiredError):
        await review_service.decide(
            None, _finding(), reviewer_user_id=None, decision=watch.DECISION_APPROVE
        )


@pytest.mark.parametrize("decision", sorted(watch.DECISIONS_REQUIRING_REASON))
def test_every_closing_decision_requires_a_reason(decision):
    """These decisions become part of a compliance record somebody may have to
    explain years later. "ok" is not an explanation, hence the length floor."""
    with pytest.raises(WatchNotReadyError):
        review_service._require_reason(decision, "no")
    with pytest.raises(WatchNotReadyError):
        review_service._require_reason(decision, None)
    assert review_service._require_reason(decision, "Out of scope: we do not process "
                                                    "children's data.")


def test_approving_does_not_require_prose():
    """Forcing a reason here produces a thousand rows saying "ok", which is worse than
    no field at all."""
    assert review_service._require_reason(watch.DECISION_APPROVE, None) is None


def test_a_finding_is_decided_only_from_review_required():
    from app.agents.regwatch.errors import WatchNotReadyError as _NotReady

    for status in sorted(watch.ALL_STATUSES - {watch.REVIEW_REQUIRED}):
        with pytest.raises(_NotReady):
            lifecycle.assert_reviewable(status)
    lifecycle.assert_reviewable(watch.REVIEW_REQUIRED)


def test_dismissed_is_terminal():
    """"This does not apply to us", with a name and a date, is a position the
    organisation took. Re-opening it would erase that."""
    assert watch.DISMISSED in watch.TERMINAL_STATUSES
    assert lifecycle.allowed_transitions(watch.DISMISSED) == frozenset()


def test_a_later_change_never_supersedes_a_decided_finding():
    assert not lifecycle.can_be_superseded(watch.APPROVED)
    assert not lifecycle.can_be_superseded(watch.DISMISSED)
    assert not lifecycle.can_be_superseded(watch.CLOSED)
    assert lifecycle.can_be_superseded(watch.REVIEW_REQUIRED)


# ── What a reviewer may edit ────────────────────────────────────────────────────

def test_a_reviewer_edits_the_agents_words_not_the_record_of_what_was_observed():
    editable = review_service.EDITABLE_FIELDS
    assert "summary" in editable
    assert "priority" in editable
    # The observation itself, and everything that traces it, is not editable.
    for protected in ("reference", "citations", "grounded_facts", "change_id",
                      "created_at", "status", "relevance_confidence"):
        assert protected not in editable


def test_an_unknown_field_is_refused_rather_than_silently_ignored():
    finding = _finding()
    with pytest.raises(WatchNotReadyError):
        review_service._apply_edits(finding, {"citations": []})
    with pytest.raises(WatchNotReadyError):
        review_service._apply_edits(finding, {"priority": "urgent"})


def test_an_edited_relevance_becomes_the_reviewers_position_not_the_agents():
    """The one place `confirmed` appears in Agent 5 -- and it is a person asserting
    it, which the reason text says explicitly."""
    finding = _finding()
    review_service._apply_edits(finding, {"relevance": watch.NOT_RELEVANT})
    assert finding.relevance == watch.NOT_RELEVANT
    assert finding.relevance_confidence == watch.CONFIRMED
    assert "reviewer's" in finding.relevance_reason


# ── Actions are performed by people ─────────────────────────────────────────────

def test_there_is_no_job_type_that_performs_a_compliance_action():
    """The build forbids a button that pretends to execute. Agent 5 has no
    `regwatch_act` worker path, so there is nothing that could."""
    from app.jobs import worker

    body = inspect.getsource(worker._process_one)
    assert "regwatch_collect" in body
    assert "regwatch_assess" in body
    assert "regwatch_act" not in body


@pytest.mark.asyncio
async def test_actions_cannot_be_raised_before_somebody_approved_the_finding():
    finding = _finding(status=watch.REVIEW_REQUIRED)
    with pytest.raises(WatchNotReadyError):
        await review_service.open_actions(
            None, finding, reviewer_user_id=_uuid(),
            actions=[{"title": "t", "rationale": "r" * 20, "expected_result": "e" * 10}],
        )


@pytest.mark.asyncio
async def test_an_action_without_an_expected_result_is_refused():
    """A task nobody can tell has been done is not a task."""
    finding = _finding(status=watch.APPROVED)
    with pytest.raises(WatchNotReadyError):
        await review_service.open_actions(
            None, finding, reviewer_user_id=_uuid(),
            actions=[{"title": "Review cookie banner", "rationale": "x" * 20}],
        )


@pytest.mark.asyncio
async def test_completing_an_action_requires_who_did_it_and_what_they_did():
    """The platform did not perform this work and cannot verify it, so it records an
    attestation with a name on it rather than a checkbox."""
    from app.db.models import RegWatchAction

    action = RegWatchAction(
        org_id=_uuid(), finding_id=_uuid(), title="t", rationale="r",
        expected_result="e", status=watch.ACTION_OPEN_STATUS,
    )
    with pytest.raises(WatchNotReadyError):
        await review_service.complete_action(
            None, action, actor_user_id=_uuid(), completed_by="", note="done"
        )
    with pytest.raises(WatchNotReadyError):
        await review_service.complete_action(
            None, action, actor_user_id=_uuid(), completed_by="Asha", note="done"
        )


def test_a_completed_action_says_it_was_attested_not_verified():
    body = inspect.getsource(review_service.complete_action)
    assert '"verified_by_platform": False' in body
    api = inspect.getsource(routes._finding_response)
    assert "attested_not_verified" in api


def test_a_finding_cannot_be_closed_over_open_work():
    body = inspect.getsource(review_service.close_finding)
    assert "outstanding" in body
    assert "still open" in body


# ── What the API will not disclose, and will not imply ──────────────────────────

def test_no_route_returns_a_credential_value():
    """The database stores the NAME of an environment variable. Nothing reads the
    value here, and a config carrying credential-shaped keys is refused at
    registration."""
    module = inspect.getsource(routes)
    assert "credential_ref" in module
    assert "os.getenv" not in module
    assert "os.environ" not in module
    body = inspect.getsource(routes._source_response)
    # The ref is the NAME; `requires_credential` is a boolean. Between them a reviewer
    # learns that a source is authenticated and which secret it expects, and nothing
    # more.
    assert '"credential_ref": source.credential_ref' in body
    assert '"requires_credential": bool(source.credential_ref)' in body
    for leak in ("credential_value", "source.config", "token", "password"):
        assert leak not in body


def test_a_pasted_secret_in_the_ref_field_is_rejected():
    """An env-var NAME is uppercase and alphanumeric. Anything else is very likely
    somebody pasting the secret itself into the field."""
    validator = routes.SourceCreate.model_validate
    ok = validator({
        "name": "MeitY notifications", "url": "https://example.gov.in/x",
        "jurisdiction": "India", "credential_ref": "REGWATCH_MEITY_TOKEN",
    })
    assert ok.credential_ref == "REGWATCH_MEITY_TOKEN"
    from pydantic import ValidationError

    for pasted in ("sk-live-abc123", "Bearer eyJhbGciOi", "my secret token", "abc.def"):
        with pytest.raises(ValidationError):
            validator({
                "name": "x", "url": "https://example.gov.in/x",
                "jurisdiction": "India", "credential_ref": pasted,
            })


def test_registration_refuses_a_config_carrying_credential_shaped_keys():
    body = inspect.getsource(source_service.register_source)
    for marker in ("password", "secret", "token", "api_key", "credential", "cookie"):
        assert marker in body


def test_health_travels_with_every_source_the_api_returns():
    """A client that renders a source without its health block would be rendering a
    failing watch as a working one. There is no representation without it."""
    body = inspect.getsource(routes._source_response)
    assert "source_service.health(source)" in body


def test_a_source_that_has_never_been_collected_is_not_current():
    from app.db.models import RegWatchSource

    source = RegWatchSource(
        org_id=_uuid(), name="x", url="https://example.gov.in", jurisdiction="India",
        check_interval_minutes=1440, consecutive_failures=0,
    )
    health = source_service.health(source)
    assert health["is_current"] is False
    assert health["state"] == "never_collected"


def test_a_source_whose_last_attempt_failed_is_not_current():
    from datetime import UTC, datetime, timedelta

    from app.db.models import RegWatchSource

    now = datetime.now(UTC)
    source = RegWatchSource(
        org_id=_uuid(), name="x", url="https://example.gov.in", jurisdiction="India",
        check_interval_minutes=1440, consecutive_failures=2,
        last_success_at=now - timedelta(minutes=30),
        last_checked_at=now,  # attempted since, and it failed
    )
    health = source_service.health(source)
    assert health["is_current"] is False
    assert health["state"] == "failing"
    assert "is unknown" in health["note"]
    assert "not a report that it has not changed" in health["note"]


def test_the_unwatched_count_is_stated_at_the_top_level_of_the_list():
    """A dashboard reading only the rows could show ten green and one red. The count
    of sources NOT being watched is impossible to miss."""
    body = inspect.getsource(routes.list_sources)
    assert "not_currently_watched" in body
    summary = inspect.getsource(routes.summary)
    assert "sources_not_currently_watched" in summary
    assert "coverage_note" in summary


def test_a_failing_source_is_flagged_but_never_auto_disabled():
    """A regulator unreachable for a week is exactly when a compliance team most needs
    to know the watch is not working. Switching it off would be the worst response."""
    body = inspect.getsource(source_service.record_check_outcome)
    assert "consecutive_failures" in body
    assert "enabled = False" not in body
    assert "logger.warning" in body


def test_disabling_a_source_requires_a_reason():
    body = inspect.getsource(source_service.set_enabled)
    assert "requires a reason" in body


# ── helpers ─────────────────────────────────────────────────────────────────────

def _uuid():
    import uuid

    return uuid.uuid4()


def _finding(status: str = watch.REVIEW_REQUIRED):
    from app.db.models import RegWatchFinding

    return RegWatchFinding(
        org_id=_uuid(), change_id=_uuid(), source_id=_uuid(),
        reference="REG-TEST", status=status,
        relevance=watch.UNDETERMINED, relevance_confidence=watch.UNKNOWN,
        relevance_reason="nothing established",
    )


# ── A source that could never be legitimately fetched cannot enter the registry ──

@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8010/health",
    "http://localhost/admin",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
])
@pytest.mark.asyncio
async def test_an_internal_address_cannot_be_registered_as_a_source(target):
    """Found by a live probe: the collector refused to FETCH these, but registration
    accepted them, so the approved-source list could hold an entry that would only
    ever fail with no indication of why."""
    from app.agents.regwatch.errors import InvalidSourceError
    from app.agents.regwatch.services import source_service as svc

    with pytest.raises(InvalidSourceError):
        await svc.register_source(
            None, org_id=_uuid(), name="probe", url=target, jurisdiction="India"
        )


@pytest.mark.asyncio
async def test_the_collector_still_checks_too():
    """Registration-time validation is not a replacement for the fetch-time check: DNS
    can be re-pointed between the two, and redirects are resolved per hop."""
    import inspect

    from app.agents.regwatch.connectors import http_source

    body = inspect.getsource(http_source)
    assert "assert_safe_url" in body


# ── The class of bug live testing caught three times ────────────────────────────
#
# Twice in the API and once in impact mapping, code referenced an attribute or a
# keyword that did not exist: `RopaRecordRow.purpose`, `list_findings(statuses=)`,
# `AuditLog.before_state`. None of it fails at import; all of it fails on the first
# real request. These check the seams directly.

def test_the_serialisers_run_against_real_model_instances():
    """A serialiser naming a column that does not exist raises only when a row
    reaches it. Constructing one here moves that to test time."""
    import uuid as _uuid

    from app.db.models import (
        RegWatchAction,
        RegWatchApproval,
        RegWatchCollection,
        RegWatchFinding,
        RegWatchImpact,
        RegWatchSource,
    )

    org = _uuid.uuid4()
    source = RegWatchSource(
        org_id=org, name="s", url="https://example.gov.in", jurisdiction="India",
        connector="http", check_interval_minutes=1440, consecutive_failures=0,
        credential_ref=None,
    )
    body = routes._source_response(source)
    assert body["health"]["is_current"] is False

    finding = RegWatchFinding(
        org_id=org, change_id=_uuid.uuid4(), source_id=_uuid.uuid4(),
        reference="REG-1", status=watch.DETECTED, relevance=watch.UNDETERMINED,
        relevance_confidence=watch.UNKNOWN, priority_confidence=watch.UNKNOWN,
        citations=[], grounded_facts=[], open_questions=[], requires_human_review=True,
    )
    impact = RegWatchImpact(
        org_id=org, finding_id=finding.id, target_kind=watch.TARGET_CONSENT_WEBSITE,
        target_id=_uuid.uuid4(), target_label="example.com",
        confidence=watch.POSSIBLE, derived_from=watch.DERIVED_RULE,
    )
    action = RegWatchAction(
        org_id=org, finding_id=finding.id, title="t", rationale="r",
        expected_result="e", status=watch.ACTION_OPEN_STATUS,
    )
    approval = RegWatchApproval(
        org_id=org, finding_id=finding.id, reviewer_user_id=_uuid.uuid4(),
        subject="finding", decision=watch.DECISION_APPROVE,
    )
    rendered = routes._finding_response(
        finding, impacts=[impact], actions=[action], approvals=[approval]
    )
    assert rendered["impacts"][0]["confidence"] == watch.POSSIBLE
    assert rendered["actions"][0]["attested_not_verified"] is False

    collection = RegWatchCollection(
        org_id=org, source_id=source.id, status=watch.COLLECTION_FAILED,
        error_code=watch.ERR_SOURCE_UNREACHABLE,
    )
    # The guardrail, on the row: a failed collection can never read as current.
    assert routes._collection_response(collection)["is_current"] is False


def test_every_repository_call_the_routes_make_matches_its_signature():
    """`list_findings(statuses=...)` passed review and every import, then 500'd on the
    first real request."""
    import inspect
    import re

    from app.db.repositories import regwatch_repository

    source = inspect.getsource(routes)
    for call in re.findall(r"repo\.(\w+)\(", source):
        assert hasattr(regwatch_repository, call), (
            f"routes call repo.{call}(), which does not exist"
        )

    # And the keywords, for the calls that use them.
    sig = inspect.signature(regwatch_repository.list_findings)
    assert "status" in sig.parameters
    assert "statuses" not in sig.parameters


def test_the_audit_serialiser_uses_the_columns_audit_logs_actually_has():
    import inspect

    from app.db.models import AuditLog

    columns = {c.name for c in AuditLog.__table__.columns}
    assert {"before", "after", "action", "actor_user_id"} <= columns
    body = inspect.getsource(routes.finding_audit)
    assert "e.before_state" not in body
    assert "e.before" in body and "e.after" in body
