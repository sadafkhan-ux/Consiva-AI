"""The auth bypass: /api/v1/auth/auto-session.

This endpoint removes the login gate on purpose -- it hands a full access token to
any caller with no credentials. These tests exist for one reason: to hold the line
that it is OFF unless the deployment explicitly turned it on, so it can never become
the accidental state of an install that never asked for it.

Plain `TestClient(app)` rather than `with TestClient(app)`, matching
test_api_validation.py: the `with` form runs the lifespan, which builds the LangGraph
checkpointer against a real database these tests do not have.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.v1.routes import auth as auth_routes
from app.config import get_settings
from app.db.session import get_db
from app.main import app

client = TestClient(app)

_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()


class _StubUser:
    def __init__(self, *, is_active: bool = True):
        self.id = _USER_ID
        self.org_id = _ORG_ID
        self.email = "admin@example.com"
        self.role = "admin"
        self.is_active = is_active


@pytest.fixture
def api(monkeypatch):
    """The endpoint with its database collaborators stubbed out.

    Returns a helper that configures the bypass settings and posts to the endpoint.
    """

    class _StubSession:
        """Only what this endpoint touches."""

        def __init__(self):
            self.committed = False

        async def commit(self):
            self.committed = True

    async def _fake_db():
        yield _StubSession()

    app.dependency_overrides[get_db] = _fake_db

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(auth_routes, "apply_org_scope", _noop)
    monkeypatch.setattr(
        auth_routes.audit_repository, "record", _noop, raising=False
    )

    def _configure(*, enabled: bool, email: str | None, user: _StubUser | None):
        async def _get_user_by_email(_db, _email):
            return user

        monkeypatch.setattr(
            auth_routes.user_repository, "get_user_by_email", _get_user_by_email
        )
        overridden = get_settings().model_copy(
            update={
                "auth_bypass_enabled": enabled,
                "auth_bypass_email": email,
                "consiva_jwt_secret": "test-secret-at-least-32-bytes-long!!",
            }
        )
        app.dependency_overrides[get_settings] = lambda: overridden
        return client.post("/api/v1/auth/auto-session")

    yield _configure
    app.dependency_overrides.clear()


# ── The part that matters: it is closed by default ──────────────────────────────


def test_disabled_by_default_in_the_settings_object():
    """Nobody should have to remember to turn this off. A fresh Settings has it off,
    so an install that never heard of it is not open."""
    settings = get_settings()
    assert settings.auth_bypass_enabled is False


def test_returns_404_when_not_enabled(api):
    """404, not 403: a deployment that has not enabled this should not even confirm
    the endpoint exists."""
    response = api(enabled=False, email="admin@example.com", user=_StubUser())
    assert response.status_code == 404


def test_returns_404_even_when_an_email_is_configured(api):
    """Naming an account is not the same as switching the gate off. Both are
    required, and the flag is the one that decides."""
    response = api(enabled=False, email="admin@example.com", user=_StubUser())
    assert response.status_code == 404


# ── When it IS enabled ──────────────────────────────────────────────────────────


def test_issues_a_usable_token_for_the_configured_account(api):
    response = api(enabled=True, email="admin@example.com", user=_StubUser())
    assert response.status_code == 200

    body = response.json()
    assert body["org_id"] == str(_ORG_ID)
    assert body["user_id"] == str(_USER_ID)
    assert body["role"] == "admin"
    assert body["token_type"] == "bearer"
    # The org must be in the token: app/db/session.py sets the RLS GUC from it, and
    # without it every tenant table returns zero rows.
    assert body["access_token"]
    assert body["expires_in"] > 0


def test_token_carries_the_org_so_rls_can_scope_it(api):
    import jwt

    response = api(enabled=True, email="admin@example.com", user=_StubUser())
    claims = jwt.decode(
        response.json()["access_token"],
        "test-secret-at-least-32-bytes-long!!",
        algorithms=["HS256"],
        audience="authenticated",
        options={"verify_iss": False},
    )
    assert claims["org_id"] == str(_ORG_ID)
    assert claims["sub"] == str(_USER_ID)
    assert "exp" in claims, "a token with no expiry would never stop working"


def test_enabled_without_an_email_is_a_configuration_error(api):
    """Half-configured must fail loudly rather than quietly handing out a session
    for whatever account happens to be first."""
    response = api(enabled=True, email=None, user=_StubUser())
    assert response.status_code == 500
    assert "AUTH_BYPASS_EMAIL" in response.json()["detail"]


def test_unknown_account_is_a_configuration_error(api):
    response = api(enabled=True, email="nobody@example.com", user=None)
    assert response.status_code == 500
    assert "not an active account" in response.json()["detail"]


def test_a_disabled_account_cannot_be_auto_signed_in(api):
    """Deactivating the account must still lock it out, even on this path --
    otherwise disabling a user would not actually disable them."""
    response = api(
        enabled=True, email="admin@example.com", user=_StubUser(is_active=False)
    )
    assert response.status_code == 500
    assert "not an active account" in response.json()["detail"]
