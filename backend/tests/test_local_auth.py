"""First-party auth: password hashing, token issue/verify, and dual-mode."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.config import get_settings
from app.core import passwords, tokens


def _settings(secret: str | None = "test-secret-at-least-32-bytes-long!!"):
    """A Settings copy with a known CONSIVA_JWT_SECRET, so these tests don't
    depend on whatever the local .env happens to hold."""
    return get_settings().model_copy(update={"consiva_jwt_secret": secret})


# ── Password hashing ────────────────────────────────────────────────────────────


def test_hash_and_verify_round_trip():
    h = passwords.hash_password("correct horse battery staple")
    assert passwords.verify_password("correct horse battery staple", h)
    assert not passwords.verify_password("wrong password entirely", h)


def test_hash_is_salted_per_call():
    """Two users with the same password must not share a hash, or one cracked
    hash would reveal both."""
    a = passwords.hash_password("same password here")
    b = passwords.hash_password("same password here")
    assert a != b
    assert passwords.verify_password("same password here", a)
    assert passwords.verify_password("same password here", b)


def test_hash_never_contains_the_password():
    h = passwords.hash_password("MySecretPassword123")
    assert "MySecretPassword123" not in h


def test_short_password_rejected():
    with pytest.raises(passwords.PasswordError, match="at least"):
        passwords.hash_password("short")


def test_overlong_password_rejected_not_truncated():
    """bcrypt silently ignores everything past 72 bytes, which would give two
    different passwords the same hash. Reject instead."""
    with pytest.raises(passwords.PasswordError, match="72 bytes"):
        passwords.hash_password("a" * 73)


def test_malformed_stored_hash_reads_as_wrong_password():
    """A corrupt row must not raise -- that would confirm the account exists."""
    assert passwords.verify_password("anything", "not-a-bcrypt-hash") is False
    assert passwords.verify_password("anything", "") is False


# ── Token issue / verify ────────────────────────────────────────────────────────


def test_issue_and_decode_round_trip():
    s = _settings()
    uid, org = str(uuid.uuid4()), str(uuid.uuid4())
    token, expires_in = tokens.issue_access_token(user_id=uid, org_id=org, role="admin", settings=s)
    payload = tokens.decode_access_token(token, s)

    assert payload["sub"] == uid
    assert payload["org_id"] == org
    assert payload["role"] == "admin"
    assert payload["iss"] == tokens.ISSUER
    assert expires_in == tokens.DEFAULT_TTL_HOURS * 3600


def test_token_signed_with_another_secret_is_rejected():
    token, _ = tokens.issue_access_token(
        user_id="u", org_id="o", role="member", settings=_settings("secret-number-one-aaaaaaaaaaaaaaaa")
    )
    with pytest.raises(tokens.TokenError):
        tokens.decode_access_token(token, _settings("secret-number-two-bbbbbbbbbbbbbbbb"))


def test_expired_token_is_rejected():
    s = _settings()
    expired = jwt.encode(
        {
            "sub": "u", "org_id": "o", "role": "admin",
            "aud": tokens.AUDIENCE, "iss": tokens.ISSUER,
            "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
        },
        s.consiva_jwt_secret, algorithm=tokens.ALGORITHM,
    )
    with pytest.raises(tokens.TokenError):
        tokens.decode_access_token(expired, s)


def test_missing_secret_raises_rather_than_using_a_default():
    """A predictable signing secret would let anyone mint an admin token."""
    with pytest.raises(tokens.TokenError, match="CONSIVA_JWT_SECRET"):
        tokens.issue_access_token(user_id="u", org_id="o", role="admin", settings=_settings(None))


def test_issuer_probe_is_routing_only_not_authorization():
    """looks_like_consiva_token reads an UNVERIFIED claim, so a forged iss must
    still fail real verification."""
    forged = jwt.encode(
        {"sub": "u", "org_id": "o", "aud": tokens.AUDIENCE, "iss": tokens.ISSUER},
        "an-attacker-chosen-secret", algorithm="HS256",
    )
    assert tokens.looks_like_consiva_token(forged) is True
    with pytest.raises(tokens.TokenError):
        tokens.decode_access_token(forged, _settings())


def test_supabase_token_is_not_mistaken_for_ours():
    supabase_shaped = jwt.encode(
        {"sub": "u", "org_id": "o", "aud": "authenticated"},
        os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256",
    )
    assert tokens.looks_like_consiva_token(supabase_shaped) is False


def test_garbage_is_not_mistaken_for_a_token():
    for junk in ("", "not.a.token", "csv_abc_def"):
        assert tokens.looks_like_consiva_token(junk) is False


# ── Dual-mode dependency behaviour ──────────────────────────────────────────────


def test_consiva_token_yields_current_user_with_role():
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from app.core.security import get_current_user

    s = _settings()
    uid, org = str(uuid.uuid4()), str(uuid.uuid4())
    token, _ = tokens.issue_access_token(user_id=uid, org_id=org, role="admin", settings=s)

    user = get_current_user(
        credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), settings=s
    )
    assert user.user_id == uid
    assert user.org_id == org
    assert user.role == "admin"

    with pytest.raises(HTTPException) as exc:
        get_current_user(
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials="garbage"), settings=s
        )
    assert exc.value.status_code == 401


def test_token_without_org_id_is_forbidden():
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from app.core.security import get_current_user

    s = _settings()
    no_org = jwt.encode(
        {"sub": "u", "aud": tokens.AUDIENCE, "iss": tokens.ISSUER,
         "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())},
        s.consiva_jwt_secret, algorithm=tokens.ALGORITHM,
    )
    with pytest.raises(HTTPException) as exc:
        get_current_user(
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=no_org), settings=s
        )
    assert exc.value.status_code == 403


def test_supabase_paths_rejected_when_supabase_is_removed():
    """After the cut-over -- no Supabase settings at all -- a Supabase token can
    no longer be verified by anything and must be refused."""
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from app.core.security import get_current_user

    stripped = get_settings().model_copy(update={
        "consiva_jwt_secret": "test-secret-at-least-32-bytes-long!!",
        "supabase_url": None, "supabase_jwt_secret": None,
    })
    legacy = jwt.encode(
        {"sub": "u", "org_id": "o", "aud": "authenticated"},
        "whatever-secret", algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc:
        get_current_user(
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=legacy),
            settings=stripped,
        )
    assert exc.value.status_code == 401
