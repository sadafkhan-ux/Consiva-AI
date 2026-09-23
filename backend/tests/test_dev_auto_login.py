"""The login form is skipped in development. It must not be skippable anywhere else.

Bypassing authentication is a reasonable thing to want while testing and a catastrophic
thing to ship. What makes it safe here is not one check but four, and these tests hold
each of them, because the failure mode is silent: a deployment with this reachable
looks exactly like one without it until somebody finds the endpoint.

  1. The router is only registered when APP_ENV=development, so the path does not
     exist in the routing table at all otherwise.
  2. The handler checks again, in case that registration is changed.
  3. It answers 404 rather than 403, so the endpoint's existence is not confirmed.
  4. The frontend only calls it behind Vite's DEV flag, so a production BUILD contains
     no code that asks for it.

And one thing that is deliberately NOT bypassed: the token. Auto-login issues an
ordinary access token through the same function real login uses, so org scoping, RLS
and audit attribution behave exactly as they do for a signed-in person. Skipping the
form is not the same as skipping the mechanism, and a shortcut that skipped the
mechanism would leave every screen empty anyway -- since migration 0018, an unscoped
database connection reads zero rows from every table.
"""

import inspect
import pathlib

import pytest

from app.api.v1 import router as api_router_module
from app.api.v1.routes import dev

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"


def _read(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


# ── The four gates ──────────────────────────────────────────────────────────────

def test_the_dev_router_is_only_registered_in_development():
    body = inspect.getsource(api_router_module)
    assert 'if get_settings().app_env == "development":' in body
    assert "api_router.include_router(dev.router)" in body


def test_the_handler_checks_the_environment_itself():
    """Defence in depth. If the registration above is ever loosened, this still
    refuses."""
    body = inspect.getsource(dev.auto_login)
    assert 'settings.app_env != "development"' in body
    assert "HTTPException(status_code=404)" in body


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


def test_the_frontend_only_calls_it_in_a_development_build():
    auth = _read("src/api/auth.ts")
    assert "export const AUTH_BYPASSED = import.meta.env.DEV;" in auth
    app = _read("src/App.tsx")
    assert "if (!AUTH_BYPASSED || profile || autoLoginTried) return;" in app


def test_a_production_bundle_contains_no_auto_login_code():
    """Vite replaces import.meta.env.DEV with a literal, so the minifier removes the
    branch entirely -- the request is not merely unreachable, the code is absent.

    Skips when no bundle has been built; it asserts a property of dist/, not of the
    source, and a missing dist/ is a missing artefact rather than a failure.
    """
    dist = FRONTEND / "dist" / "assets"
    if not dist.exists():
        pytest.skip("no production build present; run `npm run build` in frontend/")
    bundles = list(dist.glob("*.js"))
    assert bundles, "dist/assets exists but holds no JavaScript"
    for bundle in bundles:
        text = bundle.read_text(encoding="utf-8", errors="replace")
        assert "dev/auto-login" not in text, f"{bundle.name} can reach the dev endpoint"
        assert "Login bypassed" not in text, f"{bundle.name} carries the bypass banner"


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
