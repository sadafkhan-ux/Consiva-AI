"""The Consent console's contract with the backend, checked from both sides.

The frontend (frontend/src/api/client.ts and types.ts) and the FastAPI routes are written separately, so they can
drift without either side's own tests noticing: a renamed route returns 404 only when a user clicks, and a
response field the UI reads but the backend stopped sending renders as a blank. These tests read the frontend's
source and hold it against the real app:

  * every endpoint the console calls exists, with that HTTP method;
  * every field the UI's response types declare is a field the backend's response model actually has;
  * every console endpoint refuses a request without a token, and rejects malformed ids before any lookup.

No database and no network. Authentication is the real JWT path, signed with the test secret.
"""

import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api.v1.routes import consent_findings, consent_scans
from app.main import app

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api"
CLIENT_TS = (FRONTEND / "client.ts").read_text(encoding="utf-8")
TYPES_TS = (FRONTEND / "types.ts").read_text(encoding="utf-8")

client = TestClient(app)


def _headers() -> dict:
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "aud": "authenticated",
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _console_calls() -> list[tuple[str, str]]:
    """(METHOD, path template) for each consent call in client.ts, with ${x} turned into {x}."""
    calls = []
    for match in re.finditer(r"request\([`\"](/api/v1/consent/[^`\"]+)[`\"](?:,\s*\{\s*method:\s*\"(\w+)\")?", CLIENT_TS):
        path = re.sub(r"\$\{(\w+)\}", r"{\1}", match.group(1)).split("?")[0]   # the query string is not part of the route
        calls.append(((match.group(2) or "GET").upper(), path))
    return calls


def _backend_routes() -> set[tuple[str, str]]:
    """(METHOD, path) from the app's own OpenAPI document, which is what the framework actually serves."""
    routes = set()
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            routes.add((method.upper(), path))
    return routes


def _normalise(path: str) -> str:
    return re.sub(r"\{\w+\}", "{}", path)


def test_the_console_calls_the_sixteen_documented_endpoints():
    assert len(_console_calls()) == 17, _console_calls()


@pytest.mark.parametrize("method,path", _console_calls())
def test_every_endpoint_the_console_calls_exists_with_that_method(method, path):
    backend = {(m, _normalise(p)) for m, p in _backend_routes()}
    assert (method, _normalise(path)) in backend, f"{method} {path} is called by the UI but is not a backend route"


# ── response fields ─────────────────────────────────────────────────────────────────

def _ts_fields(interface: str) -> set[str]:
    match = re.search(rf"export interface {interface}\b[^{{]*\{{(.*?)\n\}}", TYPES_TS, re.DOTALL)
    assert match, f"{interface} not found in types.ts"
    body = match.group(1)
    names = set(re.findall(r"^\s{2}(\w+)\??:", body, re.MULTILINE))
    parent = re.search(rf"export interface {interface} extends (\w+)", TYPES_TS)
    if parent:
        names |= _ts_fields(parent.group(1))
    return names


def _pydantic(model_name: str):
    for module in (consent_scans, consent_findings):
        model = getattr(module, model_name, None)
        if model is not None:
            return model
    from app.api.v1 import schemas as pkg  # noqa: F401  (fall through to the schemas package)
    import importlib
    import pkgutil

    for info in pkgutil.iter_modules(pkg.__path__):
        model = getattr(importlib.import_module(f"app.api.v1.schemas.{info.name}"), model_name, None)
        if model is not None:
            return model
    raise AssertionError(f"{model_name} not found in the backend")


# TS interface -> backend response model. A UI field the model lacks would render blank.
PAIRS = [
    ("ScanResponse", "ScanResponse"),
    ("ScanStatusResponse", "ScanStatusResponse"),
    ("AgentRunResponse", "AgentRunResponse"),
    ("FindingResponse", "FindingResponse"),
    ("StageResponse", "StageResponse"),
    ("AuditLogResponse", "AuditLogResponse"),
    ("EvidencePageItem", "EvidencePageItem"),
    ("EvidenceCookieItem", "EvidenceCookieItem"),
    ("EvidenceTrackerItem", "EvidenceTrackerItem"),
    ("EvidenceConsentSignalItem", "EvidenceConsentSignalItem"),
    ("EvidencePolicyItem", "EvidencePolicyItem"),
    ("ScanEvidenceResponse", "ScanEvidenceResponse"),
    ("DpdpScoreResponse", "DpdpScoreResponse"),
    ("DpdpScoreBlock", "DpdpScoreBlock"),
    ("DpdpCategoryScore", "DpdpCategoryScore"),
    ("DpdpRequirementResult", "DpdpRequirementResult"),
    ("DpdpLedger", "DpdpLedger"),
    ("DpdpLedgerMetric", "DpdpLedgerMetric"),
    ("DpdpStatusRule", "DpdpStatusRule"),
    ("UsageTotals", "UsageTotals"),
    ("UsageCost", "UsageCost"),
    ("UsageStage", "UsageStage"),
    ("UsagePageRow", "UsagePageRow"),
    ("UsageSharedRow", "UsageSharedRow"),
    ("UsagePages", "UsagePages"),
    ("ScanUsage", "UsageResponse"),
    ("UsageHistoryItem", "UsageHistoryItem"),
    ("ScoreComparison", "ScoreComparisonResponse"),
]


@pytest.mark.parametrize("ts_name,model_name", PAIRS)
def test_every_field_the_ui_reads_is_a_field_the_backend_sends(ts_name, model_name):
    ui = _ts_fields(ts_name)
    backend = set(_pydantic(model_name).model_fields)
    missing = ui - backend
    assert not missing, f"{ts_name} declares {sorted(missing)} but {model_name} does not send them"


def test_the_page_attribution_added_for_trackers_reaches_the_ui_type():
    assert "page_url" in _ts_fields("EvidenceTrackerItem")
    assert "page_url" in _pydantic("EvidenceTrackerItem").model_fields


# ── auth and id validation on every console endpoint ───────────────────────────────

SCAN = str(uuid.uuid4())
ENDPOINTS = [
    ("POST", "/api/v1/consent/scans", {"url": "https://example.com", "authorized": True}),
    ("GET", f"/api/v1/consent/scans/{SCAN}", None),
    ("GET", f"/api/v1/consent/scans/{SCAN}/stages", None),
    ("GET", f"/api/v1/consent/scans/{SCAN}/evidence", None),
    ("GET", f"/api/v1/consent/scans/{SCAN}/dpdp-score", None),
    ("GET", f"/api/v1/consent/scans/{SCAN}/dpdp-score/compare", None),
    ("GET", f"/api/v1/consent/scans/{SCAN}/usage", None),
    ("GET", "/api/v1/consent/scans/usage/history", None),
    ("POST", f"/api/v1/consent/scans/{SCAN}/analyze", None),
    ("GET", f"/api/v1/consent/scans/{SCAN}/findings", None),
    ("GET", f"/api/v1/consent/scans/{SCAN}/audit", None),
    ("POST", f"/api/v1/consent/findings/{SCAN}/approve", {"reason": None}),
    ("POST", f"/api/v1/consent/findings/{SCAN}/reject", {"reason": "not a violation"}),
    ("POST", f"/api/v1/consent/findings/{SCAN}/edit", {"edited_payload": {}, "reason": "x"}),
]


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_every_console_endpoint_refuses_a_request_without_a_token(method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 401, f"{method} {path} answered {response.status_code} without a token"


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_every_console_endpoint_refuses_a_forged_token(method, path, body):
    forged = jwt.encode({"sub": "x", "org_id": "y", "aud": "authenticated",
                         "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())}, "not-the-secret", algorithm="HS256")
    response = client.request(method, path, json=body, headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


@pytest.mark.parametrize("path", [
    "/api/v1/consent/scans/not-a-uuid",
    "/api/v1/consent/scans/not-a-uuid/stages",
    "/api/v1/consent/scans/not-a-uuid/evidence",
    "/api/v1/consent/scans/not-a-uuid/dpdp-score",
    "/api/v1/consent/scans/not-a-uuid/dpdp-score/compare",
    "/api/v1/consent/scans/not-a-uuid/findings",
    "/api/v1/consent/scans/not-a-uuid/audit",
    "/api/v1/consent/scans/not-a-uuid/usage",
])
def test_a_malformed_scan_id_is_a_validation_error_not_a_server_error(path):
    assert client.get(path, headers=_headers()).status_code == 422


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/", "http://192.168.1.1/", "file:///etc/passwd", "ftp://example.com/",
    "http://[::1]/", "javascript:alert(1)",
])
async def test_scan_targets_that_are_internal_or_not_http_never_pass_the_safety_check(url):
    from app.scanner.url_safety import assert_safe_url

    with pytest.raises(Exception):  # noqa: B017 -- which typed error depends on the reason; that it refuses is the contract
        await assert_safe_url(url)


# ---------------------------------------------------------------------------------------------------------------------------
# GET /api/v1/consent/scans: the recent scans the console lists so one can be reopened.
# ---------------------------------------------------------------------------------------------------------------------------

def test_the_scan_list_is_scoped_to_the_callers_organisation_and_clamps_the_limit(monkeypatch):
    from types import SimpleNamespace

    from app.db.session import get_db

    seen = {}

    async def fake_list(db, org_id, limit=20):
        seen["org"], seen["limit"] = org_id, limit
        now = datetime.now(UTC)
        return [SimpleNamespace(id=uuid.uuid4(), url="https://a.test/", status="completed", created_at=now, completed_at=now),
                SimpleNamespace(id=uuid.uuid4(), url="https://b.test/", status="failed", created_at=now, completed_at=None)]

    async def fake_db():
        yield None

    monkeypatch.setattr(consent_scans.scan_repository, "list_recent_scans", fake_list)
    app.dependency_overrides[get_db] = fake_db
    try:
        headers = _headers()
        org = jwt.decode(headers["Authorization"].split()[1], os.environ["SUPABASE_JWT_SECRET"], algorithms=["HS256"], audience="authenticated")["org_id"]
        resp = client.get("/api/v1/consent/scans?limit=500", headers=headers)
        assert resp.status_code == 200
        rows = resp.json()
        assert [r["url"] for r in rows] == ["https://a.test/", "https://b.test/"]
        assert set(rows[0]) == {"id", "url", "status", "created_at", "completed_at"}
        assert rows[1]["completed_at"] is None
        assert str(seen["org"]) == org            # only the caller's own organisation
        assert seen["limit"] == 50                # a huge request is clamped
        client.get("/api/v1/consent/scans?limit=0", headers=headers)
        assert seen["limit"] == 1
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_the_scan_list_query_filters_by_organisation_and_orders_newest_first():
    import asyncio
    from types import SimpleNamespace

    from app.db.repositories import scan_repository

    captured = {}

    class Db:
        async def execute(self, stmt):
            captured["sql"] = str(stmt.compile())
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    assert asyncio.run(scan_repository.list_recent_scans(Db(), uuid.uuid4(), 5)) == []
    sql = captured["sql"].lower()
    assert "org_id" in sql and "order by" in sql and "created_at desc" in sql and "limit" in sql


def test_business_sector_is_validated_on_the_score_endpoint():
    sid = uuid.uuid4()
    resp = client.get(f"/api/v1/consent/scans/{sid}/dpdp-score?business_sector=nonsense", headers=_headers())
    assert resp.status_code == 422
    resp = client.get(f"/api/v1/consent/scans/{sid}/dpdp-score?business_sector=general", headers=_headers())
    assert resp.status_code == 422                     # "general" means undeclared: it is not an accepted declaration


def test_business_sector_accepts_each_declarable_sector_before_any_lookup(monkeypatch):
    from app.db.session import get_db
    from app.scoring.sector import SECTORS

    async def no_scan(db, scan_id, org_id):
        return None

    async def fake_db():
        yield None

    monkeypatch.setattr(consent_scans.scan_repository, "get_scan", no_scan)
    app.dependency_overrides[get_db] = fake_db
    try:
        for key in (k for k in SECTORS if k != "general"):
            resp = client.get(f"/api/v1/consent/scans/{uuid.uuid4()}/dpdp-score?business_sector={key}", headers=_headers())
            assert resp.status_code == 404, (key, resp.status_code)   # passed validation, then the scan was (rightly) not found
    finally:
        app.dependency_overrides.pop(get_db, None)
