"""Evidence, timeline and impact (§11-§17), plus the cross-agent test §45 requires.

The theme throughout: a claim carries its own confidence and cites what it rests on,
and the system refuses to let an unsupported assertion look like a supported one.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.breach.errors import EvidenceUnavailableError, InvalidIncidentError
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import investigation_service as inv
from app.db.models import IncidentCase

NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
ORG = uuid.uuid4()
USER = uuid.uuid4()


def make_case() -> IncidentCase:
    return IncidentCase(
        id=uuid.uuid4(), org_id=ORG, reference="INC-TEST01",
        title="Unauthorized access to the customer database",
        description="Suspicious login followed by a bulk read.",
        source=vocab.SOURCE_SIEM, detected_at=NOW, status=vocab.INVESTIGATING,
    )


class _DB:
    async def flush(self):
        return None


# ── Secret redaction happens on the way IN ───────────────────────────────────────

def test_secrets_are_stripped_from_evidence_detail():
    """Incident evidence routinely arrives as a raw log line, which is exactly where a
    bearer token ends up."""
    clean, found = inv.redact({
        "user": "alice", "password": "hunter2", "Authorization": "Bearer abc.def",
        "src_ip": "10.0.0.4",
    })
    assert found
    assert clean["password"] == "[redacted]"
    assert clean["Authorization"] == "[redacted]"
    assert clean["user"] == "alice"
    assert clean["src_ip"] == "10.0.0.4"


def test_redaction_reaches_nested_structures():
    clean, found = inv.redact({
        "request": {"headers": {"cookie": "sid=abc"}, "path": "/api"},
        "events": [{"api_key": "k-1"}, {"ok": True}],
    })
    assert found
    assert clean["request"]["headers"]["cookie"] == "[redacted]"
    assert clean["request"]["path"] == "/api"
    assert clean["events"][0]["api_key"] == "[redacted]"
    assert clean["events"][1]["ok"] is True


def test_clean_detail_is_left_alone():
    clean, found = inv.redact({"src_ip": "10.0.0.4", "rows_read": 1200})
    assert not found
    assert clean == {"src_ip": "10.0.0.4", "rows_read": 1200}


@pytest.mark.parametrize("value", [None, "a string", 42, []])
def test_redaction_survives_a_non_dict(value):
    assert inv.redact(value) == ({}, False)


@pytest.mark.parametrize(
    "key",
    [
        # The one a live run caught: an exact-match list let this straight through.
        "db_password", "dbPassword", "DB_PASSWORD",
        "admin_token", "X-API-Key", "api_key", "apiKey",
        "client_secret", "private_key", "refresh_token", "access_token",
        "session_id", "sessionCookie", "auth_header", "user_passphrase",
        # And the bare forms, which conventionally hold the credential itself.
        "session", "auth", "password", "Authorization",
    ],
)
def test_a_secret_is_caught_however_the_field_is_spelt(key):
    clean, found = inv.redact({key: "hunter2", "src_ip": "10.0.0.4"})
    assert found, f"{key} was not recognised as a secret"
    assert clean[key] == "[redacted]"
    assert clean["src_ip"] == "10.0.0.4"


@pytest.mark.parametrize(
    "key",
    [
        # Over-redaction blinds an investigation, so the qualified words must not fire
        # on their own. Each of these is a fact a responder needs.
        "session_count", "session_duration", "auth_event_type", "authentication_event",
        "api_version", "access_time", "client_ip", "private_network", "query_count",
        "refresh_interval",
    ],
)
def test_ordinary_log_fields_are_not_redacted(key):
    clean, found = inv.redact({key: 42})
    assert not found, f"{key} was redacted; the value a responder needs is gone"
    assert clean[key] == 42


def test_a_secret_inside_a_log_line_is_stripped_but_the_line_survives():
    """A raw log line has no secret-shaped key at all -- the credential is in the
    value. Redacting the whole line would throw away the evidence, so only the
    credential goes."""
    clean, found = inv.redact({
        "line": "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig' https://api",
        "cmd": "PGPASSWORD=hunter2 psql -h db -U app",
        "aws": "used AKIAIOSFODNN7EXAMPLE to list the bucket",
    })
    assert found
    blob = str(clean)
    assert "eyJhbGciOiJIUzI1NiJ9" not in blob
    assert "hunter2" not in blob
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    # The part a responder actually reads is still there.
    assert "curl -H 'Authorization: Bearer [redacted]' https://api" == clean["line"]
    assert "psql -h db -U app" in clean["cmd"]
    assert clean["aws"].startswith("used [redacted] to list")


def test_ordinary_prose_is_not_mangled():
    clean, found = inv.redact({
        "note": "The session lasted 40 minutes and the user had access to two tables.",
    })
    assert not found
    assert clean["note"].startswith("The session lasted 40 minutes")


@pytest.mark.asyncio
async def test_evidence_records_that_it_was_redacted(monkeypatch):
    """The flag matters: a reviewer must know the stored detail is not the whole log
    line, and an export must know not to render it."""
    case = make_case()
    stored = {}

    async def _add(db, org_id, row):
        stored["row"] = row
        return row

    async def _audit(db, **kw):
        stored["audit"] = kw

    monkeypatch.setattr(inv.incident_repository, "add_evidence", _add)
    monkeypatch.setattr(inv.audit_service, "record", _audit)

    row = await inv.add_evidence(
        _DB(), case, kind=vocab.EV_AUTH_EVENT, source_system="okta",
        summary="Login from an unrecognised device",
        detail={"user": "alice", "session": "s-991"},
    )
    assert row.contains_secrets
    assert row.detail["session"] == "[redacted]"
    # The audit entry carries the summary, never the detail -- it must not become a
    # second copy of what was just redacted.
    assert "detail" not in stored["audit"]["after"]
    assert stored["audit"]["after"]["secrets_redacted"] is True


@pytest.mark.asyncio
async def test_evidence_needs_a_readable_summary(monkeypatch):
    case = make_case()
    monkeypatch.setattr(inv.incident_repository, "add_evidence", _noop_add)
    monkeypatch.setattr(inv.audit_service, "record", _noop_audit)
    with pytest.raises(InvalidIncidentError):
        await inv.add_evidence(
            _DB(), case, kind=vocab.EV_ACCESS_LOG, source_system="db", summary="   "
        )


@pytest.mark.asyncio
async def test_an_unknown_evidence_kind_is_refused(monkeypatch):
    case = make_case()
    monkeypatch.setattr(inv.incident_repository, "add_evidence", _noop_add)
    with pytest.raises(InvalidIncidentError):
        await inv.add_evidence(
            _DB(), case, kind="vibes", source_system="x", summary="y"
        )


@pytest.mark.asyncio
async def test_ropa_context_is_marked_derived(monkeypatch):
    """Evidence Consiva produced about itself is not evidence that something
    happened, and the risk engine weights it accordingly."""
    case = make_case()
    monkeypatch.setattr(inv.incident_repository, "add_evidence", _noop_add)
    monkeypatch.setattr(inv.audit_service, "record", _noop_audit)
    row = await inv.add_evidence(
        _DB(), case, kind=vocab.EV_ROPA_CONTEXT, source_system="consiva",
        summary="ROPA says this database holds contact data",
    )
    assert row.is_derived


# ── Timeline claims must cite evidence ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_confident_timeline_entry_must_cite_evidence(monkeypatch):
    """"We are fairly sure the database was read at 10:05" with nothing behind it is
    a guess wearing a timestamp."""
    case = make_case()
    monkeypatch.setattr(inv.incident_repository, "add_timeline_entries", _noop_entries)
    monkeypatch.setattr(inv.audit_service, "record", _noop_audit)

    with pytest.raises(EvidenceUnavailableError):
        await inv.add_timeline_entry(
            _DB(), case, occurred_at=NOW, event="Database read in bulk",
            confidence=vocab.PROBABLE, actor_user_id=USER,
        )


@pytest.mark.asyncio
async def test_a_possible_timeline_entry_needs_no_evidence(monkeypatch):
    """Recording a hypothesis is fine, as long as it is labelled as one."""
    case = make_case()
    monkeypatch.setattr(inv.incident_repository, "add_timeline_entries", _noop_entries)
    monkeypatch.setattr(inv.audit_service, "record", _noop_audit)
    row = await inv.add_timeline_entry(
        _DB(), case, occurred_at=NOW, event="Possible lateral movement",
        confidence=vocab.POSSIBLE,
    )
    assert row.confidence == vocab.POSSIBLE


@pytest.mark.asyncio
async def test_only_a_person_may_confirm_a_timeline_entry(monkeypatch):
    case = make_case()
    monkeypatch.setattr(inv.incident_repository, "add_timeline_entries", _noop_entries)
    with pytest.raises(InvalidIncidentError):
        await inv.add_timeline_entry(
            _DB(), case, occurred_at=NOW, event="Confirmed exfiltration",
            confidence=vocab.CONFIRMED, evidence_id=uuid.uuid4(), actor_user_id=None,
        )


@pytest.mark.asyncio
async def test_seeding_transcribes_evidence_rather_than_inferring(monkeypatch):
    """It places what evidence says happened. It does not correlate, infer causation,
    or fill gaps -- an attacker's movements between two log lines are exactly what a
    machine should not invent."""
    case = make_case()
    evidence = [
        SimpleNamespace(id=uuid.uuid4(), observed_at=NOW, is_derived=False,
                        kind=vocab.EV_AUTH_EVENT, summary="Login from 10.0.0.4",
                        source_system="okta"),
        SimpleNamespace(id=uuid.uuid4(), observed_at=NOW + timedelta(minutes=3),
                        is_derived=False, kind=vocab.EV_DATABASE_EVENT,
                        summary="SELECT over 1200 rows", source_system="pg"),
        # Undated: cannot be placed on a timeline at all.
        SimpleNamespace(id=uuid.uuid4(), observed_at=None, is_derived=False,
                        kind=vocab.EV_OBSERVATION, summary="Someone mentioned it",
                        source_system="slack"),
        # Derived: describes Consiva, not the world.
        SimpleNamespace(id=uuid.uuid4(), observed_at=NOW, is_derived=True,
                        kind=vocab.EV_ROPA_CONTEXT, summary="ROPA lookup",
                        source_system="consiva"),
    ]
    written = {}

    async def _list_ev(db, i, o): return evidence
    async def _list_tl(db, i, o): return []
    async def _add(db, org_id, rows): written["rows"] = rows

    monkeypatch.setattr(inv.incident_repository, "list_evidence", _list_ev)
    monkeypatch.setattr(inv.incident_repository, "list_timeline", _list_tl)
    monkeypatch.setattr(inv.incident_repository, "add_timeline_entries", _add)
    monkeypatch.setattr(inv.audit_service, "record", _noop_audit)

    rows = await inv.seed_timeline_from_evidence(_DB(), case)
    assert len(rows) == 2, "undated or derived evidence reached the timeline"
    assert all(r.confidence == vocab.PROBABLE for r in rows)
    assert all(r.evidence_id is not None for r in rows)


@pytest.mark.asyncio
async def test_seeding_twice_does_not_duplicate_entries(monkeypatch):
    case = make_case()
    ev = SimpleNamespace(id=uuid.uuid4(), observed_at=NOW, is_derived=False,
                         kind=vocab.EV_AUTH_EVENT, summary="Login", source_system="okta")
    already = SimpleNamespace(occurred_at=NOW, event="authentication event: Login")

    async def _list_ev(db, i, o): return [ev]
    async def _list_tl(db, i, o): return [already]

    monkeypatch.setattr(inv.incident_repository, "list_evidence", _list_ev)
    monkeypatch.setattr(inv.incident_repository, "list_timeline", _list_tl)
    monkeypatch.setattr(inv.incident_repository, "add_timeline_entries", _noop_entries)
    monkeypatch.setattr(inv.audit_service, "record", _noop_audit)

    assert await inv.seed_timeline_from_evidence(_DB(), case) == []


# ── §45: the cross-agent test ────────────────────────────────────────────────────

def test_agent_2s_stored_classification_is_reused_not_recomputed():
    """§45: Agent 2 discovered customers.email as Contact Data; Agent 4 gets an
    incident on that database and must reach the same conclusion. Using the STORED
    verdict means the two agents cannot disagree about the same column."""
    snapshot = {
        "tables": ["customers", "orders"],
        "columns": {
            "customers.id": "uuid", "customers.email": "text",
            "customers.phone": "text", "orders.order_id": "text",
            "orders.amount": "numeric",
        },
        "classifications": {
            "customers.email": "Contact Data",
            "customers.phone": "Contact Data",
        },
    }
    found = inv._categories_from_snapshot(snapshot)
    by_column = {(t, c): cat for t, c, cat in found}

    assert by_column[("customers", "email")] == "Contact Data"
    assert by_column[("customers", "phone")] == "Contact Data"
    # Operational columns are not personal data and must not appear.
    assert ("orders", "order_id") not in by_column
    assert ("orders", "amount") not in by_column
    assert ("customers", "id") not in by_column


def test_a_column_the_baseline_never_classified_is_classified_now():
    """Baselines predate classifier improvements. A column with no stored verdict gets
    one from Agent 2's rules rather than being silently skipped."""
    snapshot = {
        "tables": ["customers"],
        "columns": {"customers.email": "text"},
        "classifications": {},
    }
    found = inv._categories_from_snapshot(snapshot)
    assert found, "an unclassified column was dropped instead of being classified"
    assert found[0][2] == "Contact Data"


def test_a_stored_non_personal_label_is_not_mistaken_for_a_category():
    """The engine records non-personal outcomes as explicit labels. Those are not
    data categories and must not become affected-data rows."""
    snapshot = {
        "tables": ["jobs"],
        "columns": {"jobs.status": "text"},
        "classifications": {"jobs.status": "Not Personal Data (operational)"},
    }
    assert inv._categories_from_snapshot(snapshot) == []


@pytest.mark.parametrize(
    "snapshot",
    [{}, {"columns": None}, {"columns": "nonsense"}, {"columns": {}, "classifications": None},
     {"columns": {"nodot": "text"}}],
)
def test_an_unreadable_snapshot_yields_nothing_rather_than_raising(snapshot):
    """A parsing failure must not take an incident investigation down with it."""
    assert inv._categories_from_snapshot(snapshot) == []


@pytest.mark.asyncio
async def test_a_system_consiva_never_discovered_is_a_recorded_gap(monkeypatch):
    """Absence of a data map must be visible, not read as "no personal data here"."""
    case = make_case()
    systems = [SimpleNamespace(id=uuid.uuid4(), system_name="legacy-fileserver",
                               data_source_id=None)]

    async def _list_sys(db, i, o): return systems
    async def _replace(db, i, o, rows): return None

    monkeypatch.setattr(inv.incident_repository, "list_affected_systems", _list_sys)
    monkeypatch.setattr(inv.incident_repository, "replace_affected_data", _replace)
    monkeypatch.setattr(inv.audit_service, "record", _noop_audit)

    rows, notes = await inv.derive_affected_data(_DB(), case)
    assert rows == []
    assert any("not a source Consiva has discovered" in n for n in notes)


@pytest.mark.asyncio
async def test_derived_categories_are_only_possible_never_confirmed(monkeypatch):
    """ROPA says what a system CONTAINS, not what the incident TOUCHED. Collapsing
    those would let the agent report a breach of financial data because the affected
    database happens to have a payments table."""
    case = make_case()
    systems = [SimpleNamespace(id=uuid.uuid4(), system_name="crm",
                               data_source_id=uuid.uuid4())]
    snapshot = {
        "tables": ["customers"], "columns": {"customers.email": "text"},
        "classifications": {"customers.email": "Contact Data"},
    }
    captured = {}

    async def _list_sys(db, i, o): return systems
    async def _snapshot(db, o, name): return snapshot
    async def _replace(db, i, o, rows): captured["rows"] = rows

    monkeypatch.setattr(inv.incident_repository, "list_affected_systems", _list_sys)
    monkeypatch.setattr(inv.ropa_repository, "get_current_baseline_snapshot", _snapshot)
    monkeypatch.setattr(inv.incident_repository, "replace_affected_data", _replace)
    monkeypatch.setattr(inv.audit_service, "record", _noop_audit)

    rows, _ = await inv.derive_affected_data(_DB(), case)
    assert rows
    for row in rows:
        assert row.confidence == vocab.POSSIBLE
        assert row.derived_from == "ropa_metadata"


# ── Impact figures state their basis ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_counted_figure_must_say_how_it_was_counted(monkeypatch):
    """A bare number presented as exact is the claim most likely to be repeated to a
    regulator."""
    case = make_case()
    monkeypatch.setattr(inv.incident_repository, "upsert_affected_subjects", _noop_subjects)
    with pytest.raises(InvalidIncidentError):
        await inv.record_affected_subjects(
            _DB(), case, subject_group="Customer", record_count=11_500,
            count_basis="counted", basis_note=None,
        )


@pytest.mark.asyncio
async def test_a_number_without_a_basis_is_refused(monkeypatch):
    case = make_case()
    monkeypatch.setattr(inv.incident_repository, "upsert_affected_subjects", _noop_subjects)
    with pytest.raises(InvalidIncidentError):
        await inv.record_affected_subjects(
            _DB(), case, subject_group="Customer", record_count=None,
            count_basis="estimated",
        )


def test_one_unknown_group_makes_the_whole_total_unknown():
    """Summing what is known and presenting it as the total is how "at least 400"
    becomes "400" between a spreadsheet and a press release."""
    subjects = [
        SimpleNamespace(record_count=11_500, count_basis="counted"),
        SimpleNamespace(record_count=None, count_basis="unknown"),
    ]
    total, basis = inv.impact_total(subjects)
    assert total is None
    assert basis == "unknown"


def test_one_estimated_group_makes_the_whole_total_an_estimate():
    subjects = [
        SimpleNamespace(record_count=11_500, count_basis="counted"),
        SimpleNamespace(record_count=400, count_basis="estimated"),
    ]
    total, basis = inv.impact_total(subjects)
    assert total == 11_900
    assert basis == "estimated"


def test_all_counted_groups_give_a_counted_total():
    subjects = [
        SimpleNamespace(record_count=11_500, count_basis="counted"),
        SimpleNamespace(record_count=400, count_basis="counted"),
    ]
    assert inv.impact_total(subjects) == (11_900, "counted")


def test_no_subjects_at_all_is_unknown_not_zero():
    assert inv.impact_total([]) == (None, "unknown")


# ── helpers ──────────────────────────────────────────────────────────────────────

async def _noop_add(db, org_id, row):
    return row


async def _noop_entries(db, org_id, rows):
    return None


async def _noop_audit(db, **kw):
    return None


async def _noop_subjects(db, **kw):
    return SimpleNamespace(**kw)
