"""The 96-parameter (method 3) score is the one official Enterprise score (owner's decision, 24 Nov 2026): the report never falls back to the 51-parameter report."""

import asyncio
import uuid
from types import SimpleNamespace

from app.enterprise.api import results
from app.enterprise.report import service


class _Db:
    def __init__(self, org):
        self.org = org

    async def scalar(self, *_a, **_k):
        return self.org


def _patch(monkeypatch, latest_seq, run_calls, stored):
    from app.enterprise.method3 import service as method3

    seq = iter(latest_seq)

    async def latest(_db, _sid):
        return next(seq)

    async def run_safe(sid, org, **kw):
        run_calls.append((sid, org, kw))

    async def store(_db, sid, **fields):
        stored.append(fields)
        return SimpleNamespace(**fields)

    async def gather(*_a, **_k):
        raise AssertionError("the 51-parameter report must not be printed")

    monkeypatch.setattr(method3, "latest", latest)
    monkeypatch.setattr(method3, "run_safe", run_safe)
    monkeypatch.setattr(service, "_store", store)
    monkeypatch.setattr(service, "gather", gather)


def test_a_scan_without_a_96_parameter_result_is_scored_first_and_never_gets_the_51_parameter_report(monkeypatch):
    runs, stored = [], []
    _patch(monkeypatch, [None, None], runs, stored)
    sid, org = uuid.uuid4(), uuid.uuid4()
    row = asyncio.run(service.generate_report(_Db(org), sid))
    assert runs and runs[0][0] == sid and runs[0][1] == org       # it tried to calculate the 96-parameter score
    assert row.status == "failed" and "96-parameter" in row.error
    assert len(stored) == 1


def test_the_official_score_is_the_method3_score():
    row = SimpleNamespace(score=20.0, marks_earned=16.25, applicable_max=81.25, version="ent-dpdp-3.0.1", basis="readiness",
                          as_of=__import__("datetime").date(2026, 11, 24), created_at="2026-11-24T07:05:00Z")
    out = results._official_score(row)
    assert out["score"] == 20.0 and out["marks_earned"] == 16.25 and out["applicable_max"] == 81.25 and out["version"] == "ent-dpdp-3.0.1"
    assert results._official_score(None) is None
    assert results._m3_score(None) is None
