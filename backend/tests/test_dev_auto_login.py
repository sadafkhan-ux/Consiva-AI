"""The login form is skipped in development, and on a deployment that explicitly sets
OPEN_ACCESS=true. It must not be skippable anywhere else.

Bypassing authentication is a reasonable thing to want while testing and a catastrophic
thing to ship. What makes it safe here is not one check but four, and these tests hold
each of them, because the failure mode is silent: a deployment with this reachable
looks exactly like one without it until somebody finds the endpoint.

  1. The router is only registered when APP_ENV=development or OPEN_ACCESS=true, so
     the path does not exist in the routing table at all otherwise.
  2. The handler checks again, in case that registration is changed.
  3. It answers 404 rather than 403, so the endpoint's existence is not confirmed.
  4. The frontend only calls it behind Vite's DEV flag or VITE_OPEN_ACCESS=true, so any
     other production BUILD contains no code that asks for it.

And one thing that is deliberately NOT bypassed: the token. Auto-login issues an
ordinary access token through the same function real login uses, so org scoping, RLS
and audit attribution behave exactly as they do for a signed-in person. Skipping the
form is not the same as skipping the mechanism, and a shortcut that skipped the
mechanism would leave every screen empty anyway -- since migration 0018, an unscoped
database connection reads zero rows from every table.
"""

import inspect
import pathlib
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api.v1 import router as api_router_module
from app.config import Settings, get_settings
from app.api.v1.routes import dev
from app.core import tokens
from app.db.session import get_db
from app.main import app

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"


def _read(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


# ── The four gates ──────────────────────────────────────────────────────────────

def test_the_dev_router_is_only_registered_when_auto_login_is_enabled():
    body = inspect.getsource(api_router_module)
    assert "if get_settings().auto_login_enabled:" in body
    assert "api_router.include_router(dev.router)" in body


def _settings(**overrides) -> Settings:
    base = dict(
        nvidia_api_key="k", nvidia_llm_model="m", nvidia_embed_model="e",
        database_url="postgresql+asyncpg://u:p@h/d", app_env="production",
    )
    return Settings(_env_file=None, **{**base, **overrides})


def test_open_access_is_off_unless_stated(monkeypatch):
    """The switch that removes the login has to be asked for. A deployment that never
    mentions it keeps the login screen."""
    monkeypatch.delenv("OPEN_ACCESS", raising=False)
    assert _settings().open_access is False
    assert _settings().auto_login_enabled is False


def test_auto_login_is_enabled_by_development_or_open_access(monkeypatch):
    monkeypatch.delenv("OPEN_ACCESS", raising=False)
    assert _settings(app_env="development").auto_login_enabled is True
    assert _settings(open_access=True).auto_login_enabled is True


def test_the_handler_checks_the_condition_itself():
    """Defence in depth. If the registration above is ever loosened, this still
    refuses."""
    body = inspect.getsource(dev.auto_login)
    assert "if not settings.auto_login_enabled:" in body
    assert "HTTPException(status_code=404)" in body


def test_demo_token_stays_development_only_under_open_access():
    """Open access registers the dev router, which also carries demo-token. That
    one mints a token for a fixed demo org and has no business on a real site."""
    body = inspect.getsource(dev.demo_token)
    assert 'settings.app_env != "development"' in body
    assert "open_access" not in body


def test_it_answers_404_rather_than_403():
    """A 403 confirms the endpoint is there, which is the one thing an endpoint like
    this should not do.

    Checks what is RAISED rather than searching the whole source: the docstring says
    the word "403" while explaining why it is not used, and an assertion that cannot
    tell prose from code would fail on the explanation.
    """
    body = inspect.getsource(dev.auto_login)
    assert "HTTPException(status_code=404)" in body
    assert "status_code=403" not in body


def test_the_frontend_only_calls_it_in_a_dev_or_open_access_build():
    """Both operands are replaced with literals by Vite, so a production build made
    without VITE_OPEN_ACCESS=true has the branch removed by the minifier. (The built
    bundle itself used to be checked here, but dist/ now legitimately contains the
    code when it was built for open access, so the test could not tell right from
    wrong. Check a build by grepping it for `dev/auto-login`.)"""
    auth = _read("src/api/auth.ts")
    assert 'export const OPEN_ACCESS = import.meta.env.VITE_OPEN_ACCESS === "true";' in auth
    assert "export const AUTH_BYPASSED = import.meta.env.DEV || OPEN_ACCESS;" in auth
    app = _read("src/App.tsx")
    assert "if (!AUTH_BYPASSED || profile || autoLoginTried) return;" in app


def test_the_frontend_build_defaults_to_the_login_screen():
    dockerfile = _read("Dockerfile")
    assert 'ARG VITE_OPEN_ACCESS="false"' in dockerfile


def test_the_real_login_still_exists():
    """This removes a step for developers, not the ability to authenticate. A
    production build has to still be able to sign somebody in."""
    auth = _read("src/api/auth.ts")
    assert "export async function login(" in auth
    assert "/api/v1/auth/login" in auth
    app = _read("src/App.tsx")
    # And the form is still rendered as the fallback rather than deleted.
    assert "<LoginScreen onSignedIn={setProfile}" in app


# ── What is NOT bypassed ────────────────────────────────────────────────────────

def test_auto_login_issues_a_real_token_not_a_special_one():
    """A bespoke token shape would be a second auth system, which this project's
    build rules forbid, and would drift from the real one the first time claims
    changed."""
    body = inspect.getsource(dev.auto_login)
    assert "tokens.issue_access_token" in body
    for claim in ("user_id=", "org_id=", "role="):
        assert claim in body


def test_it_signs_in_as_a_real_user_from_the_database():
    """Not a fabricated identity. The org on the token has to be one that owns data,
    or every screen loads empty under row-level security."""
    body = inspect.getsource(dev.auto_login)
    assert "select(User)" in body
    assert "User.created_at" in body  # stable choice across calls


def test_an_empty_database_gets_an_explanation_not_a_crash():
    body = inspect.getsource(dev.auto_login)
    assert "status_code=409" in body
    assert "create_user.py" in body


def test_the_response_says_plainly_that_no_password_was_checked():
    """So the console's banner reads a fact off the payload instead of asserting
    something it assumed."""
    body = inspect.getsource(dev.auto_login)
    assert '"auth_bypassed": True' in body
    assert "no password was checked" in body


def test_the_console_shows_a_permanent_warning_while_bypassed():
    """A console that silently skips authentication looks exactly like one that
    authenticated -- which matters the moment anybody screenshots it."""
    app = _read("src/App.tsx")
    assert "Login bypassed" in app
    assert "AUTH_BYPASSED && (" in app


# ── Over HTTP ───────────────────────────────────────────────────────────────────
#
# The checks above read the source. These call the endpoint, so a refactor that keeps
# the right strings but breaks the behaviour still fails. Plain `TestClient(app)`
# rather than `with TestClient(app)`, matching test_api_validation.py: the `with`
# form runs the lifespan, which needs a real database these tests do not have.

_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_SECRET = "test-secret-at-least-32-bytes-long!!"


class _StubUser:
    id = _USER_ID
    org_id = _ORG_ID
    email = "admin@example.com"
    role = "admin"


class _StubOrg:
    name = "Example Org"


@pytest.fixture
def post_auto_login(monkeypatch):
    """Posts to the endpoint with the database stubbed: the first query returns
    `user`, the second the user's organisation."""

    def _call(*, app_env: str = "development", open_access: bool = False, user=_StubUser()):
        results = iter([user, _StubOrg()])

        class _Result:
            def __init__(self, value):
                self._value = value

            def scalar_one_or_none(self):
                return self._value

        class _StubSession:
            async def execute(self, _stmt):
                return _Result(next(results))

        async def _fake_db():
            yield _StubSession()

        app.dependency_overrides[get_db] = _fake_db
        settings = get_settings().model_copy(
            update={
                "app_env": app_env,
                "open_access": open_access,
                "consiva_jwt_secret": _SECRET,
            }
        )
        monkeypatch.setattr(dev, "get_settings", lambda: settings)
        return TestClient(app).post("/api/v1/dev/auto-login")

    yield _call
    app.dependency_overrides.clear()


def test_http_404_outside_development_without_open_access(post_auto_login):
    assert post_auto_login(app_env="production").status_code == 404


def test_http_open_access_enables_it_in_production(post_auto_login):
    assert post_auto_login(app_env="production", open_access=True).status_code == 200


def test_http_issues_a_token_that_verifies_and_carries_the_org(post_auto_login):
    response = post_auto_login()
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["auth_bypassed"] is True
    assert body["user"]["org_id"] == str(_ORG_ID)

    # The org must be in the token: app/db/session.py sets the RLS GUC from it, and
    # without it every tenant table returns zero rows.
    claims = jwt.decode(
        body["access_token"],
        _SECRET,
        algorithms=[tokens.ALGORITHM],
        audience=tokens.AUDIENCE,
        issuer=tokens.ISSUER,
    )
    assert claims["sub"] == str(_USER_ID)
    assert claims["org_id"] == str(_ORG_ID)
    assert "exp" in claims, "a token with no expiry would never stop working"


def test_http_empty_database_is_409_with_a_way_forward(post_auto_login):
    response = post_auto_login(user=None)
    assert response.status_code == 409
    assert "create_user.py" in response.json()["detail"]
