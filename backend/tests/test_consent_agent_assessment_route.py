"""One door: the integration API (/consent-agent) serves everything a scan screen and its PDF use, through the console's own handlers.

Additive. The console routes keep their paths and answers; the ten read routes the PDF's print page uses (and the PDF) now also accept a service key holding `consent:scan`. A person's login
token is checked by exactly the same function as before. Decisions that change a scan (analyze, report approval, finding review) stay on the console, login token only.
"""
import asyncio

from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials

from app.api.v1.routes import consent_agent, consent_scans
from app.core import tokens
from app.config import get_settings
from app.core.integration_auth import get_caller
from app.core.security import get_current_user
from app.main import app

AGENT = "/api/v1/consent-agent/scans"
CONSOLE = "/api/v1/consent/scans"
MIRRORED = ("assessment", "stages", "evidence", "policy-analysis", "requirements", "screenshot", "audit", "report.pdf")
READ_ROUTES_FOR_THE_PRINT_PAGE = ("/{scan_id}", "/{scan_id}/stages", "/{scan_id}/findings", "/{scan_id}/evidence", "/{scan_id}/requirements", "/{scan_id}/policy-analysis", "/{scan_id}/screenshot",
                                  "/{scan_id}/assessment", "/{scan_id}/audit", "/{scan_id}/report.pdf")


def _untitled(schema):
    return {k: v for k, v in schema.items() if k != "title"}


def _ops(router, prefix):
    return {(m, r.path) for r in router.routes if isinstance(r, APIRoute) and r.path.startswith(prefix) for m in r.methods}


def _deps(router, path, method="GET"):
    r = next(r for r in router.routes if isinstance(r, APIRoute) and r.path == path and method in r.methods)
    return {d.call for d in r.dependant.dependencies}


def test_the_integration_api_is_the_nine_original_routes_plus_the_read_routes_and_the_console_functions():
    original = {("POST", AGENT + "/web"), ("POST", AGENT), ("GET", AGENT), ("POST", AGENT + "/application"), ("GET", AGENT + "/{scan_id}/status"), ("GET", AGENT + "/{scan_id}"),
                ("GET", AGENT + "/{scan_id}/findings"), ("GET", AGENT + "/{scan_id}/summary"), ("POST", AGENT + "/{scan_id}/cancel")}
    added = {("GET", f"{AGENT}/{{scan_id}}/{name}") for name in MIRRORED}
    console_functions = {("POST", f"{AGENT}/{{scan_id}}/analyze"), ("GET", f"{AGENT}/{{scan_id}}/usage"), ("GET", f"{AGENT}/{{scan_id}}/url-inventory"), ("GET", "/api/v1/consent-agent/usage/history"),
                         ("POST", "/api/v1/consent-agent/findings/{finding_id}/approve"), ("POST", "/api/v1/consent-agent/findings/{finding_id}/reject"), ("POST", "/api/v1/consent-agent/findings/{finding_id}/edit"),
                         ("GET", "/api/v1/consent-agent/websites/verification-token"), ("POST", "/api/v1/consent-agent/websites/verify")}
    assert _ops(consent_agent.router, "/api/v1/consent-agent") == original | added | console_functions
    for m, path in console_functions:
        assert get_current_user in _deps(consent_agent.router, path, m) and get_caller not in _deps(consent_agent.router, path, m), (m, path)   # login token only, as on the console


def test_every_added_route_is_read_only_and_uses_the_service_key_or_person_check():
    for name in MIRRORED:
        assert get_caller in _deps(consent_agent.router, f"{AGENT}/{{scan_id}}/{name}")


def test_the_console_routes_are_the_same_set_and_only_the_print_page_reads_accept_a_service_key():
    ops = _ops(consent_scans.router, CONSOLE)
    assert len(ops) == 19
    for suffix in READ_ROUTES_FOR_THE_PRINT_PAGE:
        assert get_caller in _deps(consent_scans.router, CONSOLE + suffix), suffix
    for m, path in ops:
        if (m == "GET" and path.replace(CONSOLE, "") in READ_ROUTES_FOR_THE_PRINT_PAGE):
            continue
        assert get_current_user in _deps(consent_scans.router, path, m) and get_caller not in _deps(consent_scans.router, path, m), (m, path)   # start, analyze, approvals, usage, URL list: login token only


def test_the_added_routes_call_the_console_handler_and_do_not_copy_it():
    assert consent_agent._console_assessment is consent_scans.get_scan_assessment
    import inspect
    for fn in (consent_agent.get_scan_stages, consent_agent.get_scan_evidence, consent_agent.get_scan_policy_analysis, consent_agent.get_scan_requirements, consent_agent.get_scan_screenshot,
               consent_agent.get_scan_audit, consent_agent.get_scan_report_pdf):
        assert "_cs." in inspect.getsource(fn)


def test_a_login_token_is_checked_by_exactly_the_same_function_through_get_caller():
    settings = get_settings()
    token, _ = tokens.issue_access_token(user_id="3a159164-c798-4eb6-9966-04b00a192d9d", org_id="4234fa20-3275-4ab3-a30f-820e4d277519", role="admin", settings=settings)
    cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    via_caller = asyncio.run(get_caller(credentials=cred, db=None, settings=settings))
    assert via_caller == get_current_user(credentials=cred, settings=settings)


def test_the_response_models_match_the_console_for_the_same_path():
    spec = app.openapi()["paths"]
    for name in ("assessment", "stages", "evidence", "policy-analysis", "requirements", "screenshot", "audit"):
        new = spec[f"{AGENT}/{{scan_id}}/{name}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        old = spec[f"{CONSOLE}/{{scan_id}}/{name}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert _untitled(new) == _untitled(old), name          # FastAPI names a list schema after its route; the shape is what must match
    assert "application/pdf" in spec[f"{AGENT}/{{scan_id}}/report.pdf"]["get"]["responses"]["200"]["content"]
