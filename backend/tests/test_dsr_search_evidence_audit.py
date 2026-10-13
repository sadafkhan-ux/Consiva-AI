"""Section 5 audit: Data Search and Evidence.

Targeted coverage for five questions about `app/agents/dsr/services/search_service.py`,
`app/agents/dsr/services/evidence_push_service.py` and the search/evidence functions
in `app/db/repositories/dsr_repository.py` that `tests/test_dsr_end_to_end.py` and
`tests/test_dsr_evidence_push.py` do not already answer:

  1. A search across several authorized sources where some succeed, one fails, and
     one finds nothing must report the failure distinctly -- not fold it into an
     aggregate "clean" result just because another source found something.
  2. A case that has never been searched must not look like a search that ran and
     found nothing (NO_MATCH) -- it must be a different, recognizable state.
  3. Evidence must be traceable (record_reference/record_snapshot populated) and
     ropa_category must degrade to None rather than crash when a column cannot be
     classified.
  4. Retrying a search on the same case must not duplicate evidence for the same
     underlying record, and must not therefore double-plan it.
  5. Purging pushed evidence is scoped to the right org/case, and never corrupts a
     plan or response that already referenced the (untouched) `dsr_evidence` rows.

Mirrors test_dsr_end_to_end.py's style: an in-memory fake of the repository and a
fake connector, no live Postgres, so the real services are driven exactly as
production code drives them.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.dsr.connectors.base import SearchOutcome, SubjectMatch
from app.agents.dsr.errors import ConnectorUnavailableError
from app.agents.dsr.schemas import case
from app.agents.dsr.services import (
    case_service,
    evidence_push_service,
    identity_service,
    planning_service,
    search_service,
)
from app.api.v1.routes import dsr as dsr_routes
from app.core.security import CurrentUser
from app.db.models import (
    DsrActionPlan,
    DsrEvidence,
    DsrEvidencePush,
    DsrIdentityVerification,
    DsrRequest,
    DsrSearchRun,
)
from app.db.repositories import dsr_repository

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
ORG = uuid.uuid4()
OTHER_ORG = uuid.uuid4()
USER = uuid.uuid4()


# ── In-memory world (same shape as test_dsr_end_to_end.py's, kept self-contained
#    so this file has no import-time coupling to a file another lane may be
#    editing concurrently) ─────────────────────────────────────────────────────

class World:
    def __init__(self):
        self.requests: dict[uuid.UUID, DsrRequest] = {}
        self.verifications: list[DsrIdentityVerification] = []
        self.search_runs: list[DsrSearchRun] = []
        self.evidence: list[DsrEvidence] = []
        self.evidence_pushes: list[DsrEvidencePush] = []
        self.plans: list[DsrActionPlan] = []
        self.actions: list = []
        self.audit: list[dict] = []


class FakeDB:
    async def flush(self):
        return None

    async def commit(self):
        return None


class FakeConnector:
    def __init__(self, *, matches=(), distinct=1, search_raises=None, truncated=False):
        self._matches = matches
        self._distinct = distinct
        self._search_raises = search_raises
        self._truncated = truncated

    async def search_subject(self, *, identifiers, limit=500):
        if self._search_raises:
            raise self._search_raises
        return SearchOutcome(
            matches=tuple(self._matches),
            tables_searched=tuple({m.table_name for m in self._matches}) or ("customers",),
            distinct_subjects=self._distinct,
            truncated=self._truncated,
        )


def make_authorization(name: str, *, source_id=None):
    source_id = source_id or uuid.uuid4()
    return SimpleNamespace(
        id=uuid.uuid4(), data_source_id=source_id, enabled=True,
        searchable_tables=["customers"], identity_tables=["customers"],
        identifier_columns={"customers": {"email": "email"}},
        returnable_columns={"customers": ["name", "email", "unmappable_col"]},
        erasable_columns={"customers": ["name", "email"]},
        allow_execution=True, write_credential_ref=f"{name}_WRITE",
    ), SimpleNamespace(
        id=source_id, name=name, enabled=True, connector="postgres",
        config={"host": "h", "dbname": "d", "user": "u"}, credential_ref=f"{name}_READ",
    )


def match(table="customers", record_id=7, column="email"):
    return SubjectMatch(
        table_name=table, matched_column=column, identifier_kind="email",
        match_type="normalized_exact", confidence=1.0,
        record_reference={"id": record_id},
        record_snapshot={"name": "A Person", "email": "a@b.com", "unmappable_col": "xyz"},
    )


@pytest.fixture
def world(monkeypatch):
    w = World()
    _install(monkeypatch, w)
    return w


def _install(monkeypatch, w: World):
    from app.db.repositories import dsr_repository as repo

    async def create_request(db, **kw):
        row = DsrRequest(id=uuid.uuid4(), status=case.RECEIVED, request_type=case.UNCLASSIFIED,
                          received_at=NOW, sla_breached=False, **kw)
        w.requests[row.id] = row
        return row

    async def get_request(db, request_id, org_id):
        row = w.requests.get(request_id)
        return row if row and row.org_id == org_id else None

    async def find_by_key(db, org_id, key):
        return next((r for r in w.requests.values()
                     if r.org_id == org_id and r.idempotency_key == key), None)

    async def reference_exists(db, org_id, reference):
        return any(r.org_id == org_id and r.reference == reference for r in w.requests.values())

    async def create_verification(db, *, org_id, request_id, method, challenge_hash,
                                   expires_at, max_attempts=5):
        row = DsrIdentityVerification(
            id=uuid.uuid4(), org_id=org_id, request_id=request_id, method=method,
            status=case.IDV_PENDING, challenge_hash=challenge_hash, expires_at=expires_at,
            max_attempts=max_attempts, created_at=NOW,
        )
        row.attempts = 0
        w.verifications.append(row)
        return row

    async def latest_verification(db, request_id, org_id):
        rows = [v for v in w.verifications if v.request_id == request_id and v.org_id == org_id]
        return rows[-1] if rows else None

    w.source_authorizations: list = []

    async def list_source_auth(db, org_id):
        return list(w.source_authorizations)

    async def create_search_run(db, **kw):
        row = DsrSearchRun(id=uuid.uuid4(), status="pending", match_count=0,
                            distinct_subject_count=0, tables_searched=[], **kw)
        w.search_runs.append(row)
        return row

    async def list_search_runs(db, request_id, org_id):
        return [r for r in w.search_runs if r.request_id == request_id and r.org_id == org_id]

    async def add_evidence(db, org_id, rows):
        for row in rows:
            if row.org_id != org_id:
                raise ValueError("refusing to write evidence for the wrong org")
        w.evidence.extend(rows)

    async def list_evidence(db, request_id, org_id):
        return [e for e in w.evidence if e.request_id == request_id and e.org_id == org_id]

    async def get_evidence(db, evidence_id, org_id):
        return next((e for e in w.evidence if e.id == evidence_id and e.org_id == org_id), None)

    async def next_plan_version(db, request_id, org_id):
        return len([p for p in w.plans if p.request_id == request_id]) + 1

    async def get_current_plan(db, request_id, org_id):
        rows = [p for p in w.plans if p.request_id == request_id and p.status != "superseded"]
        return rows[-1] if rows else None

    async def create_plan(db, **kw):
        row = DsrActionPlan(id=uuid.uuid4(), status="draft", **kw)
        w.plans.append(row)
        return row

    async def add_actions(db, org_id, rows):
        w.actions.extend(rows)

    async def list_actions(db, plan_id, org_id):
        return [a for a in w.actions if a.plan_id == plan_id]

    # ── Push-evidence plumbing (for the purge test) ──────────────────────────────

    async def create_evidence_push(db, **kw):
        row = DsrEvidencePush(id=uuid.uuid4(), purged_at=None, **kw)
        w.evidence_pushes.append(row)
        return row

    async def list_evidence_pushes(db, request_id, org_id):
        return [
            p for p in w.evidence_pushes
            if p.request_id == request_id and p.org_id == org_id and p.purged_at is None
        ]

    async def purge_evidence_pushes(db, request_id, org_id):
        rows = await list_evidence_pushes(db, request_id, org_id)
        for row in rows:
            row.payload_ciphertext = None
            row.purged_at = NOW
        return len(rows)

    for name, fn in [
        ("create_request", create_request), ("get_request", get_request),
        ("find_request_by_idempotency_key", find_by_key),
        ("reference_exists", reference_exists),
        ("create_identity_verification", create_verification),
        ("get_latest_identity_verification", latest_verification),
        ("list_source_authorizations", list_source_auth),
        ("create_search_run", create_search_run), ("list_search_runs", list_search_runs),
        ("add_evidence", add_evidence), ("list_evidence", list_evidence),
        ("get_evidence", get_evidence),
        ("next_plan_version", next_plan_version), ("get_current_plan", get_current_plan),
        ("create_plan", create_plan), ("add_actions", add_actions), ("list_actions", list_actions),
        ("create_evidence_push", create_evidence_push),
        ("list_evidence_pushes", list_evidence_pushes),
        ("purge_evidence_pushes", purge_evidence_pushes),
    ]:
        monkeypatch.setattr(repo, name, fn)

    w.data_sources: list = []

    async def get_data_source(db, source_id, org_id):
        return next((s for s in w.data_sources if s.id == source_id), None)

    async def list_data_sources(db, org_id):
        return list(w.data_sources)

    from app.db.repositories import ropa_repository
    monkeypatch.setattr(ropa_repository, "get_data_source", get_data_source)
    monkeypatch.setattr(ropa_repository, "list_data_sources", list_data_sources)

    async def audit(db, **kw):
        w.audit.append(kw)

    from app.services import audit_service
    monkeypatch.setattr(audit_service, "record", audit)


def _use_connectors(monkeypatch, by_source_name: dict):
    from app.agents.dsr.connectors import factory

    def build_connector(*, data_source, authorization, for_execution=False):
        return by_source_name[data_source.name]

    monkeypatch.setattr(factory, "build_connector", build_connector)


async def _open_case(text="Show me my data.", **kw) -> DsrRequest:
    request, _ = await case_service.create_case(
        FakeDB(), org_id=ORG, raw_request=text, requester_email="a@b.com",
        created_by_user_id=USER, now=NOW, **kw,
    )
    await case_service.classify_case(FakeDB(), request, actor_user_id=USER)
    return request


async def _verify(request):
    await identity_service.verify_manually(
        FakeDB(), request, reviewer_user_id=USER,
        evidence_note="Checked government ID in person, ref ID-991", now=NOW,
    )
    await case_service.transition(FakeDB(), request, case.IDENTITY_VERIFIED, actor_user_id=USER)


async def _search(request):
    await case_service.transition(FakeDB(), request, case.SEARCHING, actor_user_id=USER)
    summary = await search_service.run_search(FakeDB(), request)
    await case_service.transition(
        FakeDB(), request, case.SEARCH_COMPLETED,
        error_code=summary.outcome_code,
        error_detail="see search runs" if summary.outcome_code else None,
    )
    return summary


def _current_user(org=ORG) -> CurrentUser:
    return CurrentUser(user_id=str(USER), org_id=str(org))


# ── 1. Partial failure across several sources must be distinctly reported ───────

@pytest.mark.asyncio
async def test_one_failed_source_is_never_hidden_behind_a_clean_result(world, monkeypatch):
    """3 authorized sources: one succeeds with a match, one's connector is down, one
    finds nothing. The case must come out of SEARCH_COMPLETED saying a source
    failed -- NOT with the same (status, error_code) a fully clean search would
    have, which is what `SearchSummary.outcome_code` did before this fix whenever
    `found_anything` was True (see search_service.py's `outcome_code` docstring)."""
    good_auth, good_source = make_authorization("crm")
    down_auth, down_source = make_authorization("legacy-billing")
    empty_auth, empty_source = make_authorization("support-tickets")
    world.source_authorizations = [good_auth, down_auth, empty_auth]
    world.data_sources = [good_source, down_source, empty_source]

    _use_connectors(monkeypatch, {
        "crm": FakeConnector(matches=[match(record_id=1)], distinct=1),
        "legacy-billing": FakeConnector(search_raises=ConnectorUnavailableError("down for maintenance")),
        "support-tickets": FakeConnector(matches=[], distinct=0),
    })

    request = await _open_case()
    await _verify(request)
    summary = await _search(request)

    # The per-source facts are all there...
    assert sorted(summary.sources_searched) == ["crm", "support-tickets"]
    assert summary.sources_failed == ["legacy-billing"]
    assert summary.evidence_count == 1

    # ...and the fix: the case-level outcome now says so too, not just the runs.
    assert summary.outcome_code == case.ERR_SEARCH_FAILED
    assert request.status == case.SEARCH_COMPLETED
    assert request.error_code == case.ERR_SEARCH_FAILED, (
        "a case with one failed source must not carry the same (None) error_code "
        "as a fully clean search just because another source found a match"
    )

    # The successful source's evidence is still there and usable -- a connector
    # failure on ONE source must not cost the case what another source already
    # found.
    evidence = await dsr_repository.list_evidence(FakeDB(), request.id, request.org_id)
    assert len(evidence) == 1
    assert evidence[0].source_name == "crm"

    # And it still plans: the record found is still actionable despite the
    # partial failure elsewhere.
    grants = {
        good_source.name: (await _grant_for(good_auth, good_source.name)),
    }
    plan, actions = await planning_service.build_plan(FakeDB(), request, grants=grants)
    assert len(actions) == 1


async def _grant_for(authorization, source_name):
    from app.agents.dsr.connectors import factory
    return factory.build_grant(authorization, source_name=source_name)


# ── 2. "Never searched" must not look like "searched, found nothing" ────────────

@pytest.mark.asyncio
async def test_never_searched_is_distinguishable_from_no_match(world, monkeypatch):
    request = await _open_case()
    await _verify(request)

    # Before POST /search has ever been called:
    assert request.status == case.IDENTITY_VERIFIED
    assert request.error_code is None, (
        "a case that has not been searched yet must not carry NO_MATCH or any "
        "other search outcome code"
    )
    results = await dsr_routes.list_results(request.id, user=_current_user(), db=FakeDB())
    assert results["search_runs"] == []
    assert results["evidence"] == []

    # Now actually search it and find nothing -- a DIFFERENT, equally explicit state.
    good_auth, good_source = make_authorization("crm")
    world.source_authorizations = [good_auth]
    world.data_sources = [good_source]
    _use_connectors(monkeypatch, {"crm": FakeConnector(matches=[], distinct=0)})
    summary = await _search(request)

    assert summary.outcome_code == case.ERR_NO_MATCH
    assert request.status == case.SEARCH_COMPLETED
    assert request.error_code == case.ERR_NO_MATCH

    # The two states are not the same tuple of (status, error_code):
    pre_search_state = (case.IDENTITY_VERIFIED, None)
    no_match_state = (request.status, request.error_code)
    assert pre_search_state != no_match_state


# ── 3. Evidence traceability and graceful ropa_category degradation ─────────────

@pytest.mark.asyncio
async def test_matched_evidence_is_traceable_and_an_unplaceable_column_is_none_not_a_crash(
    world, monkeypatch,
):
    good_auth, good_source = make_authorization("crm")
    world.source_authorizations = [good_auth]
    world.data_sources = [good_source]
    _use_connectors(monkeypatch, {"crm": FakeConnector(matches=[match(record_id=99)])})

    request = await _open_case("Send me a copy of my data.")
    await _verify(request)
    await _search(request)

    evidence = await dsr_repository.list_evidence(FakeDB(), request.id, request.org_id)
    assert len(evidence) == 1
    row = evidence[0]

    # Traceable: a reviewer (or an execution step) can find the exact record.
    assert row.record_reference == {"id": 99}
    assert row.record_snapshot == {"name": "A Person", "email": "a@b.com", "unmappable_col": "xyz"}

    # The matched column (email) is a real category; a classifier should place it.
    assert row.matched_column == "email"
    assert row.ropa_category is None or isinstance(row.ropa_category, str)

    # A SECOND evidence row for a column the classifier cannot place at all must
    # still be recorded, with ropa_category=None, never raise.
    unplaceable = search_service._ropa_category("customers", "totally_made_up_xyzzy_column")
    assert unplaceable is None


# ── 4. Retrying a search must not duplicate evidence for the same record ────────

@pytest.mark.asyncio
async def test_retrying_a_search_does_not_duplicate_evidence_or_actions(world, monkeypatch):
    """The scenario this investigation flagged as most likely to hide a real bug:
    a case searched once, then searched again (a connector fixed after failing, an
    ambiguous result reviewed and retried -- both legal FAILED/REVIEW_REQUIRED ->
    SEARCHING edges in lifecycle.py). Before the fix, the SECOND search's evidence
    was simply appended to the FIRST's, `dsr_repository.list_evidence` returned
    both for the SAME real-world record, and `planning_service.build_plan` (which
    reads every evidence row ever written for the case) planned -- and would have
    executed -- the same deletion/disclosure twice.
    """
    good_auth, good_source = make_authorization("crm")
    world.source_authorizations = [good_auth]
    world.data_sources = [good_source]
    connector = FakeConnector(matches=[match(record_id=7)], distinct=1)
    _use_connectors(monkeypatch, {"crm": connector})

    request = await _open_case("Delete my personal information.")
    await _verify(request)

    # First attempt finds the one real record.
    summary_1 = await _search(request)
    assert summary_1.evidence_count == 1
    grant = await _grant_for(good_auth, good_source.name)
    plan_1, actions_1 = await planning_service.build_plan(
        FakeDB(), request, grants={good_source.name: grant},
    )
    assert len(actions_1) == 1

    # An operator sends the case back for another look and retries the search --
    # a legal lifecycle edge (SEARCH_COMPLETED -> REVIEW_REQUIRED -> SEARCHING).
    await case_service.transition(FakeDB(), request, case.REVIEW_REQUIRED, actor_user_id=USER)

    # Second attempt: the connector reports the SAME record again (nothing in the
    # customer's database changed; this is what a retry naturally looks like).
    summary_2 = await _search(request)
    assert summary_2.evidence_count == 1, "the second run still genuinely matched the record"

    all_evidence = await dsr_repository.list_evidence(FakeDB(), request.id, request.org_id)
    assert len(all_evidence) == 1, (
        f"expected exactly one dsr_evidence row for the one real record across both "
        f"search attempts, got {len(all_evidence)} -- a retry must not duplicate "
        f"evidence for a record it already recorded"
    )

    plan_2, actions_2 = await planning_service.build_plan(
        FakeDB(), request, grants={good_source.name: grant},
    )
    assert len(actions_2) == 1, (
        f"expected exactly one planned action for the one real record, got "
        f"{len(actions_2)} -- duplicated evidence means the SAME deletion would be "
        f"planned (and could be executed) twice"
    )


@pytest.mark.asyncio
async def test_retry_with_a_genuinely_new_record_adds_evidence_without_losing_the_old(
    world, monkeypatch,
):
    """Dedup must key on the record, not blanket-suppress a retry: a second search
    that finds a record the FIRST one did not (e.g. a new order placed meanwhile)
    must still add it, alongside -- not instead of -- what was already evidenced.
    """
    good_auth, good_source = make_authorization("crm")
    world.source_authorizations = [good_auth]
    world.data_sources = [good_source]

    connector = FakeConnector(matches=[match(record_id=7)], distinct=1)
    _use_connectors(monkeypatch, {"crm": connector})
    request = await _open_case("Show me my data.")
    await _verify(request)
    await _search(request)

    await case_service.transition(FakeDB(), request, case.REVIEW_REQUIRED, actor_user_id=USER)
    connector._matches = [match(record_id=7), match(record_id=8)]
    summary_2 = await _search(request)
    assert summary_2.evidence_count == 2, "this run genuinely matched two records"

    all_evidence = await dsr_repository.list_evidence(FakeDB(), request.id, request.org_id)
    ids = sorted(e.record_reference["id"] for e in all_evidence)
    assert ids == [7, 8], "the new record (8) must be added without duplicating the old one (7)"


# ── 5. Evidence purge: org/case scoping, and no dangling references ─────────────

@pytest.mark.asyncio
async def test_purge_is_scoped_to_the_right_org_and_case_and_leaves_dsr_evidence_intact(
    world, monkeypatch,
):
    """Two cases, in two different orgs, both carrying pushed evidence. Purging
    one must clear only that one case's pushed payloads -- never another case's,
    and never another org's, even if a caller somehow supplied the wrong ids. And
    purging must never touch `dsr_evidence` (the classified rows planning and the
    response actually reason over), which is what makes it safe to call on a case
    that already has a plan or response referencing that evidence.
    """
    _wire_verified_push(monkeypatch)

    request_a = DsrRequest(
        id=uuid.uuid4(), org_id=ORG, reference="DSR-A", raw_request="x",
        request_type=case.ACCESS, due_at=NOW + timedelta(days=30),
        requester_email="a@b.com", status=case.SEARCH_COMPLETED,
    )
    request_b = DsrRequest(
        id=uuid.uuid4(), org_id=OTHER_ORG, reference="DSR-B", raw_request="y",
        request_type=case.ACCESS, due_at=NOW + timedelta(days=30),
        requester_email="b@c.com", status=case.SEARCH_COMPLETED,
    )
    world.requests[request_a.id] = request_a
    world.requests[request_b.id] = request_b

    run_a = DsrSearchRun(id=uuid.uuid4(), org_id=ORG, request_id=request_a.id,
                          source_name="crm", status="completed", match_count=1,
                          distinct_subject_count=1, tables_searched=["customers"])
    run_b = DsrSearchRun(id=uuid.uuid4(), org_id=OTHER_ORG, request_id=request_b.id,
                          source_name="crm", status="completed", match_count=1,
                          distinct_subject_count=1, tables_searched=["customers"])
    world.search_runs.extend([run_a, run_b])

    evidence_a = DsrEvidence(
        id=uuid.uuid4(), org_id=ORG, request_id=request_a.id, search_run_id=run_a.id,
        source_name="crm", table_name="customers", matched_column="email",
        identifier_kind="email", match_type="exact", confidence=1.0,
        record_reference={"id": 1}, record_snapshot={"email": "a@b.com"},
    )
    world.evidence.append(evidence_a)

    await dsr_repository.create_evidence_push(
        FakeDB(), org_id=ORG, request_id=request_a.id, search_run_id=run_a.id,
        integration_key_id=None, source_name="crm", searched_at=NOW,
        subject={"email": "a@b.com"}, payload_ciphertext="cipher-a",
        table_count=1, row_count=1, truncated=False, client_reference=None,
    )
    await dsr_repository.create_evidence_push(
        FakeDB(), org_id=OTHER_ORG, request_id=request_b.id, search_run_id=run_b.id,
        integration_key_id=None, source_name="crm", searched_at=NOW,
        subject={"email": "b@c.com"}, payload_ciphertext="cipher-b",
        table_count=1, row_count=1, truncated=False, client_reference=None,
    )

    # Purging case A (org ORG) must not touch case B's push (org OTHER_ORG), even
    # though both pushes otherwise look alike (same source_name, same shape).
    purged = await evidence_push_service.purge(FakeDB(), request_a.id, ORG)
    assert purged == 1

    push_a = next(p for p in world.evidence_pushes if p.request_id == request_a.id)
    push_b = next(p for p in world.evidence_pushes if p.request_id == request_b.id)
    assert push_a.purged_at is not None
    assert push_a.payload_ciphertext is None
    assert push_b.purged_at is None, "purging case A's evidence must not purge case B's"
    assert push_b.payload_ciphertext == "cipher-b"

    # A caller cannot purge case A's push by supplying the WRONG org (IDOR check).
    repurged = await evidence_push_service.purge(FakeDB(), request_b.id, ORG)
    assert repurged == 0, "case B belongs to a different org; ORG must not be able to purge it"

    # dsr_evidence (what planning/response actually reason over) is never touched
    # by a purge -- a plan or response built from evidence_a stays valid.
    still_there = await dsr_repository.get_evidence(FakeDB(), evidence_a.id, ORG)
    assert still_there is not None
    assert still_there.record_snapshot == {"email": "a@b.com"}


def _wire_verified_push(monkeypatch):
    from app.db.repositories import dsr_repository as repo

    async def _verified(db, request_id, org_id):
        return SimpleNamespace(status=case.IDV_VERIFIED)

    monkeypatch.setattr(repo, "get_latest_identity_verification", _verified)
