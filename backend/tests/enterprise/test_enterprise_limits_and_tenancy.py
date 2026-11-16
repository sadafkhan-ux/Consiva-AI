"""The crawl's memory and time limits, and the rule that every Enterprise route is authenticated and scoped to the caller's organisation."""
import ast
from pathlib import Path

import pytest

from app.enterprise.crawl.limits import MEMORY_LIMIT, OK, PAUSE, TIME_LIMIT, LimitGuard
from app.enterprise.config import EnterpriseSettings

API = Path(__file__).resolve().parents[2] / "app" / "enterprise" / "api"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def guard(clock, mem, **kw):
    return LimitGuard(memory_budget_mb=kw.pop("budget", 1000), max_runtime_seconds=kw.pop("deadline", 0), clock=clock, rss=lambda: mem["mb"], patience=kw.pop("patience", 300))


def test_under_the_budget_the_crawl_carries_on():
    c, mem = Clock(), {"mb": 500}
    assert guard(c, mem).check() is OK


def test_over_the_budget_it_pauses_then_stops_after_the_patience_and_says_so():
    c, mem = Clock(), {"mb": 1500}
    g = guard(c, mem)
    assert g.check() == PAUSE
    c.t += 299
    assert g.check() == PAUSE
    c.t += 2
    assert g.check() == MEMORY_LIMIT


def test_memory_that_comes_back_down_resets_the_wait():
    c, mem = Clock(), {"mb": 1500}
    g = guard(c, mem)
    assert g.check() == PAUSE
    c.t += 200
    mem["mb"] = 400
    assert g.check() is OK
    mem["mb"] = 1500
    c.t += 200
    assert g.check() == PAUSE                      # a new wait, not the old one carried over


def test_the_deadline_stops_the_crawl_with_time_limit():
    c, mem = Clock(), {"mb": 10}
    g = guard(c, mem, deadline=3600)
    assert g.check() is OK
    c.t += 3600
    assert g.check() == TIME_LIMIT


def test_a_zero_budget_or_deadline_means_none():
    c, mem = Clock(), {"mb": 10**9}
    g = LimitGuard(memory_budget_mb=0, max_runtime_seconds=0, clock=c, rss=lambda: mem["mb"])
    c.t += 10**7
    assert g.check() is OK


def test_the_defaults_are_bounded_and_documented():
    s = EnterpriseSettings(_env_file=None)
    assert s.memory_budget_mb == 4096 and s.max_runtime_seconds == 43_200 and s.max_urls == 100_000


# ---- tenancy: every route is authenticated and loads its scan through the organisation-scoped lookup ---------------------------------------------------------------------

def _routes(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))):
        if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name) and d.func.value.id == "router" for d in fn.decorator_list):
            yield fn


@pytest.mark.parametrize("module", ["scans.py", "results.py"])
def test_every_route_needs_a_caller_and_scopes_to_the_organisation(module):
    routes = list(_routes(API / module))
    assert routes
    for fn in routes:
        source = ast.unparse(fn)
        assert "get_caller" in source or "get_application_scan_caller" in source, f"{fn.name} has no authentication dependency"
        scoped = "_load(" in source or "_scan(" in source or "queue.enqueue" in source and "_load(" in source
        listing = fn.name in ("list_scans", "create_scan")          # a list is filtered by the caller's organisation; a create takes it from the caller
        assert scoped or listing, f"{fn.name} does not load its scan through the organisation-scoped lookup"
        if fn.name == "list_scans":
            assert "EntScan.org_id == org" in source, "the list is not filtered by organisation"


def test_a_second_scan_named_in_a_comparison_must_also_be_the_callers():
    src = (API / "results.py").read_text(encoding="utf-8")
    assert src.count("await _scan(db, against, user)") == 1


def test_the_scan_lookup_filters_by_organisation():
    scans_src = (API.parent / "scans.py").read_text(encoding="utf-8")
    assert "EntScan.org_id == org_id" in scans_src
