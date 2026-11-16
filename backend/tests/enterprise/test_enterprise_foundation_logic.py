"""The parts of the foundation that decide before touching the database."""
import asyncio

import pytest
from sqlalchemy.dialects import postgresql

from app.enterprise import scans
from app.enterprise.crawl import frontier
from app.enterprise.evidence import store as evidence


def run(coro):
    return asyncio.run(coro)


def test_a_claim_skips_rows_another_worker_holds_and_takes_the_shallowest_first():
    sql = str(frontier._CLAIM_SQL).lower()
    assert "for update skip locked" in sql and "order by priority, depth" in sql and "status = 'pending'" in sql


def test_reclaim_gives_up_on_a_page_that_used_all_its_attempts_and_says_why():
    sql = str(frontier._RECLAIM_SQL).lower()
    assert "attempts >= :max_attempts then 'failed'" in sql and "worker lost" in sql


def test_a_failure_or_an_exclusion_without_a_reason_is_refused():
    with pytest.raises(ValueError):
        run(frontier.mark_failed(None, None, ""))
    with pytest.raises(ValueError):
        run(frontier.mark_excluded(None, None, ""))


def test_evidence_must_name_its_kind_source_and_method_and_is_never_cut():
    base = dict(kind="page_text", source_url="https://x.test/", content="hello", detection_method="dom")
    with pytest.raises(ValueError):
        run(evidence.store(None, None, **{**base, "kind": "bogus"}))
    with pytest.raises(ValueError):
        run(evidence.store(None, None, **{**base, "source_url": ""}))
    with pytest.raises(ValueError):
        run(evidence.store(None, None, **{**base, "detection_method": ""}))
    with pytest.raises(ValueError):
        run(evidence.store(None, None, **{**base, "page_number": -1}))
    from app.enterprise.config import get_enterprise_settings
    get_enterprise_settings.cache_clear()
    big = "x" * (get_enterprise_settings().max_evidence_bytes + 1)
    with pytest.raises(evidence.EvidenceTooLarge):
        run(evidence.store(None, None, **{**base, "content": big}))


def test_the_same_content_has_the_same_hash_and_a_change_has_another():
    assert evidence.content_hash("a") == evidence.content_hash("a") != evidence.content_hash("b")


def test_a_scan_moves_only_along_the_allowed_lifecycle():
    assert "running" in scans.TRANSITIONS["queued"] and "completed" in scans.TRANSITIONS["running"]
    for final in ("completed", "failed", "cancelled"):
        assert scans.TRANSITIONS[final] == frozenset()
    assert "completed" not in scans.TRANSITIONS["queued"]


def test_a_scan_needs_an_absolute_http_url():
    for bad in ("example.com", "ftp://example.com", "https://"):
        with pytest.raises(ValueError):
            run(scans.create_scan(None, org_id=None, website_url=bad))


def test_the_claim_query_compiles_for_postgres():
    assert frontier._CLAIM_SQL.compile(dialect=postgresql.dialect())
