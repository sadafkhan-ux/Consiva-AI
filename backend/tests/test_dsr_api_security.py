"""DSR API security (prompt §31, §37).

Route-level properties: every endpoint is authenticated, no endpoint accepts an
org_id from the caller, and a case belonging to another organization is
indistinguishable from one that does not exist.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

DSR_PATHS = sorted(p for p in app.openapi()["paths"] if "/dsr" in p)


def _concrete(path: str) -> str:
    """Fill path params with a syntactically valid UUID so the route matches and the
    request reaches the auth dependency rather than 404ing on parsing."""
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000001", path)


def _methods(path: str) -> list[str]:
    return [m.upper() for m in app.openapi()["paths"][path]]


# ── Authentication ───────────────────────────────────────────────────────────────

def test_there_are_dsr_routes_to_test():
    assert len(DSR_PATHS) >= 10, "route discovery found nothing; the rest is vacuous"


@pytest.mark.parametrize("path", DSR_PATHS)
def test_every_dsr_route_requires_a_token(path):
    for method in _methods(path):
        response = client.request(method, _concrete(path), json={})
        assert response.status_code == 401, (
            f"{method} {path} returned {response.status_code} without a token"
        )


@pytest.mark.parametrize("path", DSR_PATHS)
def test_a_garbage_token_is_rejected(path):
    for method in _methods(path):
        response = client.request(
            method, _concrete(path), json={},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 401, f"{method} {path} accepted a garbage token"


def test_an_unsigned_token_is_rejected():
    """alg=none is the classic JWT bypass."""
    forged = (
        "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
        "eyJzdWIiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDEiLCJvcmdfaWQiOiJhbnkifQ."
    )
    response = client.get(
        "/api/v1/dsr/requests", headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 401


# ── No caller-supplied tenancy ───────────────────────────────────────────────────

def test_no_dsr_route_accepts_an_org_id_parameter():
    """org_id comes from the verified token and nowhere else. A route that accepted
    one as a path/query parameter would let any authenticated user read another
    tenant's cases (§37)."""
    spec = app.openapi()
    offenders = []
    for path in DSR_PATHS:
        for method, operation in spec["paths"][path].items():
            for parameter in operation.get("parameters", []):
                if "org" in parameter["name"].lower():
                    offenders.append(f"{method.upper()} {path} -> {parameter['name']}")
    assert not offenders, f"routes accept an org identifier from the caller: {offenders}"


def test_the_create_body_does_not_accept_an_org_id():
    schema = app.openapi()["components"]["schemas"]["DsrCreate"]
    assert not [f for f in schema["properties"] if "org" in f.lower()]


def test_the_create_body_does_not_accept_a_status_or_case_reference():
    """A caller must not be able to open a case that is already 'approved', nor pick
    its reference -- both would bypass the state machine at intake."""
    schema = app.openapi()["components"]["schemas"]["DsrCreate"]
    forbidden = {"status", "reference", "due_at", "sla_breached", "error_code"}
    assert not (forbidden & set(schema["properties"]))


# ── Input validation ─────────────────────────────────────────────────────────────

def test_request_text_is_length_bounded():
    """An unbounded text field is a cheap way to fill a tenant's disk."""
    schema = app.openapi()["components"]["schemas"]["DsrCreate"]
    assert schema["properties"]["raw_request"]["maxLength"] == 5000
    assert schema["properties"]["raw_request"]["minLength"] == 1


def test_decision_is_constrained_to_the_known_vocabulary():
    from app.agents.dsr.services import approval_service
    from app.api.v1.routes.dsr import Decision

    with pytest.raises(ValueError):
        Decision(decision="definitely_approved")
    for valid in approval_service.DECISIONS:
        assert Decision(decision=valid).decision == valid


def test_email_shape_is_checked():
    from app.api.v1.routes.dsr import DsrCreate

    with pytest.raises(ValueError):
        DsrCreate(raw_request="delete my data", requester_email="not-an-email")
    assert DsrCreate(raw_request="x", requester_email="a@b.com").requester_email == "a@b.com"


# ── Secrets never reach a response ───────────────────────────────────────────────

def test_no_dsr_response_schema_exposes_a_credential():
    """A DSR source's read and write passwords live in the environment. No response
    model should even have a field shaped like one."""
    spec = app.openapi()
    leaky = {"password", "secret", "credential", "token", "dsn", "connection_string"}
    offenders = []
    for name, schema in spec["components"]["schemas"].items():
        if not name.lower().startswith(("dsr", "decision", "verify", "classify")):
            continue
        for field in schema.get("properties", {}):
            if any(bad in field.lower() for bad in leaky):
                offenders.append(f"{name}.{field}")
    # `challenge` is deliberately returned once, to the authenticated operator, and
    # is not a stored credential -- it exists only as a hash on the row.
    assert not offenders, f"response schemas expose credential-shaped fields: {offenders}"
