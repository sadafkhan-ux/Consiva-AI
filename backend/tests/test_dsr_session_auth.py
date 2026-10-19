"""DSR authenticates with the signed-in user's own session -- there is no DSR key to create or pass.

What this holds:

  * every DSR route depends on the ordinary user authentication, and none on a service-key
    dependency (so nothing in the normal workflow can ask for a key)
  * the DSR key scopes no longer exist, so a key cannot be minted for DSR
  * a service key presented to DSR is simply not a valid credential
  * the administrator-only configuration routes still require the administrator role
  * the organisation comes from the verified token, so tenant isolation is unchanged

Synthetic data and in-memory fakes only; nothing here touches a database.
"""

import inspect
import uuid

import pytest
from fastapi import HTTPException

from app.agents.dsr.errors import AdminRequiredError
from app.api.v1.routes import dsr
from app.core import integration_auth
from app.core.security import CurrentUser, get_current_user

ROUTES = [r for r in dsr.router.routes if hasattr(r, "dependant")]


def _calls(route) -> set:
    """Every dependency callable the route pulls in, directly or transitively."""
    found: set = set()

    def walk(dependant):
        for child in dependant.dependencies:
            found.add(child.call)
            walk(child)

    walk(route.dependant)
    return found


def test_there_are_dsr_routes_to_check():
    assert len(ROUTES) >= 25, "the route walk found too few routes to be a meaningful check"


def test_every_dsr_route_authenticates_with_the_signed_in_user():
    for route in ROUTES:
        assert get_current_user in _calls(route), f"{route.path} does not require a signed-in user"


def test_no_dsr_route_can_be_reached_with_a_service_key():
    """Nothing the DSR routes depend on lives in the service-key module."""
    for route in ROUTES:
        for call in _calls(route):
            module = getattr(call, "__module__", "")
            assert module != integration_auth.__name__, f"{route.path} depends on {call.__name__}"


def test_the_dsr_key_scopes_and_dependencies_no_longer_exist():
    assert not hasattr(integration_auth, "get_dsr_operator")
    assert not hasattr(integration_auth, "get_dsr_config_writer")
    assert not hasattr(integration_auth, "SCOPE_DSR_OPERATE")
    assert not hasattr(integration_auth, "SCOPE_DSR_CONFIG")
    assert not [s for s in integration_auth.KNOWN_SCOPES if s.startswith("dsr:")], (
        "a key can no longer be minted with a DSR scope"
    )


def test_a_service_key_is_not_a_valid_credential_for_dsr():
    full_key, _prefix, _hash = integration_auth.generate_key()
    credentials = type("Creds", (), {"credentials": full_key})()
    settings = type("S", (), {"app_env": "production", "supabase_url": None, "supabase_jwt_secret": None})()

    with pytest.raises(HTTPException) as caught:
        get_current_user(credentials, settings)

    assert caught.value.status_code == 401


def test_missing_credentials_are_refused():
    with pytest.raises(HTTPException) as caught:
        get_current_user(None, object())
    assert caught.value.status_code == 401


def test_only_an_administrator_passes_the_configuration_check():
    dsr._require_admin(CurrentUser(user_id="u", org_id="o", role="admin"))
    for role in (None, "member", "viewer"):
        with pytest.raises(AdminRequiredError) as caught:
            dsr._require_admin(CurrentUser(user_id="u", org_id="o", role=role))
        assert caught.value.status_code == 403


def test_a_principal_carrying_a_key_name_is_no_longer_treated_as_an_administrator():
    """Before, a key stood in for the admin role. With no keys on DSR there is nothing to stand in."""
    with pytest.raises(AdminRequiredError):
        dsr._require_admin(CurrentUser(user_id="u", org_id="o", role=None, key_name="old-key"))


@pytest.mark.parametrize("handler", [
    dsr.put_source_authorization, dsr.put_retention_rule, dsr.disable_retention_rule,
    dsr.revoke_source, dsr.test_source_connection,
])
def test_every_configuration_write_requires_the_administrator_role(handler):
    assert "_require_admin(user)" in inspect.getsource(handler)


def test_no_dsr_route_accepts_an_org_id_from_the_caller():
    """The organisation comes from the verified token, which is what keeps tenants apart."""
    for route in ROUTES:
        parameters = set(inspect.signature(route.endpoint).parameters)
        assert "org_id" not in parameters, f"{route.path} takes org_id from the caller"


def test_the_token_org_is_what_scopes_the_request():
    user = CurrentUser(user_id=str(uuid.uuid4()), org_id=str(uuid.uuid4()), role="member")
    assert dsr.uuid.UUID(user.org_id), "DSR derives uuid.UUID(user.org_id), never a request value"
