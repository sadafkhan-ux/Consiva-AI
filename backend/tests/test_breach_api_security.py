"""Agent 4 API security (§31, §37, §38, §50).

Route-level properties, checked against the real route table rather than a list
maintained by hand -- a route added later is picked up automatically, which is the
only version of this test that stays true.

Incident evidence is the most sensitive data in the platform: account names, attack
paths, occasionally a credential somebody pasted into a ticket. So on top of the
tenancy properties Agents 2 and 3 are held to, this file checks the two things
specific to Agent 4 -- that detail is not handed out in bulk, and that no response
model has a field shaped like a secret.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.agents.breach.schemas import incident as vocab
from app.main import app

client = TestClient(app)

INCIDENT_PATHS = sorted(p for p in app.openapi()["paths"] if "/incidents" in p)


def _concrete(path: str) -> str:
    """Fill path params with a syntactically valid UUID so the request reaches the auth
    dependency rather than 404ing on parsing."""
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000001", path)


def _methods(path: str) -> list[str]:
    return [m.upper() for m in app.openapi()["paths"][path]]


def _incident_schemas() -> dict:
    """The request/response models this agent contributes, by name."""
    spec = app.openapi()
    wanted = set()
    for path in INCIDENT_PATHS:
        for operation in spec["paths"][path].values():
            body = operation.get("requestBody", {})
            for content in body.get("content", {}).values():
                ref = content.get("schema", {}).get("$ref", "")
                if ref:
                    wanted.add(ref.rsplit("/", 1)[-1])
    return {name: spec["components"]["schemas"][name] for name in wanted}


# ── Authentication ───────────────────────────────────────────────────────────────

def test_there_are_incident_routes_to_test():
    assert len(INCIDENT_PATHS) >= 15, "route discovery found nothing; the rest is vacuous"


@pytest.mark.parametrize("path", INCIDENT_PATHS)
def test_every_incident_route_requires_a_token(path):
    for method in _methods(path):
        response = client.request(method, _concrete(path), json={})
        assert response.status_code == 401, (
            f"{method} {path} returned {response.status_code} without a token"
        )


@pytest.mark.parametrize("path", INCIDENT_PATHS)
def test_a_garbage_token_is_rejected(path):
    for method in _methods(path):
        response = client.request(
            method, _concrete(path), json={},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 401, f"{method} {path} accepted a garbage token"


def test_an_unsigned_token_is_rejected():
    """alg=none is the classic JWT bypass, and an incident list is exactly the kind of
    thing it would be used against."""
    forged = (
        "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
        "eyJzdWIiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDEiLCJvcmdfaWQiOiJhbnkifQ."
    )
    response = client.get(
        "/api/v1/incidents", headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 401


# ── No caller-supplied tenancy (§37) ─────────────────────────────────────────────

def test_no_incident_route_accepts_an_org_id_parameter():
    spec = app.openapi()
    offenders = []
    for path in INCIDENT_PATHS:
        for method, operation in spec["paths"][path].items():
            for parameter in operation.get("parameters", []):
                if "org" in parameter["name"].lower():
                    offenders.append(f"{method.upper()} {path} -> {parameter['name']}")
    assert not offenders, f"routes accept an org identifier from the caller: {offenders}"


def test_no_request_body_accepts_an_org_id():
    offenders = [
        f"{name}.{field}"
        for name, schema in _incident_schemas().items()
        for field in schema.get("properties", {})
        if "org" in field.lower()
    ]
    assert not offenders, f"request bodies accept tenancy from the caller: {offenders}"


def test_intake_cannot_set_status_reference_or_findings():
    """Everything the state machine and the confidence rules own.

    A caller who could POST `breach_confirmed: confirmed` at intake would bypass the
    one guard that makes 'confirmed' mean a person said so.
    """
    schema = app.openapi()["components"]["schemas"]["IncidentCreate"]
    forbidden = {
        "status", "reference", "due_at", "sla_breached", "error_code",
        "breach_confirmed", "personal_data_involved", "severity", "severity_score",
        "closure_summary", "closed_at",
    }
    assert not (forbidden & set(schema["properties"]))


# ── Input validation ─────────────────────────────────────────────────────────────

def test_free_text_fields_are_length_bounded():
    """An unbounded text field on an unauthenticated-adjacent intake route is a cheap
    way to fill a tenant's disk."""
    unbounded = []
    for name, schema in _incident_schemas().items():
        for field, spec in schema.get("properties", {}).items():
            variants = spec.get("anyOf", [spec])
            # A `format` means it is a uuid or a timestamp, which are bounded by their
            # own parsing -- only genuinely free text needs a cap.
            strings = [
                v for v in variants
                if v.get("type") == "string" and "format" not in v and "enum" not in v
            ]
            if strings and not any("maxLength" in v for v in strings):
                # Enumerated vocabularies are bounded by the vocabulary itself.
                if field in ("source", "kind", "system_kind", "action_kind",
                             "decision", "audience", "confidence", "count_basis",
                             "field", "to_status", "initial_severity", "error_code"):
                    continue
                unbounded.append(f"{name}.{field}")
    assert not unbounded, f"unbounded text fields: {unbounded}"


@pytest.mark.parametrize(
    ("model", "field", "bad"),
    [
        ("IncidentCreate", "source", "totally_made_up_source"),
        ("EvidenceIn", "kind", "vibes"),
        ("AffectedSystemIn", "system_kind", "mainframe_of_legend"),
        ("ActionIn", "action_kind", "hack_back"),
        ("DecisionIn", "decision", "definitely_approved"),
        ("CommunicationIn", "audience", "the_press_and_everyone"),
        ("FindingIn", "confidence", "pretty_sure"),
    ],
)
def test_closed_vocabularies_are_enforced_at_the_edge(model, field, bad):
    """§8: closed vocabularies, not free text. Checked on the Pydantic model itself so
    the rejection happens before anything reaches a service."""
    import app.api.v1.routes.incidents as routes

    cls = getattr(routes, model)
    payload = _minimal_payload(model) | {field: bad}
    with pytest.raises(ValueError):
        cls(**payload)


def _minimal_payload(model: str) -> dict:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return {
        "IncidentCreate": {
            "title": "something happened", "description": "a longer description here",
            "source": vocab.SOURCE_MANUAL, "detected_at": now,
        },
        "EvidenceIn": {
            "kind": vocab.EV_ACCESS_LOG, "source_system": "siem", "summary": "a line",
        },
        "AffectedSystemIn": {
            "system_name": "crm", "system_kind": vocab.SYS_DATABASE,
        },
        "ActionIn": {
            "action_kind": vocab.ACT_NOTIFY_INTERNAL, "title": "tell someone",
            "rationale": "because", "expected_result": "they know",
        },
        "DecisionIn": {"decision": vocab.DECISION_APPROVE},
        "CommunicationIn": {"audience": vocab.COMM_INTERNAL, "subject": "heads up"},
        "FindingIn": {"field": "breach_confirmed", "confidence": vocab.PROBABLE},
    }[model]


def test_a_finding_can_only_target_the_two_substantive_questions():
    import app.api.v1.routes.incidents as routes

    with pytest.raises(ValueError):
        routes.FindingIn(field="status", confidence=vocab.CONFIRMED)
    for good in ("personal_data_involved", "breach_confirmed"):
        assert routes.FindingIn(field=good, confidence=vocab.CONFIRMED).field == good


# ── Secrets and sensitive detail (§38) ───────────────────────────────────────────

def test_no_incident_schema_exposes_a_credential_shaped_field():
    """Incident evidence often arrives holding a token somebody pasted into a ticket.
    Detail is redacted on the way in; no schema should even have a field named for
    one on the way out."""
    leaky = {"password", "secret", "credential", "api_key", "dsn", "connection_string"}
    offenders = [
        f"{name}.{field}"
        for name, schema in _incident_schemas().items()
        for field in schema.get("properties", {})
        if any(bad in field.lower() for bad in leaky)
    ]
    assert not offenders, f"schemas expose credential-shaped fields: {offenders}"


def test_the_evidence_list_route_does_not_return_detail():
    """A list view is the one most likely to be left open on a shared screen, and
    detail is the part carrying account names and attack paths. Reading the source
    because the response is a bare dict with no declared model."""
    import inspect

    import app.api.v1.routes.incidents as routes

    body = inspect.getsource(routes.list_evidence)
    assert '"has_detail"' in body, "the list should say detail exists"
    assert '"detail":' not in body, "the evidence list returns detail in bulk"


def test_reading_one_piece_of_evidence_is_audited():
    """Who looked at incident evidence is itself something an investigation may need
    to answer."""
    import inspect

    import app.api.v1.routes.incidents as routes

    body = inspect.getsource(routes.get_evidence_detail)
    assert "audit_service.record" in body
    assert "evidence_viewed" in body


# ── No fake execution (§50) ──────────────────────────────────────────────────────

def test_no_route_claims_to_perform_containment():
    """Consiva does not disable accounts, revoke keys or isolate services. The API
    surface must not imply it does: the only containment endpoint records what a
    person did."""
    performing = [
        p for p in INCIDENT_PATHS
        if re.search(r"/(execute|disable|revoke|isolate|contain|block)\b", p)
    ]
    assert not performing, f"routes that imply Consiva acts: {performing}"
    assert any(p.endswith("/attest") for p in INCIDENT_PATHS), (
        "containment should be recorded through an attestation endpoint"
    )


def test_attestation_requires_a_name_and_a_description():
    """'Somebody did it' with no name and no description records nothing."""
    import app.api.v1.routes.incidents as routes

    with pytest.raises(ValueError):
        routes.AttestationIn(performed_by="", attestation="disabled the account")
    with pytest.raises(ValueError):
        routes.AttestationIn(performed_by="alice", attestation="")


def test_there_is_no_send_endpoint_only_a_record_that_a_person_sent():
    """Consiva has no outbound provider. An endpoint called 'send' would be a button
    that pretends to do something."""
    assert not [p for p in INCIDENT_PATHS if p.endswith("/send")]
    assert any(p.endswith("/sent") for p in INCIDENT_PATHS)


def test_the_analysis_route_queues_analysis_and_nothing_else():
    """Containment is deliberately not a job type -- a queued 'disable account' job
    would be exactly the fake execution §50 forbids."""
    import inspect

    import app.api.v1.routes.incidents as routes

    body = inspect.getsource(routes.start_analysis)
    assert 'job_type="incident_analysis"' in body
    assert "contain" not in body.lower().replace("containment is deliberately", "")


def test_the_worker_knows_incident_analysis_and_no_containment_job():
    import inspect

    from app.jobs import worker

    body = inspect.getsource(worker._process_one)
    assert '"incident_analysis"' in body
    assert "incident_contain" not in body
    assert "incident_execute" not in body


def test_the_job_type_constraint_was_widened_for_agent_4():
    """The DSR build found `agent_jobs.job_type` silently rejecting two agents' jobs.
    This checks Agent 4 does not repeat it."""
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    text = "\n".join(p.read_text(encoding="utf-8") for p in migrations.glob("*.sql"))
    assert "'incident_analysis'" in text, (
        "no migration adds incident_analysis to agent_jobs.job_type; "
        "every queued analysis will fail with a CheckViolationError"
    )
