"""The domain-first setup wizard, the outcome contract and revocation.

Synthetic data and in-memory fakes only: no real DNS, no real HTTP, no customer database.
The properties held here are the ones that matter commercially and for safety:

  * a domain is validated and proven BEFORE anything is discovered
  * proving a domain authorises nothing, and writes nothing to the Consent agent's tables
  * discovery never invents or calls an endpoint
  * a stale verification is not trusted
  * a source is revoked completely, including its write credential
  * a portal may show "resolved" only for a verified outcome, and disagreements are reported
    rather than overwritten
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.agents.dsr.errors import DsrNotFoundError, SourceNotAuthorizedError
from app.agents.dsr.schemas import case
from app.agents.dsr.services import approval_service, case_service, outcome_service, setup_service
from app.api.v1.routes import dsr as dsr_routes
from app.core.security import CurrentUser
from app.db.repositories import dsr_repository, ropa_repository
from app.services import audit_service, dsr_run_service

from tests.test_dsr_end_to_end import (  # noqa: F401 -- `world` is a pytest fixture
    ORG, USER, FakeConnector, FakeDB, _open_case, _plan, _search, _use_connector, _verify,
    match, world,
)
from tests.test_dsr_execution_closure_audit import _fake_purge, _run_worker_execution_against

ADMIN = CurrentUser(user_id=str(USER), org_id=str(ORG), role="admin")
MEMBER = CurrentUser(user_id=str(USER), org_id=str(ORG), role="member")
NOW = datetime(2026, 10, 15, tzinfo=UTC)


# ── Domain validation ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("example.com", "example.com"),
    ("  Example.COM  ", "example.com"),
    ("www.example.com", "www.example.com"),
    ("https://example.com", "example.com"),
    ("HTTPS://Example.COM:443/", "example.com"),
    ("http://example.com/path?x=1#frag", "example.com"),
    ("shop.example.co.in", "shop.example.co.in"),
    ("münchen.de", "xn--mnchen-3ya.de"),
    ("example.com.", "example.com"),
])
def test_valid_domains_are_normalised(raw, expected):
    assert setup_service.normalize_domain(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "localhost", "intranet", "10.0.0.5", "127.0.0.1", "[::1]", "169.254.169.254",
    "db.internal", "printer.local", "x.test", "exa mple.com", "example..com", "-bad.example.com",
    "ftp://example.com", "javascript:alert(1)", "https://user:pw@example.com",
    "example.com:8080", "http://", "a" * 300 + ".com", "example.c", "example.123",
])
def test_invalid_or_internal_domains_are_refused(raw):
    with pytest.raises(setup_service.InvalidDomainError):
        setup_service.normalize_domain(raw)


# ── Ownership ────────────────────────────────────────────────────────────────────

async def _dns_yes(domain, token):
    return True


async def _dns_no(domain, token):
    return False


@pytest.mark.asyncio
async def test_a_fresh_existing_verification_is_reused_without_a_dns_lookup():
    async def must_not_be_called(domain, token):
        raise AssertionError("no DNS lookup needed for a fresh verification")

    result = await setup_service.establish_ownership(
        org_id=ORG, domain="example.com", verified_at=NOW - timedelta(days=30),
        check_dns=True, now=NOW, dns_check=must_not_be_called,
    )
    assert result.proven and result.method == "existing_verification" and not result.stale


@pytest.mark.asyncio
async def test_a_stale_verification_is_not_trusted_and_must_be_reproven():
    old = NOW - timedelta(days=setup_service.DOMAIN_VERIFICATION_MAX_AGE_DAYS + 1)
    without_dns = await setup_service.establish_ownership(
        org_id=ORG, domain="example.com", verified_at=old, check_dns=False, now=NOW, dns_check=_dns_yes,
    )
    assert not without_dns.proven and without_dns.stale
    assert "re-proven" in without_dns.detail

    reproven = await setup_service.establish_ownership(
        org_id=ORG, domain="example.com", verified_at=old, check_dns=True, now=NOW, dns_check=_dns_yes,
    )
    assert reproven.proven and reproven.method == "dns_txt"


@pytest.mark.asyncio
async def test_a_website_row_with_no_verification_stamp_proves_nothing():
    result = await setup_service.establish_ownership(
        org_id=ORG, domain="example.com", verified_at=None, check_dns=False, now=NOW, dns_check=_dns_yes,
    )
    assert not result.proven and not result.stale


@pytest.mark.asyncio
async def test_a_missing_dns_record_is_not_proof():
    result = await setup_service.establish_ownership(
        org_id=ORG, domain="example.com", verified_at=None, check_dns=True, now=NOW, dns_check=_dns_no,
    )
    assert not result.proven and "not found" in result.detail


def test_the_txt_record_is_org_specific_and_matches_the_existing_scheme():
    mine = setup_service.txt_record_for(ORG, "example.com")
    other = setup_service.txt_record_for(uuid.uuid4(), "example.com")
    assert mine["value"].startswith("consiva-verify=") and mine["type"] == "TXT"
    assert mine["value"] != other["value"], "another org must not be able to reuse this record"
    assert setup_service.txt_record_for(ORG, "EXAMPLE.com")["value"] == mine["value"]


# ── Manifest discovery: never invents, never calls what it finds ────────────────

def _client_for(handler):
    return lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False,
    )


@pytest.fixture
def safe_host(monkeypatch):
    async def ok(host):
        return ["93.184.216.34"]
    monkeypatch.setattr(setup_service, "assert_safe_host", ok)


@pytest.mark.asyncio
async def test_a_published_manifest_is_read_as_an_unverified_claim(safe_host):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={
            "version": 1,
            "connectors": [
                {"type": "postgres", "base_url": "https://db-gateway.example.com", "description": "x"},
                {"type": "rest", "base_url": "https://api.example.com/dsr",
                 "docs_url": "https://docs.example.com/dsr"},
            ],
        })

    result = await setup_service.fetch_manifest("example.com", client_factory=_client_for(handler))

    assert result["found"] and result["declared_by_customer"] is True
    by_type = {c["type"]: c for c in result["connectors"]}
    assert by_type["postgres"]["supported_by_consiva"] is True
    assert by_type["rest"]["supported_by_consiva"] is False, "listed is not supported"
    assert seen == ["https://example.com/.well-known/consiva-dsr.json"], (
        "only the manifest itself may be fetched -- never an endpoint it lists"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [(404, "not_published"), (500, "http_500"), (302, "redirect_not_followed")])
async def test_a_missing_or_unusable_manifest_is_reported_precisely(safe_host, status, reason):
    result = await setup_service.fetch_manifest(
        "example.com", client_factory=_client_for(lambda r: httpx.Response(status, headers={"location": "https://evil"})),
    )
    assert result == {"found": False, "reason": reason, "declared_by_customer": True}


@pytest.mark.asyncio
async def test_an_invalid_or_oversized_manifest_is_rejected(safe_host):
    bad = await setup_service.fetch_manifest(
        "example.com", client_factory=_client_for(lambda r: httpx.Response(200, json={"version": 9})),
    )
    assert bad["found"] is False and bad["reason"] == "invalid_manifest"

    insecure = await setup_service.fetch_manifest(
        "example.com", client_factory=_client_for(lambda r: httpx.Response(200, json={
            "version": 1, "connectors": [{"type": "rest", "base_url": "http://api.example.com"}],
        })),
    )
    assert insecure["reason"] == "invalid_manifest", "plain http endpoints may not be declared"

    big = await setup_service.fetch_manifest(
        "example.com",
        client_factory=_client_for(lambda r: httpx.Response(200, content=b"x" * (setup_service.MANIFEST_MAX_BYTES + 1))),
    )
    assert big["reason"] == "too_large"


@pytest.mark.asyncio
async def test_a_host_that_fails_the_ssrf_guard_is_never_fetched(monkeypatch):
    from app.core.exceptions import ScanAuthorizationError

    async def blocked(host):
        raise ScanAuthorizationError("private address")

    def must_not_connect():
        raise AssertionError("an unsafe host must not be fetched")

    monkeypatch.setattr(setup_service, "assert_safe_host", blocked)
    result = await setup_service.fetch_manifest("example.com", client_factory=must_not_connect)
    assert result["found"] is False and result["reason"] == "host_not_reachable_safely"


# ── Source readiness ─────────────────────────────────────────────────────────────

def _src(**kw):
    return SimpleNamespace(
        id=kw.get("id", uuid.uuid4()), name=kw.get("name", "crm"), connector=kw.get("connector", "postgres"),
        enabled=kw.get("enabled", True), credential_ref=kw.get("credential_ref", "CRM_READ"),
        credential_ciphertext=kw.get("credential_ciphertext"),
    )


def _auth(source, **kw):
    return SimpleNamespace(
        data_source_id=source.id, enabled=kw.get("enabled", True),
        allow_execution=kw.get("allow_execution", False),
        write_credential_ciphertext=kw.get("write_credential_ciphertext"),
    )


@pytest.mark.parametrize("kind,expected", [
    ("fresh", "ready_to_authorize"),
    ("sqlserver", "connector_not_supported"),
    ("no_credential", "needs_credential"),
    ("disabled", "disabled"),
    ("authorized", "authorized"),
    ("revoked", "revoked"),
])
def test_each_source_is_classified_with_a_next_step(kind, expected):
    source = {
        "fresh": _src(), "sqlserver": _src(connector="sqlserver"),
        "no_credential": _src(credential_ref=None), "disabled": _src(enabled=False),
        "authorized": _src(), "revoked": _src(),
    }[kind]
    auths = []
    if kind == "authorized":
        auths = [_auth(source)]
    if kind == "revoked":
        auths = [_auth(source, enabled=False)]

    [row] = setup_service.assess_sources([source], auths)

    assert row["readiness"] == expected and row["next_step"]
    assert row["allow_execution"] is False, "execution is never on unless explicitly authorised"


def test_the_catalogue_never_claims_a_path_that_does_not_exist():
    by_id = {p["id"]: p for p in setup_service.INTEGRATION_PATHS}
    assert by_id["postgres_direct"]["status"] == "supported"
    assert by_id["evidence_push"]["status"] == "supported"
    for unavailable in ("rest_connector", "other_databases"):
        assert by_id[unavailable]["status"] == "not_available"
        assert by_id[unavailable]["alternative"] == "evidence_push"


# ── Routes ───────────────────────────────────────────────────────────────────────

class _Db:
    async def flush(self):
        return None

    async def commit(self):
        return None


@pytest.fixture
def wizard(monkeypatch):
    state = SimpleNamespace(
        website=None, audits=[], sources=[], authorizations=[], dns=_dns_no,
        manifest_calls=0, authorization_writes=0,
    )

    async def find_website(db, org_id, domain):
        return state.website

    async def list_sites(db, org_id):
        return [state.website] if state.website else []

    async def list_sources(db, org_id):
        return state.sources

    async def list_auths(db, org_id):
        return state.authorizations

    async def audit(db, **kw):
        state.audits.append(kw)

    async def upsert(*a, **kw):
        state.authorization_writes += 1

    async def fetch_manifest(domain, **kw):
        state.manifest_calls += 1
        return {"found": False, "reason": "not_published", "declared_by_customer": True}

    async def txt(domain, token):
        return await state.dns(domain, token)

    monkeypatch.setattr(dsr_repository, "find_website", find_website)
    monkeypatch.setattr(dsr_repository, "list_org_websites", list_sites)
    monkeypatch.setattr(dsr_repository, "list_all_source_authorizations", list_auths)
    monkeypatch.setattr(dsr_repository, "upsert_source_authorization", upsert)
    monkeypatch.setattr(ropa_repository, "list_data_sources", list_sources)
    monkeypatch.setattr(audit_service, "record", audit)
    monkeypatch.setattr(setup_service, "fetch_manifest", fetch_manifest)
    monkeypatch.setattr(setup_service, "txt_has_token", txt)
    monkeypatch.setattr(
        setup_service, "establish_ownership",
        _wrap_establish(setup_service.establish_ownership, txt),
    )
    return state


def _wrap_establish(real, txt):
    async def wrapped(**kw):
        kw.setdefault("dns_check", txt)
        return await real(**kw)
    return wrapped


@pytest.mark.asyncio
async def test_checking_a_domain_returns_the_record_to_publish_and_grants_nothing(wizard):
    result = await dsr_routes.setup_check_domain(
        payload=dsr_routes.DomainIn(domain="https://Example.com/"), user=ADMIN, db=_Db(),
    )
    assert result["domain"] == "example.com"
    assert result["ownership"]["proven"] is False
    assert result["txt_record"]["value"].startswith("consiva-verify=")
    assert result["grants_access"] is False
    assert wizard.authorization_writes == 0, "proving a domain must not authorise a source"
    assert wizard.audits[-1]["action"] == "dsr.domain_checked"


@pytest.mark.asyncio
async def test_a_live_dns_check_proves_ownership_and_hides_the_record_once_proven(wizard):
    wizard.dns = _dns_yes
    result = await dsr_routes.setup_check_domain(
        payload=dsr_routes.DomainIn(domain="example.com", verify=True), user=ADMIN, db=_Db(),
    )
    assert result["ownership"]["proven"] and result["ownership"]["method"] == "dns_txt"
    assert result["txt_record"] is None


@pytest.mark.asyncio
async def test_discovery_is_refused_until_the_domain_is_proven(wizard):
    with pytest.raises(setup_service.DomainNotVerifiedError, match="not verified"):
        await dsr_routes.setup_discover(
            payload=dsr_routes.DomainIn(domain="example.com"), user=ADMIN, db=_Db(),
        )
    assert wizard.manifest_calls == 0, "nothing may be fetched for an unproven domain"


@pytest.mark.asyncio
async def test_discovery_for_a_proven_domain_lists_options_and_sources_without_authorising(wizard):
    wizard.website = SimpleNamespace(domain="example.com", verified_at=datetime.now(UTC) - timedelta(days=5))
    pg, ms = _src(name="crm"), _src(name="erp", connector="sqlserver")
    wizard.sources = [pg, ms]

    result = await dsr_routes.setup_discover(
        payload=dsr_routes.DomainIn(domain="example.com"), user=ADMIN, db=_Db(),
    )

    assert result["ownership"]["method"] == "existing_verification"
    readiness = {s["name"]: s["readiness"] for s in result["sources"]}
    assert readiness == {"crm": "ready_to_authorize", "erp": "connector_not_supported"}
    assert {p["id"] for p in result["paths"]} >= {"postgres_direct", "evidence_push"}
    assert result["manifest"]["found"] is False and wizard.manifest_calls == 1
    assert result["grants_access"] is False and wizard.authorization_writes == 0


@pytest.mark.asyncio
async def test_a_non_admin_cannot_use_the_wizard(wizard):
    for call in (
        dsr_routes.setup_check_domain(payload=dsr_routes.DomainIn(domain="example.com"), user=MEMBER, db=_Db()),
        dsr_routes.setup_discover(payload=dsr_routes.DomainIn(domain="example.com"), user=MEMBER, db=_Db()),
    ):
        with pytest.raises(Exception, match="administrator"):
            await call


@pytest.mark.asyncio
async def test_the_overview_reports_stale_and_fresh_domains_and_changes_nothing(wizard):
    wizard.website = SimpleNamespace(
        domain="old.example.com",
        verified_at=datetime.now(UTC) - timedelta(days=setup_service.DOMAIN_VERIFICATION_MAX_AGE_DAYS + 30),
    )
    result = await dsr_routes.setup_overview(user=MEMBER, db=_Db())

    [domain] = result["domains"]
    assert domain["verified"] is False and domain["stale"] is True
    assert wizard.audits == [] and wizard.authorization_writes == 0


# ── Schema listing and revocation ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_schema_listing_returns_names_only_and_works_before_authorization(monkeypatch):
    source = _src()
    built = {}

    async def get_source(db, sid, org_id):
        return source

    class _Conn:
        async def list_schema(self):
            return {"schema": "public", "truncated": False, "tables": [
                {"name": "customers", "columns": [{"name": "email", "type": "text"}]},
            ]}

    def build(**kw):
        built.update(kw)
        return _Conn()

    async def audit(db, **kw):
        built["audit"] = kw

    monkeypatch.setattr(ropa_repository, "get_data_source", get_source)
    monkeypatch.setattr(dsr_routes, "build_connector", build)
    monkeypatch.setattr(audit_service, "record", audit)

    result = await dsr_routes.get_source_schema(data_source_id=source.id, user=ADMIN, db=_Db())

    assert result["available"] and result["tables"][0]["name"] == "customers"
    assert built["for_execution"] is False, "a schema listing must never build a write connector"
    assert built["authorization"].searchable_tables == [] and built["authorization"].allow_execution is False
    assert "email" not in repr(built["audit"]["after"]), "column names are not written to the audit trail"


@pytest.mark.asyncio
async def test_a_schema_listing_failure_is_a_useful_result_not_a_crash(monkeypatch):
    from app.agents.dsr.errors import ConnectorUnavailableError

    source = _src()

    async def get_source(db, sid, org_id):
        return source

    class _Down:
        async def list_schema(self):
            raise ConnectorUnavailableError("source 'crm' is unreachable or rejected the connection")

    async def audit(db, **kw):
        return None

    monkeypatch.setattr(ropa_repository, "get_data_source", get_source)
    monkeypatch.setattr(dsr_routes, "build_connector", lambda **kw: _Down())
    monkeypatch.setattr(audit_service, "record", audit)

    result = await dsr_routes.get_source_schema(data_source_id=source.id, user=ADMIN, db=_Db())

    assert result["available"] is False and result["error_code"] == "CONNECTOR_UNAVAILABLE"


@pytest.mark.asyncio
async def test_the_schema_listing_refuses_an_unsupported_connector(monkeypatch):
    source = _src(connector="sqlserver")

    async def get_source(db, sid, org_id):
        return source

    monkeypatch.setattr(ropa_repository, "get_data_source", get_source)
    with pytest.raises(SourceNotAuthorizedError, match="cannot search"):
        await dsr_routes.get_source_schema(data_source_id=source.id, user=ADMIN, db=_Db())


@pytest.mark.asyncio
async def test_revoking_clears_execution_and_both_write_credentials(monkeypatch):
    source = _src()
    row = SimpleNamespace(
        id=uuid.uuid4(), data_source_id=source.id, enabled=True, allow_execution=True,
        write_credential_ref="CRM_WRITE", write_credential_ciphertext="ciphertext",
        searchable_tables=["customers"],
    )
    audits = []

    async def get_source(db, sid, org_id):
        return source

    async def get_auth(db, sid, org_id):
        return row

    async def audit(db, **kw):
        audits.append(kw)

    monkeypatch.setattr(ropa_repository, "get_data_source", get_source)
    monkeypatch.setattr(dsr_repository, "get_source_authorization", get_auth)
    monkeypatch.setattr(audit_service, "record", audit)

    result = await dsr_routes.revoke_source(data_source_id=source.id, user=ADMIN, db=_Db())

    assert result == {"source_name": "crm", "enabled": False, "allow_execution": False}
    assert row.enabled is False and row.allow_execution is False
    assert row.write_credential_ref is None and row.write_credential_ciphertext is None
    assert row.searchable_tables == ["customers"], "the read allowlist stays for review"
    assert audits[0]["action"] == "dsr.source_authorization_revoked"
    assert "ciphertext" not in repr(audits[0])


@pytest.mark.asyncio
async def test_revoking_a_source_with_no_authorization_is_a_clear_error(monkeypatch):
    source = _src()

    async def get_source(db, sid, org_id):
        return source

    async def get_auth(db, sid, org_id):
        return None

    monkeypatch.setattr(ropa_repository, "get_data_source", get_source)
    monkeypatch.setattr(dsr_repository, "get_source_authorization", get_auth)
    with pytest.raises(DsrNotFoundError, match="no DSR authorization"):
        await dsr_routes.revoke_source(data_source_id=source.id, user=ADMIN, db=_Db())


@pytest.mark.asyncio
async def test_a_revoked_source_is_not_searched(world, monkeypatch):
    """The disabled authorization must stop a search, not merely be hidden in the UI."""
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Delete my personal information.")
    await _verify(request)

    async def revoked(db, org_id):
        return [SimpleNamespace(enabled=False)]
    monkeypatch.setattr(dsr_repository, "list_source_authorizations", revoked)

    async def no_enqueue(db, **kw):
        raise AssertionError("no search job may be queued against a revoked source")
    monkeypatch.setattr(dsr_routes.queue, "enqueue", no_enqueue)

    with pytest.raises(SourceNotAuthorizedError):
        await dsr_routes.start_search(request_id=request.id, user=ADMIN, db=FakeDB())


# ── Outcome: the contract a portal reads before showing "resolved" ───────────────

async def _approved(monkeypatch, n=1, connector=None):
    _use_connector(monkeypatch, connector or FakeConnector(matches=[match(record_id=7), match("orders", 11)][:n]))
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)
    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    for a in actions:
        await approval_service.decide_action(
            FakeDB(), request, a.id, reviewer_user_id=USER, decision="approved", reason="ok",
        )
    await case_service.transition(FakeDB(), request, case.APPROVED)
    return request, actions


@pytest.mark.asyncio
async def test_found_records_alone_are_not_a_resolution(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    await _plan(request)

    out = await dsr_routes.get_outcome(request_id=request.id, user=ADMIN, db=FakeDB())

    assert out["search"]["state"] == "succeeded" and out["search"]["records_found"] == 1
    assert out["execution"]["executed_verified"] == 0
    assert out["closure"] == outcome_service.CLOSURE_OPEN
    assert out["portal_may_show_resolved"] is False


@pytest.mark.asyncio
async def test_a_verified_execution_and_completion_is_the_only_resolution(world, monkeypatch):
    from app.agents.dsr.services import response_service

    monkeypatch.setattr(dsr_repository, "purge_evidence_pushes", _fake_purge)
    request, actions = await _approved(monkeypatch, 1)
    _run_worker_execution_against(monkeypatch, FakeDB())
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)

    mid = await dsr_routes.get_outcome(request_id=request.id, user=ADMIN, db=FakeDB())
    assert mid["execution"]["executed_verified"] == 1
    assert mid["portal_may_show_resolved"] is False, "executed but not yet answered/closed"

    await response_service.build_response(FakeDB(), request)
    await dsr_routes.complete_case(request_id=request.id, user=ADMIN, db=FakeDB())
    done = await dsr_routes.get_outcome(request_id=request.id, user=ADMIN, db=FakeDB())

    assert done["closure"] == outcome_service.CLOSURE_VERIFIED_COMPLETE
    assert done["portal_may_show_resolved"] is True and done["public_status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_a_failed_execution_is_reported_as_failed_even_though_data_was_found(world, monkeypatch):
    from app.agents.dsr.errors import ActionFailedError

    fc = FakeConnector(matches=[match()], exec_raises=ActionFailedError("down"))
    request, _ = await _approved(monkeypatch, 1, connector=fc)
    _run_worker_execution_against(monkeypatch, FakeDB())
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)

    out = await dsr_routes.get_outcome(request_id=request.id, user=ADMIN, db=FakeDB())

    assert out["search"]["records_found"] == 1
    assert out["execution"]["failed"] == 1 and out["execution"]["executed_verified"] == 0
    assert out["closure"] == outcome_service.CLOSURE_FAILED
    assert out["portal_may_show_resolved"] is False


def test_closure_for_an_unverified_or_unresolved_completion_never_reads_clean():
    base = dict.fromkeys(("executed_verified", "executed_unverified", "failed", "blocked", "rejected", "awaiting"), 0)
    assert outcome_service.closure(case.COMPLETED, None, {**base, "executed_verified": 2}) == "verified_complete"
    assert outcome_service.closure(case.COMPLETED, None, base) == "completed_nothing_to_do"
    for dirty in ("failed", "blocked", "rejected", "awaiting", "executed_unverified"):
        got = outcome_service.closure(case.COMPLETED, None, {**base, "executed_verified": 1, dirty: 1})
        assert got == "partially_complete", dirty
    assert outcome_service.closure(case.FAILED, case.ERR_SOURCE_NOT_AUTHORIZED, base) == "waiting_for_setup"
    assert outcome_service.closure(case.FAILED, case.ERR_ACTION_FAILED, base) == "failed"
    assert outcome_service.closure(case.PARTIALLY_COMPLETED, None, base) == "partially_complete"


# ── Reconciliation: report, never overwrite ──────────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_reports_the_exact_mismatches_seen_in_the_live_app(world, monkeypatch):
    # Portal: "Resolved" / identity "Not verified".  Agent: FAILED / identity verified.
    resolved_case, _ = await case_service.create_case(
        FakeDB(), org_id=ORG, raw_request="Give me my data", requester_email="a@b.com",
        idempotency_key="PORTAL-1", created_by_user_id=USER, now=NOW,
    )
    await case_service.classify_case(FakeDB(), resolved_case, actor_user_id=USER)
    await _verify(resolved_case)
    await case_service.transition(FakeDB(), resolved_case, case.SEARCHING)
    await case_service.transition(
        FakeDB(), resolved_case, case.FAILED, error_code=case.ERR_SEARCH_FAILED, error_detail="x",
    )
    before = (resolved_case.status, resolved_case.error_code, resolved_case.updated_at)

    payload = dsr_routes.ReconcileRequest(items=[
        dsr_routes.ReconcileItem(client_reference="PORTAL-1", portal_status="Resolved", portal_identity_verified=False),
        dsr_routes.ReconcileItem(client_reference="PORTAL-404", portal_status="Open"),
        dsr_routes.ReconcileItem(client_reference="PORTAL-1", portal_status="Mystery Status"),
    ])
    out = await dsr_routes.reconcile_requests(payload=payload, user=ADMIN, db=FakeDB())

    first, missing, unmapped = out["results"]
    assert first["agrees"] is False
    assert "portal says 'Resolved' but the agent case is FAILED" in first["reason"]
    assert "identity differs" in first["reason"]
    assert missing["found"] is False and "no agent case" in missing["reason"]
    assert unmapped["agrees"] is None and "no mapping" in unmapped["reason"]
    assert (out["checked"], out["mismatches"], out["not_found"], out["unmapped"]) == (3, 1, 1, 1)
    assert (resolved_case.status, resolved_case.error_code, resolved_case.updated_at) == before, (
        "reconciliation must never change the case"
    )


def test_portal_status_matching_is_forgiving_about_spelling_but_strict_about_meaning():
    ok = outcome_service.reconcile_item(
        client_reference="x", portal_status="Partially Resolved", portal_identity_verified=None,
        request=SimpleNamespace(id=uuid.uuid4(), reference="DSR-1", status=case.PARTIALLY_COMPLETED, error_code=None),
        identity_satisfied=None,
    )
    assert ok["agrees"] is True
    wrong = outcome_service.reconcile_item(
        client_reference="x", portal_status="resolved", portal_identity_verified=None,
        request=SimpleNamespace(id=uuid.uuid4(), reference="DSR-1", status=case.PARTIALLY_COMPLETED, error_code=None),
        identity_satisfied=None,
    )
    assert wrong["agrees"] is False, "'resolved' must not agree with a partial outcome"


# ── A revoked authorization is visible to the wizard but never usable ────────────

@pytest.mark.asyncio
async def test_the_overview_shows_a_revoked_source_as_revoked(wizard):
    source = _src()
    wizard.sources = [source]
    wizard.authorizations = [_auth(source, enabled=False)]

    result = await dsr_routes.setup_overview(user=MEMBER, db=_Db())

    assert result["sources"][0]["readiness"] == "revoked"


def test_the_search_path_still_sees_only_enabled_authorizations():
    """list_source_authorizations feeds search and execution; a revoked row must never reach
    them. The display-only list is a separate function precisely so this filter stays put."""
    import inspect

    search_list = inspect.getsource(dsr_repository.list_source_authorizations)
    display_list = inspect.getsource(dsr_repository.list_all_source_authorizations)
    assert "enabled.is_(True)" in search_list
    assert "enabled" not in display_list.split('"""', 2)[-1], "the display list must not filter"
