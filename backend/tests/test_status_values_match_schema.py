"""Every status value the code writes must be one the database accepts.

THE PATTERN THIS EXISTS TO STOP

An enumerated CHECK constraint is invisible to the Python that writes into it. A new
value is a plain string: it type-checks, it reads fine in review, and it passes every
unit test that never opens a database. It fails only at runtime, in the one place
nobody is watching.

That has now happened three times in this codebase, each found by a 500 on a real call
rather than by any test:

    0021  agent_run_stages.stage   rejected 'rule_findings_generated'
    0023  agent_jobs.job_type      rejected 'consent_api_chain'
    0024  consent_scans.status     rejected 'cancelled'

The first two have their own guards (test_stage_names_match_schema,
test_job_types_match_schema). This one generalises to status columns: it reads the
enumerated CHECKs out of the migrations and compares them against the literal status
strings the application assigns. No database needed.
"""

import ast
import pathlib
import re

BACKEND = pathlib.Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
MIGRATIONS = BACKEND / "migrations"

# `status text not null ... check (status in ('a','b'))` inside a CREATE TABLE, and
# `alter table t add constraint ... check (status in ('a','b'))` when one is widened.
_CHECK_IN = r"check\s*\(\s*status\s+in\s*\((.*?)\)\s*\)"
_VALUE = re.compile(r"'([a-z_]+)'")


def _allowed(table: str) -> set[str]:
    """The status values ONE named table permits, as the LAST migration to define them
    leaves them.

    Reverse filename order matters: these constraints are dropped and re-added when
    widened, so scanning forwards -- or taking a union across files -- would still look
    correct after a migration NARROWED the list.
    """
    patterns = (
        re.compile(r"alter\s+table\s+" + table + r"\b.*?" + _CHECK_IN, re.S | re.I),
        re.compile(r"create\s+table[^;]*?\b" + table + r"\b.*?" + _CHECK_IN, re.S | re.I),
    )
    for path in sorted(MIGRATIONS.glob("*.sql"), reverse=True):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return set(_VALUE.findall(match.group(1)))
    return set()


def _all_allowed_status_values() -> set[str]:
    """Every value any enumerated `status` CHECK in any migration permits.

    Collected across ALL tables rather than a hand-listed few. The first version of
    this test named three tables and immediately failed on twelve perfectly legal
    values -- 'delivered', 'paused', 'executing' and friends -- belonging to
    dsr_requests, agent_runs, ropa_discovery_runs and the rest. A guard that must be
    kept in sync with a list of tables is a guard that goes stale; this one cannot.
    """
    values: set[str] = set()
    for path in MIGRATIONS.glob("*.sql"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(_CHECK_IN, text, re.S | re.I):
            values |= set(_VALUE.findall(match.group(1)))
        # The form pg_dump and some older migrations use.
        for match in re.finditer(r"check\s*\(\s*status\s*=\s*any\s*\(array\[(.*?)\]", text, re.S | re.I):
            values |= set(_VALUE.findall(match.group(1)))
    return values


def _assigned_status_literals() -> set[str]:
    """Every `<something>.status = "literal"` the application performs.

    Deliberately not narrowed to one ORM class: attribute assignment carries no type
    here (`scan.status = "cancelled"` on a variable merely named `scan`), so this
    over-collects on purpose. Over-collecting is the safe direction -- it can only
    produce an extra value to check, never hide one.
    """
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "status":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        found.add(node.value.value)
    return found


def test_every_status_literal_the_code_assigns_is_accepted_somewhere():
    """The whole class of bug, as one assertion.

    Compared against the union across every status CHECK, because the assignment
    `x.status = "cancelled"` does not say which table `x` is. A value that NO table
    accepts is unambiguously wrong and raises at runtime; one that some table accepts
    is not something this test can adjudicate without guessing, and guessing is how a
    guard starts producing noise instead of signal.
    """
    allowed = _all_allowed_status_values()
    assert allowed, "no enumerated status CHECK found in any migration"

    orphaned = {v for v in _assigned_status_literals() if v not in allowed}
    assert not orphaned, (
        f"these status values are assigned in the code and accepted by NO status CHECK "
        f"in any migration: {sorted(orphaned)}. Each raises CheckViolationError at "
        f"runtime. Allowed across all tables: {sorted(allowed)}"
    )


def test_a_scan_can_be_cancelled():
    """Named on its own so the regression reads as itself. The API's cancel endpoint
    returned 500 on its first real call because of this."""
    statuses = _allowed("consent_scans")
    assert "cancelled" in statuses, f"consent_scans.status cannot be 'cancelled': {sorted(statuses)}"


def test_cancelled_is_in_addition_to_failed_not_instead_of_it():
    """A cancelled scan was stopped on purpose and nothing went wrong. Folding it into
    'failed' would count deliberate stops as errors on every dashboard -- and would tell
    a caller their scan broke when they are the one who stopped it."""
    for table in ("consent_scans", "agent_jobs"):
        statuses = _allowed(table)
        assert {"cancelled", "failed"} <= statuses, (
            f"{table}.status must allow both, has {sorted(statuses)}"
        )


def test_the_parsers_find_what_we_know_exists():
    """A parser matching nothing would make every test here pass vacuously."""
    assert "completed" in _allowed("consent_scans")
    assert "queued" in _allowed("agent_jobs")
    assert {"paused", "delivered"} <= _all_allowed_status_values(), (
        "the all-tables collector is missing other tables' status enums"
    )
    assert "cancelled" in _assigned_status_literals(), "assignment parser found no status literals"
