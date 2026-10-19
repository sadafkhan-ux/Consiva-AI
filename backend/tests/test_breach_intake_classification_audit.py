"""Audit coverage for Agent 4 intake, classification and lifecycle (2026-10 Lane 1).

Scope: incident creation (tenant scoping, reference/idempotency handling, clear 4xx
validation), the deterministic classifier's honesty about uncertainty, `set_finding`'s
human-and-reason gate, the lifecycle state machine's terminal/reason guarantees, and
the generic `/transition` endpoint's refusal to become a side door around a gated
route.

This file does not re-prove what test_breach_lifecycle.py and
test_breach_classification_risk.py already cover (the transition table's structural
guarantees, the classifier's phrase/keyword/ambiguity behaviour) -- it adds the
service- and route-level checks those unit-level files don't reach, and the one bug
this audit found and fixed: REJECTED and ESCALATED could be reached through the
generic transition endpoint with no stated reason at all, despite lifecycle.py's own
docstring promising both "carry a reason in the audit entry".
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.agents.breach.errors import (
    IncidentNotFoundError,
    IncidentNotReadyError,
    InvalidIncidentError,
)
from app.agents.breach.rules import classification as clf
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import incident_service, lifecycle
from app.api.v1.routes import incidents as routes
from app.core.security import CurrentUser
from tests.test_breach_end_to_end import NOW, ORG, USER, World, _DB, open_incident, wire

_USER = CurrentUser(user_id=str(USER), org_id=str(ORG))


# ── Incident creation: validation produces a clear 4xx, not a 500 ────────────────

@pytest.mark.asyncio
async def test_create_incident_rejects_a_blank_title():
    with pytest.raises(InvalidIncidentError):
        await incident_service.create_incident(
            _DB(), org_id=ORG, title="   ", description="something happened",
            source=vocab.SOURCE_MANUAL, detected_at=NOW, now=NOW,
        )


@pytest.mark.asyncio
async def test_create_incident_rejects_a_blank_description():
    with pytest.raises(InvalidIncidentError):
        await incident_service.create_incident(
            _DB(), org_id=ORG, title="Suspicious login", description="",
            source=vocab.SOURCE_MANUAL, detected_at=NOW, now=NOW,
        )


@pytest.mark.asyncio
async def test_create_incident_rejects_an_unknown_source():
    with pytest.raises(InvalidIncidentError):
        await incident_service.create_incident(
            _DB(), org_id=ORG, title="Suspicious login", description="details here",
            source="carrier_pigeon", detected_at=NOW, now=NOW,
        )


@pytest.mark.asyncio
async def test_create_incident_rejects_an_unknown_initial_severity():
    with pytest.raises(InvalidIncidentError):
        await incident_service.create_incident(
            _DB(), org_id=ORG, title="Suspicious login", description="details here",
            source=vocab.SOURCE_MANUAL, detected_at=NOW, initial_severity="apocalyptic",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_create_incident_rejects_a_detection_time_in_the_future():
    """A detection date after now is almost always a timezone mistake, and one that
    stood would put the whole response clock in the wrong place."""
    with pytest.raises(InvalidIncidentError):
        await incident_service.create_incident(
            _DB(), org_id=ORG, title="Suspicious login", description="details here",
            source=vocab.SOURCE_MANUAL, detected_at=NOW + timedelta(hours=1), now=NOW,
        )


@pytest.mark.asyncio
async def test_create_incident_rejects_occurrence_after_detection():
    with pytest.raises(InvalidIncidentError):
        await incident_service.create_incident(
            _DB(), org_id=ORG, title="Suspicious login", description="details here",
            source=vocab.SOURCE_MANUAL, detected_at=NOW,
            occurred_at=NOW + timedelta(hours=2), now=NOW,
        )


@pytest.mark.asyncio
async def test_a_malformed_create_request_is_a_4xx_through_the_route():
    """The route-level model validation must also surface as a clean 4xx-shaped
    pydantic ValueError rather than ever reaching the service with a bad source."""
    with pytest.raises(ValueError):
        routes.IncidentCreate(
            title="Suspicious login", description="details here",
            source="not_a_real_source", detected_at=datetime.now(UTC),
        )


# ── Incident creation: idempotency and reference uniqueness ─────────────────────

@pytest.mark.asyncio
async def test_a_repeated_idempotency_key_returns_the_existing_incident(monkeypatch):
    """A SIEM retrying its webhook must not split one alert's evidence across two
    incidents."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    first, is_new_first = await incident_service.create_incident(
        db, org_id=ORG, title="Suspicious login", description="details here",
        source=vocab.SOURCE_SIEM, detected_at=NOW, idempotency_key="alert-123",
        created_by_user_id=USER, now=NOW,
    )
    assert is_new_first
    second, is_new_second = await incident_service.create_incident(
        db, org_id=ORG, title="A different title entirely", description="ignored",
        source=vocab.SOURCE_SIEM, detected_at=NOW, idempotency_key="alert-123",
        created_by_user_id=USER, now=NOW,
    )
    assert not is_new_second
    assert second.id == first.id
    assert len(world.incidents) == 1


@pytest.mark.asyncio
async def test_reference_generation_retries_past_a_collision(monkeypatch):
    """`new_reference` is random by design (§ so it does not disclose volume); a
    collision must be retried rather than surfacing as a duplicate-key error."""
    from app.db.repositories import incident_repository as repo

    calls = {"n": 0}

    async def reference_exists(db, org_id, candidate):
        calls["n"] += 1
        return calls["n"] <= 2  # first two candidates collide, third is free

    monkeypatch.setattr(repo, "reference_exists", reference_exists)
    reference = await incident_service._unused_reference(_DB(), ORG)
    assert reference.startswith("INC-")
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_reference_generation_gives_up_after_its_attempt_budget(monkeypatch):
    from app.db.repositories import incident_repository as repo

    async def always_collides(db, org_id, candidate):
        return True

    monkeypatch.setattr(repo, "reference_exists", always_collides)
    with pytest.raises(IncidentNotReadyError):
        await incident_service._unused_reference(_DB(), ORG)


# ── Cross-tenant isolation ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_incident_in_another_org_404s_exactly_like_a_missing_one(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    other_org = uuid.uuid4()

    with pytest.raises(IncidentNotFoundError) as leaked:
        await incident_service.get_incident_or_raise(db, case.id, other_org)
    with pytest.raises(IncidentNotFoundError) as missing:
        await incident_service.get_incident_or_raise(db, uuid.uuid4(), other_org)
    # Same error, same shape -- confirming existence to a caller outside the org is
    # itself a disclosure.
    assert type(leaked.value) is type(missing.value)


@pytest.mark.asyncio
async def test_the_route_layer_scopes_every_lookup_to_the_callers_org(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    outsider = CurrentUser(user_id=str(uuid.uuid4()), org_id=str(uuid.uuid4()))
    with pytest.raises(IncidentNotFoundError):
        await routes.get_incident(case.id, user=outsider, db=db)


# ── Classification: never a confident guess ──────────────────────────────────────

@pytest.mark.asyncio
async def test_an_ambiguous_report_is_stored_as_other_at_zero_confidence_not_hidden(monkeypatch):
    """The classifier's `ambiguous` signal must survive into the stored incident as an
    honest, reviewable marker -- not get flattened into something that looks decided."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world,
        title="A laptop was stolen, ransomware encrypted the file server, and our "
              "vendor reported a breach",
        description="Three separate reports came in overnight and nobody has sorted "
                     "out which incident this actually is yet.",
    )
    assert case.incident_type == vocab.TYPE_OTHER
    assert case.classification_confidence == 0.0
    assert case.classification_method == clf.METHOD_DETERMINISTIC


@pytest.mark.asyncio
async def test_empty_incident_text_classifies_as_other_not_a_crash(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    # title must satisfy the route's min_length in real use, but the service itself
    # takes whatever the caller gives it -- whitespace-only text is the case the
    # classifier must not choke on.
    case = await open_incident(db, world, title="   .   ", description="   .   ")
    assert case.incident_type == vocab.TYPE_OTHER
    assert case.classification_confidence == 0.0


@pytest.mark.asyncio
async def test_a_confident_phrase_match_is_recorded_with_its_evidence(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Ransomware encrypted our file server overnight",
        description="Every file on the primary fileserver now has a .locked extension.",
    )
    assert case.incident_type == vocab.TYPE_MALWARE
    assert case.classification_confidence == clf.CONF_PHRASE
    assert case.classification_method == clf.METHOD_DETERMINISTIC


@pytest.mark.asyncio
async def test_classify_route_rejects_an_override_outside_the_vocabulary(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    with pytest.raises(InvalidIncidentError):
        await incident_service.classify_incident(
            db, case, actor_user_id=USER, override_type="sql_injection_attack"
        )
    with pytest.raises(InvalidIncidentError):
        await incident_service.classify_incident(
            db, case, actor_user_id=USER, override_type=vocab.TYPE_UNCLASSIFIED,
        )


@pytest.mark.asyncio
async def test_a_manual_override_is_recorded_at_full_confidence_and_distinguishable(monkeypatch):
    """A human override and a confident rule match must not look alike in the audit
    trail -- `method` is how a reader tells "the rules decided this" from "a person
    decided this"."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world,
        title="A laptop was stolen, ransomware encrypted the file server, and our "
              "vendor reported a breach",
        description="Ambiguous on its face; a reviewer determined it was the vendor "
                     "incident that mattered.",
    )
    assert case.incident_type == vocab.TYPE_OTHER  # ambiguous, pre-override

    await incident_service.classify_incident(
        db, case, actor_user_id=USER, override_type=vocab.TYPE_THIRD_PARTY,
    )
    assert case.incident_type == vocab.TYPE_THIRD_PARTY
    assert case.classification_method == clf.METHOD_MANUAL
    assert case.classification_confidence == 1.0


# ── set_finding: a human, named, with a reason, for CONFIRMED ────────────────────

@pytest.mark.asyncio
async def test_set_finding_rejects_an_unknown_field(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    with pytest.raises(InvalidIncidentError):
        await incident_service.set_finding(
            db, case, field="severity", confidence=vocab.PROBABLE,
            actor_user_id=USER, reason="not a real finding field",
        )


@pytest.mark.asyncio
async def test_set_finding_rejects_an_unknown_confidence(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    with pytest.raises(InvalidIncidentError):
        await incident_service.set_finding(
            db, case, field="breach_confirmed", confidence="pretty_sure",
            actor_user_id=USER, reason="not a real confidence level",
        )


@pytest.mark.asyncio
async def test_set_finding_confirmed_requires_a_reason(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    with pytest.raises(IncidentNotReadyError):
        await incident_service.set_finding(
            db, case, field="breach_confirmed", confidence=vocab.CONFIRMED,
            actor_user_id=USER, reason="   ",
        )


@pytest.mark.asyncio
async def test_set_finding_confirmed_requires_a_named_actor(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    with pytest.raises(IncidentNotReadyError):
        await incident_service.set_finding(
            db, case, field="breach_confirmed", confidence=vocab.CONFIRMED,
            actor_user_id=None, reason="Confirmed via the vendor's own disclosure.",
        )


@pytest.mark.asyncio
async def test_set_finding_non_confirmed_needs_no_reason(monkeypatch):
    """Only CONFIRMED carries the human-and-reason requirement; recording "possible"
    or "unknown" is not an organisation committing to a position."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    await incident_service.set_finding(
        db, case, field="personal_data_involved", confidence=vocab.POSSIBLE,
        actor_user_id=USER, reason="",
    )
    assert case.personal_data_involved == vocab.POSSIBLE


# ── Lifecycle: requires_reason, the fix this audit made ─────────────────────────

def test_requires_reason_covers_exactly_rejected_and_escalated():
    """REJECTED and ESCALATED are the two targets requires_error_code excludes with
    the stated reason that "both carry a reason in the audit entry rather than an
    error code" -- requires_reason is what makes that promise real."""
    assert lifecycle.requires_reason(vocab.REJECTED)
    assert lifecycle.requires_reason(vocab.ESCALATED)
    for status in vocab.ALL_STATUSES - {vocab.REJECTED, vocab.ESCALATED}:
        assert not lifecycle.requires_reason(status), status


@pytest.mark.asyncio
async def test_rejecting_an_incident_with_no_reason_is_refused(monkeypatch):
    """Before this fix: a caller could move an incident straight to REJECTED (a
    terminal state) through the generic transition endpoint with neither an error
    code nor a reason -- a conclusion on the permanent record nobody can account for."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    with pytest.raises(IncidentNotReadyError):
        await incident_service.transition(db, case, vocab.REJECTED, actor_user_id=USER, now=NOW)
    assert case.status == vocab.INVESTIGATING, "the refused transition must not have applied"


@pytest.mark.asyncio
async def test_escalating_an_incident_with_no_reason_is_refused(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    with pytest.raises(IncidentNotReadyError):
        await incident_service.transition(db, case, vocab.ESCALATED, actor_user_id=USER, now=NOW)


@pytest.mark.asyncio
async def test_rejecting_an_incident_with_a_reason_succeeds(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    await incident_service.transition(
        db, case, vocab.REJECTED, actor_user_id=USER, now=NOW,
        error_detail="Traced to the nightly backup job; no external access.",
    )
    assert case.status == vocab.REJECTED
    assert lifecycle.is_terminal(case.status)


@pytest.mark.asyncio
async def test_escalating_with_only_the_detail_dict_reason_also_satisfies_the_guard(monkeypatch):
    """The reason may arrive as `error_detail` (the route's path) or inside `detail`
    (a direct service caller's path); either satisfies the guard."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    await incident_service.transition(
        db, case, vocab.ESCALATED, actor_user_id=USER, now=NOW,
        detail={"reason": "Needs a senior reviewer; potential regulator notification."},
    )
    assert case.status == vocab.ESCALATED


# ── The generic /transition route: the reason guard, reached through HTTP shape ──

@pytest.mark.asyncio
async def test_transition_route_refuses_to_reject_without_a_reason(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    payload = routes.TransitionIn(to_status=vocab.REJECTED)  # no reason supplied
    with pytest.raises(IncidentNotReadyError):
        await routes.transition(case.id, payload, user=_USER, db=db)


@pytest.mark.asyncio
async def test_transition_route_accepts_a_reasoned_rejection(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    payload = routes.TransitionIn(
        to_status=vocab.REJECTED,
        reason="Confirmed as the scheduled nightly backup job; not an incident.",
    )
    result = await routes.transition(case.id, payload, user=_USER, db=db)
    assert result["status"] == vocab.REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked_target", [vocab.APPROVED, vocab.CLOSED, vocab.PARTIALLY_COMPLETED]
)
async def test_the_reason_fix_did_not_loosen_the_dedicated_target_guard(blocked_target):
    """Regression guard for this audit's own change: the new requires_reason check in
    incident_service.transition must not be reachable in a way that substitutes for
    F-3's dedicated-target block -- the generic route still refuses these three
    outright, reason or no reason."""
    payload = routes.TransitionIn(
        to_status=blocked_target, reason="a perfectly good reason, supplied anyway"
    )
    with pytest.raises(IncidentNotReadyError):
        await routes.transition(uuid.uuid4(), payload, user=_USER, db=None)
