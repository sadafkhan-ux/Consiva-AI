"""Request-validation tests only — no DB, no LLM. FastAPI validates the request body
against the Pydantic model before the route handler runs, so a malformed body never
reaches a repository/service call. Uses a real JWT signed with the test SUPABASE_JWT_SECRET
(see conftest.py) so requests pass auth and reach body validation.

Deliberately does NOT use `with TestClient(app) as client:` — that form runs the app's
lifespan, which builds a real Postgres-backed LangGraph checkpointer (app/main.py) and
would hang/fail against the fake DATABASE_URL these tests run with. Plain `TestClient(app)`
skips lifespan and is enough for pure request-validation checks.
"""

import os
import uuid

import jwt
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _auth_headers() -> dict:
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "aud": "authenticated"},
        os.environ["SUPABASE_JWT_SECRET"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_health_check_needs_no_auth():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_scan_missing_url_is_rejected():
    response = client.post("/api/v1/consent/scans", json={"authorized": True}, headers=_auth_headers())
    assert response.status_code == 422


def test_create_scan_wrong_type_is_rejected():
    response = client.post("/api/v1/consent/scans", json={"url": 12345}, headers=_auth_headers())
    assert response.status_code == 422


def test_create_scan_without_auth_header_is_rejected():
    response = client.post("/api/v1/consent/scans", json={"url": "https://example.com", "authorized": True})
    assert response.status_code == 401


def test_create_scan_with_garbage_token_is_rejected():
    response = client.post(
        "/api/v1/consent/scans",
        json={"url": "https://example.com", "authorized": True},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401


def test_get_scan_rejects_non_uuid_path_param():
    response = client.get("/api/v1/consent/scans/not-a-uuid", headers=_auth_headers())
    assert response.status_code == 422


def test_edit_finding_missing_edited_payload_is_rejected():
    response = client.post(
        f"/api/v1/consent/findings/{uuid.uuid4()}/edit",
        json={"reason": "typo"},
        headers=_auth_headers(),
    )
    assert response.status_code == 422
