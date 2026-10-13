"""DSR routes accept an org-scoped service key, with config writes behind their own scope."""

import pytest

from app.agents.dsr.errors import AdminRequiredError
from app.api.v1.routes import dsr
from app.core import integration_auth
from app.core.security import CurrentUser


def _deps(route) -> set:
    return {d.call for d in route.dependant.dependencies}


def test_scopes_are_mintable():
    assert {"dsr:operate", "dsr:config"} <= set(integration_auth.KNOWN_SCOPES)


def test_every_dsr_route_accepts_a_key_and_none_use_the_person_only_dependency():
    for route in dsr.router.routes:
        deps = _deps(route)
        assert deps & {integration_auth.get_dsr_operator, integration_auth.get_dsr_config_writer}, route.path


def test_config_writes_need_the_config_scope_and_reads_need_only_operate():
    by = {(m, r.path): _deps(r) for r in dsr.router.routes for m in r.methods}
    for key in [("PUT", "/api/v1/dsr/config/sources"), ("PUT", "/api/v1/dsr/config/retention"),
                ("DELETE", "/api/v1/dsr/config/retention/{rule_id}")]:
        assert integration_auth.get_dsr_config_writer in by[key]
    for key in [("GET", "/api/v1/dsr/config/sources"), ("GET", "/api/v1/dsr/config/retention"),
                ("GET", "/api/v1/dsr/requests")]:
        assert integration_auth.get_dsr_operator in by[key]


def test_key_passes_admin_check_but_roleless_or_member_person_does_not():
    dsr._require_admin(CurrentUser(user_id="u", org_id="o", role=None, key_name="k"))
    dsr._require_admin(CurrentUser(user_id="u", org_id="o", role="admin"))
    for role in (None, "member"):
        with pytest.raises(AdminRequiredError) as exc:
            dsr._require_admin(CurrentUser(user_id="u", org_id="o", role=role))
        # A permission failure, not a case-state one -- it used to raise
        # CaseNotReadyError (409), which misrepresented this as a precondition about
        # some case rather than a question of who is asking.
        assert exc.value.status_code == 403
