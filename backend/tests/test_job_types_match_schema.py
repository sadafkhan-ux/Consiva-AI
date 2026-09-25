"""Every job type the code enqueues must be one the database accepts.

This is the second time this exact trap has fired in this codebase. Migration 0021 had
to widen `agent_run_stages.stage` after `create_rule_findings` spent weeks writing a
value the CHECK rejected; migration 0023 had to widen `agent_jobs.job_type` after the
integration API's first real POST returned 500 on `consent_api_chain`.

Both share a failure shape worth naming: an enumerated CHECK constraint is invisible to
the Python that writes into it. A new value is a plain string, so it type-checks, reads
fine in review, and passes every unit test that does not touch a real database. It only
fails at runtime, in the one place nobody is looking.

So the agreement is asserted directly, and needs no database to do it: the job types in
the enqueue calls and the job types in the migrations, compared as sets.
"""

import ast
import pathlib
import re

BACKEND = pathlib.Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
MIGRATIONS = BACKEND / "migrations"


def _job_types_enqueued() -> set[str]:
    """`job_type=` keyword on every queue.enqueue(...) call, when it is a literal."""
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name != "enqueue":
                continue
            for kw in node.keywords:
                if kw.arg == "job_type" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        found.add(kw.value.value)
    return found


def _job_types_dispatched() -> set[str]:
    """Types the worker knows how to run: `job.job_type == "..."` comparisons.

    Checked as well as the enqueue side because the two fail differently -- a type that
    is enqueued but not dispatched sits in the queue until it is reaped, which looks
    like a hung scan rather than an error.
    """
    source = (APP / "jobs" / "worker.py").read_text(encoding="utf-8")
    return set(re.findall(r'job\.job_type\s*==\s*"([a-z_]+)"', source))


def _job_types_allowed() -> set[str]:
    """The CHECK as the LAST migration to define it leaves it -- later migrations drop
    and re-add the constraint, so the union of all of them would still look correct
    after one narrowed the list."""
    pattern = re.compile(r"check\s*\(\s*job_type\s+in\s*\((.*?)\)\s*\)", re.S | re.I)
    for path in sorted(MIGRATIONS.glob("*.sql"), reverse=True):
        match = pattern.search(path.read_text(encoding="utf-8"))
        if match:
            return set(re.findall(r"'([a-z_]+)'", match.group(1)))
    raise AssertionError("no migration defines a CHECK on agent_jobs.job_type")


def test_every_job_type_the_code_enqueues_is_accepted_by_the_database():
    """The bug, as one assertion."""
    rejected = _job_types_enqueued() - _job_types_allowed()
    assert not rejected, (
        f"these job types are enqueued by the code and REJECTED by the CHECK "
        f"constraint: {sorted(rejected)}. Every enqueue of one raises "
        f"CheckViolationError at runtime. Add them in a new migration."
    )


def test_every_job_type_the_code_enqueues_is_dispatched_by_the_worker():
    """A type nobody runs is not an error -- it is a scan that never finishes."""
    orphaned = _job_types_enqueued() - _job_types_dispatched()
    assert not orphaned, (
        f"these job types are enqueued but the worker has no branch for them: "
        f"{sorted(orphaned)}. They would sit queued until reaped."
    )


def test_the_integration_api_job_types_specifically_are_allowed():
    """Named on their own so a regression reads as itself rather than a set diff."""
    allowed = _job_types_allowed()
    for job_type in ("consent_api_chain", "consent_webhook"):
        assert job_type in allowed, f"{job_type} is not in the agent_jobs CHECK"


def test_a_job_can_be_cancelled():
    """queue.cancel_jobs_for_scan writes status='cancelled', which the original CHECK
    did not permit. Distinct from 'failed' on purpose: a cancelled job is not an error
    and must not be counted as one."""
    pattern = re.compile(r"check\s*\(\s*status\s+in\s*\((.*?)\)\s*\)", re.S | re.I)
    for path in sorted(MIGRATIONS.glob("*.sql"), reverse=True):
        match = pattern.search(path.read_text(encoding="utf-8"))
        if match:
            statuses = set(re.findall(r"'([a-z_]+)'", match.group(1)))
            assert "cancelled" in statuses, f"agent_jobs.status cannot be 'cancelled': {sorted(statuses)}"
            assert "failed" in statuses, "cancelled must be in ADDITION to failed, not instead of it"
            return
    raise AssertionError("no migration defines a CHECK on agent_jobs.status")


def test_the_parsers_find_what_we_know_exists():
    """A parser that silently matched nothing would make every test here pass."""
    enqueued = _job_types_enqueued()
    for known in ("scan", "analyze", "consent_api_chain"):
        assert known in enqueued, f"parser missed {known}; it is not finding enqueue calls"
    assert "scan" in _job_types_dispatched(), "worker-dispatch parser found nothing"
