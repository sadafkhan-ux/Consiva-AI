"""Regression test for a real bug hit live: scanning a site that actually sets
cookies with an expiry (chatgpt.com's Cloudflare __cf_bm cookie) raised
`asyncpg.exceptions.DataError: ... expected a datetime.date or datetime.datetime
instance, got 'str'`.

Root cause: app.scanner.schemas.CookieRecord.expiry is a plain ISO-8601 string
(cookie_detector.py builds it via datetime.isoformat() from Playwright's raw
Unix-timestamp `expires` field, deliberately kept as a string so the scanner module
has no reason to import SQLAlchemy's DB types). app.db.models.Cookie.expiry is a
real DateTime(timezone=True) column. asyncpg's direct parameter binding does not
coerce a str into a timestamp -- SQLAlchemy's ORM only does this coercion for values
it knows are already datetime-typed at the Python level.

This bug was never caught by any earlier test or live run this session because
every site scanned before this (projectflow.gignaati.com, across 15+ scans) set
zero cookies -- the cookie-persistence path with a real expiry value was never
actually exercised until a real site that sets real cookies was tried.
"""

from datetime import UTC, datetime

import pytest

from app.db.repositories.scan_repository import _parse_expiry


def test_parse_expiry_converts_iso_string_to_datetime():
    result = _parse_expiry("2026-08-31T06:35:34+00:00")
    assert result == datetime(2026, 8, 31, 6, 35, 34, tzinfo=UTC)
    assert isinstance(result, datetime)


def test_parse_expiry_returns_none_for_session_cookie():
    """cookie_detector.py sets expiry=None for session cookies (Playwright's
    `expires` is -1 or absent) -- must stay None, not become an error or a bogus
    epoch datetime."""
    assert _parse_expiry(None) is None


def test_parse_expiry_rejects_malformed_string_loudly():
    """A malformed value should fail fast and visibly (ValueError from
    datetime.fromisoformat), not silently insert garbage into a DateTime column."""
    with pytest.raises(ValueError):
        _parse_expiry("not-a-real-timestamp")
