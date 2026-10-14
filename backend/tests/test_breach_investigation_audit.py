"""Audit of Agent 4's Investigation lane: evidence/redaction, timeline, affected
systems/data/subjects, impact totals, risk assessment, and the `incident_analysis`
worker job.

Companion to tests/test_breach_investigation.py and tests/test_breach_worker.py --
this file does not repeat what those already cover (read both before adding here).
It documents two confirmed under-redaction defects found and fixed in this audit,
plus verifies properties the brief called out that were already correct.

F-6  a connection-string-style credential (`scheme://user:pass@host`) was not
     recognised as a secret at all -- a database DSN pasted into an incident, one of
     the most common real shapes of breach evidence, went into evidence.detail
     unredacted.
F-7  the bare key `pass` (as distinct from `password`/`pwd`/`passwd`) was not in
     `_SECRET_WORDS`, so a field literally named `pass` was stored in the clear.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.breach.rules import risk as risk_rules
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import investigation_service as inv
from app.agents.ropa.rules import personal_data_rules
from app.api.v1.routes import incidents as routes
from app.core.security import CurrentUser
from app.db.repositories import incident_repository
from app.services import incident_run_service
from tests.test_breach_end_to_end import (
    NOW,
    ORG,
    USER,
    World,
    _assign_id,
    _DB,
    open_incident,
    wire,
)

_USER = CurrentUser(user_id=str(USER), org_id=str(ORG))


# ── F-6: connection-string credentials ───────────────────────────────────────────

@pytest.mark.parametrize(
    "value",
    [
        "postgres://admin:S3cr3t!pass@db.internal:5432/prod",
        "mysql://root:hunter2@127.0.0.1:3306/app",
        "mongodb+srv://svc_user:tr0ub4dor@cluster0.abcde.mongodb.net/mydb",
        "redis://:myredispassword@cache.internal:6379/0",
        # A single bearer-style credential with no username, e.g. a scoped git token.
        "https://ghp_abcdEFGH12345678@github.com/org/repo.git",
    ],
)
def test_connection_string_credential_is_redacted(value):
    clean, found = inv.redact({"detail": value})
    assert found, f"a credential embedded in {value!r} was not recognised"
    for secret_fragment in ("S3cr3t!pass", "hunter2", "tr0ub4dor", "myredispassword", "ghp_abcdEFGH12345678"):
        assert secret_fragment not in clean["detail"]
    # The host/path -- what a responder actually needs to trace the system -- survives.
    assert "@" in clean["detail"] and "[redacted]" in clean["detail"]


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/page?q=a@b",  # "@" only appears after a path segment
        "mailto:someone@example.com",       # no "//" authority at all
        "See https://docs.example.com/guide for the runbook",
    ],
)
def test_urls_without_an_embedded_credential_are_left_alone(value):
    clean, found = inv.redact({"note": value})
    assert not found, f"{value!r} has no credential in it and should survive untouched"
    assert clean["note"] == value


# ── F-7: the bare `pass` key ──────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["pass", "db_pass", "PASS", "userPass", "x-pass"])
def test_bare_pass_key_is_redacted(key):
    assert inv.is_secret_key(key), f"{key!r} should be recognised as a secret field"
    clean, found = inv.redact({key: "hunter2"})
    assert found
    assert clean[key] == "[redacted]"


@pytest.mark.parametrize("key", ["passenger_count", "bypass_reason", "surpass_target"])
def test_pass_as_a_substring_of_an_unrelated_word_is_not_redacted(key):
    """Word-level matching, not substring: `passenger` must not become a secret just
    because it contains the letters p-a-s-s."""
    assert not inv.is_secret_key(key)
    clean, found = inv.redact({key: "ordinary value"})
    assert not found
    assert clean[key] == "ordinary value"


# ── Secret redaction: the wider sweep the audit asked for ────────────────────────

def test_aws_style_access_key_pair():
    clean, found = inv.redact({
        "aws_access_key_id": "AKIAABCDEFGHIJKLMNOP",
        "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    })
    assert found
    assert clean["aws_access_key_id"] == "[redacted]"
    # The secret key's NAME matches (`secret`), so it is redacted by the key check --
    # the value-level AKIA pattern only ever had to catch the id half.
    assert clean["aws_secret_access_key"] == "[redacted]"


def test_jwt_in_a_raw_value():
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    clean, found = inv.redact({"note": f"session token was {jwt}"})
    assert found
    assert jwt not in clean["note"]
    assert clean["note"].startswith("session token was [redacted]")


def test_bearer_header_shaped_string():
    clean, found = inv.redact({
        "line": "curl -H 'Authorization: Bearer abcDEF123456789xyzTOKEN' https://api.internal/v1/users"
    })
    assert found
    assert "abcDEF123456789xyzTOKEN" not in clean["line"]
    assert clean["line"].startswith("curl -H 'Authorization: Bearer [redacted]'")
    assert clean["line"].endswith("https://api.internal/v1/users")


@pytest.mark.parametrize("key", ["pwd", "pass", "secret", "api_key", "apiKey", "API-KEY", "db_password"])
def test_password_naming_conventions_from_the_audit_brief(key):
    assert inv.is_secret_key(key)


def test_nested_dicts_and_lists_at_depth_are_redacted():
    clean, found = inv.redact({
        "level1": {
            "level2": [
                {"level3": {"password": "deep-secret"}},
                "an ordinary string",
                {"api_key": "sk-abcdef"},
            ]
        },
        "top_level_token": "x",
    })
    assert found
    assert clean["level1"]["level2"][0]["level3"]["password"] == "[redacted]"
    assert clean["level1"]["level2"][1] == "an ordinary string"
    assert clean["level1"]["level2"][2]["api_key"] == "[redacted]"
    assert clean["top_level_token"] == "[redacted]"


@pytest.mark.parametrize("empty_value", [None, "", 0, False])
def test_a_secret_shaped_key_with_an_empty_value_does_not_crash(empty_value):
    clean, found = inv.redact({"password": empty_value, "src_ip": "10.0.0.4"})
    assert found
    assert clean["password"] == "[redacted]"
    assert clean["src_ip"] == "10.0.0.4"


def test_a_field_not_shaped_like_a_secret_is_not_over_redacted():
    """A field whose VALUE happens to look credential-shaped, but whose key does not
    say "secret" and whose value does not match one of the narrow value patterns,
    must survive -- over-redaction destroys evidence a responder needs."""
    clean, found = inv.redact({
        "ticket_reference": "INC-48213-ABCXYZ99",
        "comment": "The device serial number was XJ29-SECURE-001.",
    })
    assert not found
    assert clean["ticket_reference"] == "INC-48213-ABCXYZ99"
    assert clean["comment"] == "The device serial number was XJ29-SECURE-001."


def test_redact_is_tolerant_of_a_non_dict_detail():
    assert inv.redact(None) == ({}, False)
    assert inv.redact("just a string") == ({}, False)
    assert inv.redact([1, 2, 3]) == ({}, False)


# ── Evidence detail withholding: list vs single GET ──────────────────────────────

@pytest.mark.asyncio
async def test_evidence_list_withholds_detail_even_for_a_secret_free_row(monkeypatch):
    """The module docstring's claim is that list responses withhold detail from
    EVERY row, not just ones that carried a secret -- verify both halves."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )

    plain = await inv.add_evidence(
        db, case, kind=vocab.EV_ACCESS_LOG, source_system="siem",
        summary="A clean access log entry", detail={"src_ip": "203.0.113.9"},
        actor_user_id=USER,
    )
    secret = await inv.add_evidence(
        db, case, kind=vocab.EV_AUTH_EVENT, source_system="okta",
        summary="Login carried a stray bearer token",
        detail={"note": "Authorization: Bearer abcDEF123456789xyzTOKEN"},
        actor_user_id=USER,
    )
    assert not plain.contains_secrets
    assert secret.contains_secrets

    async def get_evidence(db, evidence_id, org_id):
        return next(
            (e for e in world.evidence if e.id == evidence_id and e.org_id == org_id), None
        )

    monkeypatch.setattr(incident_repository, "get_evidence", get_evidence)

    rows = await routes.list_evidence(case.id, user=_USER, db=db)
    by_id = {r["id"]: r for r in rows}
    for row in rows:
        assert "detail" not in row, "list_evidence must never carry the detail field"
    assert by_id[str(plain.id)]["has_detail"] is True
    assert by_id[str(plain.id)]["contains_secrets"] is False
    assert by_id[str(secret.id)]["has_detail"] is True
    assert by_id[str(secret.id)]["contains_secrets"] is True

    # The single-evidence GET returns detail, but only the already-redacted version --
    # never the raw secret, which was never stored in the first place.
    detail_view = await routes.get_evidence_detail(case.id, secret.id, user=_USER, db=db)
    assert "abcDEF123456789xyzTOKEN" not in str(detail_view["detail"])
    assert detail_view["detail"]["note"].startswith("Authorization: Bearer [redacted]")
    assert detail_view["contains_secrets"] is True


# ── Affected-data derivation reuses Agent 2's classifier ─────────────────────────

def test_risk_category_weights_cover_exactly_ropa_personal_categories():
    """risk.py's weight table must track Agent 2's taxonomy, not a second copy of it
    that can silently drift -- every weighted category must be one ROPA's classifier
    actually produces, and every category of ROPA's that matters should have a
    deliberate weight rather than falling through to the generic 'unknown' bucket."""
    ropa_categories = {
        personal_data_rules.CATEGORY_CONTACT,
        personal_data_rules.CATEGORY_IDENTITY,
        personal_data_rules.CATEGORY_GOVERNMENT_ID,
        personal_data_rules.CATEGORY_ONLINE_ID,
        personal_data_rules.CATEGORY_LOCATION,
        personal_data_rules.CATEGORY_FINANCIAL,
        personal_data_rules.CATEGORY_EMPLOYMENT,
        personal_data_rules.CATEGORY_CREDENTIAL,
        personal_data_rules.CATEGORY_HEALTH,
        personal_data_rules.CATEGORY_PROFESSIONAL,
        personal_data_rules.CATEGORY_FREE_TEXT,
        personal_data_rules.CATEGORY_BEHAVIOURAL,
    }
    assert set(risk_rules._CATEGORY_WEIGHT.keys()) == ropa_categories


@pytest.mark.asyncio
async def test_derive_affected_data_prefers_ropas_stored_classification(monkeypatch):
    """The baseline's own `classifications` map IS Agent 2's verdict and must win over
    re-deriving it -- re-deriving risks the two agents disagreeing about one column."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    # free_text_notes would classify as CATEGORY_FREE_TEXT (or not personal at all) if
    # re-derived from the column name alone, but the stored baseline says otherwise --
    # the stored verdict must be the one that lands.
    world.baselines["billing-db"] = {
        "tables": ["invoices"],
        "columns": {"invoices.free_text_notes": "text"},
        "classifications": {"invoices.free_text_notes": personal_data_rules.CATEGORY_FINANCIAL},
    }
    system = await inv.record_affected_system(
        db, case, system_name="billing-db", system_kind=vocab.SYS_DATABASE,
        actor_user_id=USER,
    )
    system.data_source_id = uuid.uuid4()

    rows, gaps = await inv.derive_affected_data(db, case)
    assert not gaps
    assert len(rows) == 1
    assert rows[0].data_category == personal_data_rules.CATEGORY_FINANCIAL
    assert rows[0].derived_from == "ropa_metadata"
    assert rows[0].confidence == vocab.POSSIBLE


@pytest.mark.asyncio
async def test_derive_affected_data_falls_back_to_the_real_classifier_when_unclassified(monkeypatch):
    """A baseline taken before a classifier improvement may have a column with no
    stored verdict at all -- the fallback must call Agent 2's actual rules, not a
    breach-agent guess, and must still drop anything that comes back non-personal."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    world.baselines["billing-db"] = {
        "tables": ["customers"],
        "columns": {"customers.email": "text", "customers.row_version": "integer"},
        "classifications": {},  # neither column was classified when this was taken
    }
    system = await inv.record_affected_system(
        db, case, system_name="billing-db", system_kind=vocab.SYS_DATABASE,
        actor_user_id=USER,
    )
    system.data_source_id = uuid.uuid4()

    rows, gaps = await inv.derive_affected_data(db, case)
    categories = {r.data_category for r in rows}
    # email is personal data by the real classifier; row_version is not and must not
    # appear just because the loop touched it.
    assert personal_data_rules.CATEGORY_CONTACT in categories
    assert not any(r.column_name == "row_version" for r in rows)


@pytest.mark.asyncio
async def test_derive_affected_data_notes_gaps_instead_of_asserting_no_data(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    await inv.record_affected_system(
        db, case, system_name="unknown-legacy-ftp", system_kind=vocab.SYS_DATABASE,
        actor_user_id=USER,
    )
    rows, gaps = await inv.derive_affected_data(db, case)
    assert rows == []
    assert gaps and "unknown-legacy-ftp" in gaps[0]


# ── Impact totals: never a bare number ───────────────────────────────────────────

class _Subject:
    def __init__(self, count_basis, record_count):
        self.count_basis = count_basis
        self.record_count = record_count


@pytest.mark.parametrize("subjects", [[], None])
def test_impact_total_with_no_subjects_recorded(subjects):
    total, basis = inv.impact_total(subjects)
    assert total is None
    assert basis == "unknown"


def test_impact_total_with_one_group_unknown_refuses_to_sum():
    """One unrounded group makes the WHOLE total unknowable, not merely approximate --
    summing the known groups and presenting that as the total would be exactly the
    false precision this function exists to prevent."""
    subjects = [
        _Subject("counted", 400),
        _Subject("unknown", None),
    ]
    total, basis = inv.impact_total(subjects)
    assert total is None
    assert basis == "unknown"


def test_impact_total_mixing_counted_and_estimated_groups_is_marked_estimated():
    subjects = [_Subject("counted", 100), _Subject("estimated", 50)]
    total, basis = inv.impact_total(subjects)
    assert total == 150
    assert basis == "estimated"


def test_impact_total_tolerates_a_record_count_of_none_even_if_basis_says_counted():
    """Defensive: validation at write time should prevent `counted` with no number,
    but the read path must not crash or silently sum a None if that invariant is
    ever violated by a future caller."""
    subjects = [_Subject("counted", None)]
    total, basis = inv.impact_total(subjects)
    assert total is None
    assert basis == "unknown"


@pytest.mark.asyncio
async def test_get_impact_route_always_carries_the_basis_with_zero_subjects(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    impact = await routes.get_impact(case.id, user=_USER, db=db)
    assert impact["subjects"] == []
    assert impact["total"] == {"record_count": None, "count_basis": "unknown"}


# ── Risk assessment: explainable and never silently empty ───────────────────────

@pytest.mark.asyncio
async def test_risk_assessment_reason_factors_and_regulatory_context_are_populated(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account with no reason to touch it queried it all night.",
    )
    assessment = await incident_run_service.assess_and_store_risk(db, case)
    assert assessment.reason
    assert assessment.factors, "a real assessment must show at least its base factors"
    row = next(r for r in world.risks if r.incident_id == case.id)
    assert row.reason
    assert row.factors
    assert row.regulatory_context == []  # patched to return [] in this harness, but
    # present as a key, never missing/None
    assert row.version == 1


@pytest.mark.asyncio
async def test_risk_assessments_are_versioned_history_not_overwritten(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account with no reason to touch it queried it all night.",
    )
    first = await incident_run_service.assess_and_store_risk(db, case)
    await inv.record_affected_subjects(
        db, case, subject_group="customers", record_count=50_000,
        count_basis="counted", basis_note="query log distinct ids", actor_user_id=USER,
    )
    second = await incident_run_service.assess_and_store_risk(db, case)

    org_risks = [r for r in world.risks if r.incident_id == case.id]
    assert len(org_risks) == 2, "the earlier assessment must survive as history"
    versions = sorted(r.version for r in org_risks)
    assert versions == [1, 2]
    superseded = {r.version: r.review_status for r in org_risks}
    assert superseded[1] == "superseded"
    assert superseded[2] == "pending"
    assert second.score >= first.score, "50,000 more counted subjects must not lower the score"


# ── The incident_analysis worker job ─────────────────────────────────────────────

class _SessionCM:
    """A fake `async with async_session_factory() as db:` that hands back the same
    fake session every time, so a test can call the worker entry point directly
    against the in-memory World the rest of this suite already uses."""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc_info):
        return False


def _wire_worker(monkeypatch, world, db):
    wire(monkeypatch, world)
    monkeypatch.setattr(incident_run_service, "async_session_factory", lambda: _SessionCM(db))
    monkeypatch.setattr(incident_run_service, "_regulatory_context", _no_regulatory_context)


async def _no_regulatory_context(db, case, assessment):
    return []


@pytest.mark.asyncio
async def test_run_analysis_reaching_response_pending_end_to_end(monkeypatch):
    world = World()
    db = _DB()
    _wire_worker(monkeypatch, world, db)

    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account with no reason to touch it queried it all night.",
    )
    evidence = await inv.add_evidence(
        db, case, kind=vocab.EV_ACCESS_LOG, source_system="siem",
        summary="400 SELECTs against customers from 203.0.113.9",
        detail={"query_count": 400}, observed_at=NOW, actor_user_id=USER,
    )
    system = await inv.record_affected_system(
        db, case, system_name="crm-postgres", system_kind=vocab.SYS_DATABASE,
        evidence_id=evidence.id, actor_user_id=USER,
    )
    system.data_source_id = uuid.uuid4()
    world.baselines["crm-postgres"] = {
        "tables": ["customers"],
        "columns": {"customers.email": "text"},
        "classifications": {"customers.email": personal_data_rules.CATEGORY_CONTACT},
    }

    await incident_run_service.run_analysis(case.id, case.org_id)

    # unauthorized_access's playbook needs no approval, so build_response_plan may
    # advance the case straight past RESPONSE_PENDING to APPROVED on its own (F-4) --
    # the point of this assertion is that analysis never leaves the case stuck in an
    # assessment stage or marked FAILED, not which exact downstream status it reaches.
    assert case.status in (
        vocab.RESPONSE_PENDING, vocab.REVIEW_REQUIRED,
        vocab.APPROVAL_REQUIRED, vocab.APPROVED,
    )
    assert len(world.data) >= 1
    assert len(world.risks) == 1
    assert len(world.timeline) >= 1


@pytest.mark.asyncio
async def test_a_redelivered_analysis_job_does_not_duplicate_timeline_or_affected_data(monkeypatch):
    """The job has to be safe against running twice for the same incident -- a worker
    crash recovered by the stale-job reaper, or a duplicate enqueue, must not leave
    the incident with two copies of the same timeline entry or the same affected-data
    row. (A new risk-assessment VERSION on a second run is the intended, documented
    behaviour -- see IncidentRiskAssessment's own docstring: "re-assessing ... is
    normal". What must not happen is silent duplication of facts, as opposed to a new
    recorded opinion about them.)"""
    world = World()
    db = _DB()
    _wire_worker(monkeypatch, world, db)

    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account with no reason to touch it queried it all night.",
    )
    evidence = await inv.add_evidence(
        db, case, kind=vocab.EV_ACCESS_LOG, source_system="siem",
        summary="400 SELECTs against customers from 203.0.113.9",
        detail={"query_count": 400}, observed_at=NOW, actor_user_id=USER,
    )
    system = await inv.record_affected_system(
        db, case, system_name="crm-postgres", system_kind=vocab.SYS_DATABASE,
        evidence_id=evidence.id, actor_user_id=USER,
    )
    system.data_source_id = uuid.uuid4()
    world.baselines["crm-postgres"] = {
        "tables": ["customers"],
        "columns": {"customers.email": "text"},
        "classifications": {"customers.email": personal_data_rules.CATEGORY_CONTACT},
    }

    await incident_run_service.run_analysis(case.id, case.org_id)
    timeline_after_first = len(world.timeline)
    data_after_first = len(world.data)

    # Nothing about the incident's underlying facts changed; this models a
    # redelivered/duplicate job, not a legitimate re-analysis after new evidence.
    await incident_run_service.run_analysis(case.id, case.org_id)

    assert len(world.timeline) == timeline_after_first, (
        "seed_timeline_from_evidence must not re-insert the same evidence-derived "
        "entry on a second run"
    )
    assert len(world.data) == data_after_first, (
        "derive_affected_data replaces rather than appends, so a second run with "
        "unchanged systems must not grow the affected-data set"
    )
    assert case.status != vocab.FAILED


@pytest.mark.asyncio
async def test_an_unexpected_crash_mid_analysis_leaves_an_explicit_failed_state(monkeypatch):
    """A crashed worker must never leave a case looking like it is still running --
    the incident has to land somewhere a person can see and act on, with a reason."""
    world = World()
    db = _DB()
    _wire_worker(monkeypatch, world, db)

    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account with no reason to touch it queried it all night.",
    )

    async def boom(db, case, *, actor_user_id=None):
        raise RuntimeError("schema baseline read exploded")

    monkeypatch.setattr(incident_run_service.investigation_service, "derive_affected_data", boom)

    with pytest.raises(RuntimeError):
        await incident_run_service.run_analysis(case.id, case.org_id)

    assert case.status == vocab.FAILED
    assert case.error_code == vocab.ERR_INVESTIGATION_FAILED


@pytest.mark.asyncio
async def test_run_analysis_is_scoped_to_the_calling_org(monkeypatch):
    """The worker job takes only ids -- it must not resolve an incident that belongs
    to a different organisation just because the id matches."""
    world = World()
    db = _DB()
    _wire_worker(monkeypatch, world, db)

    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account with no reason to touch it queried it all night.",
    )
    other_org = uuid.uuid4()
    assert other_org != case.org_id

    from app.agents.breach.errors import IncidentNotFoundError
    with pytest.raises(IncidentNotFoundError):
        await incident_run_service.run_analysis(case.id, other_org)
