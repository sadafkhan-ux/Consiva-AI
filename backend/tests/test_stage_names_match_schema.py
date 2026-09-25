"""Every stage the code writes must be a stage the database accepts.

WHY THIS EXISTS
---------------
`create_rule_findings` records itself as 'rule_findings_generated'. The CHECK
constraint in migration 0003 listed ten stage names and that was not one of them,
so the insert was rejected -- CheckViolationError -- every single time the node ran.

The fallback's whole purpose is to leave an honest record when the model fails:
`source='rules'`, `narrative_missing=True`, the reason, the rule ids. None of it was
ever written. On scan 63497fc6 (hubspot.com) the three findings reached the database
and the audit trail showed no trace of where they came from.

What makes this worth a dedicated test is how it got through. The existing suite in
test_rule_findings_fallback.py has seventeen tests over this exact node and every one
of them passed, because they read the source and assert what it SAYS. Not one of them
could see that the string it writes is rejected by the schema it writes into. A test
that inspects code cannot catch a disagreement between code and database.

So this asserts the agreement directly, and needs no database to do it: the stage
names in the Python and the stage names in the migrations, compared as sets.
"""

import ast
import pathlib
import re

BACKEND = pathlib.Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
MIGRATIONS = BACKEND / "migrations"


def _stage_names_written_by_code() -> set[str]:
    """Second positional argument of every track_stage(...) call, when it is a literal.

    Parsed rather than grepped: these calls wrap across lines, and a regex that only
    matched the single-line form would quietly report a smaller set than really
    exists -- under-reporting in precisely the direction that hides a bug.
    """
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name != "track_stage" or len(node.args) < 2:
                continue
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
    return found


def _stage_names_allowed_by_schema() -> set[str]:
    """The stage CHECK as the LAST migration to define it leaves it.

    Later migrations drop and re-add the constraint, so the winning definition is the
    one in the highest-numbered file that mentions it -- not the union of all of them,
    which would still look correct after a migration narrowed the list.
    """
    pattern = re.compile(r"check\s*\(\s*stage\s+in\s*\((.*?)\)\s*\)", re.S | re.I)
    for path in sorted(MIGRATIONS.glob("*.sql"), reverse=True):
        match = pattern.search(path.read_text(encoding="utf-8"))
        if match:
            return set(re.findall(r"'([a-z_]+)'", match.group(1)))
    raise AssertionError("no migration defines a CHECK on agent_run_stages.stage")


def test_every_stage_the_code_writes_is_accepted_by_the_database():
    """The bug, as one assertion."""
    writes = _stage_names_written_by_code()
    allowed = _stage_names_allowed_by_schema()
    rejected = writes - allowed
    assert not rejected, (
        f"these stage names are written by the code and REJECTED by the CHECK "
        f"constraint: {sorted(rejected)}. Every write of one raises "
        f"CheckViolationError. Add them in a new migration."
    )


def test_the_fallback_stage_specifically_is_allowed():
    """Named on its own so a regression reads as itself rather than as a set diff."""
    assert "rule_findings_generated" in _stage_names_allowed_by_schema()


def test_the_code_really_does_write_the_fallback_stage():
    """Guards the test above from passing vacuously. If the node stopped writing the
    stage, the set-difference test would go green while the audit record vanished."""
    assert "rule_findings_generated" in _stage_names_written_by_code()


def test_the_parser_finds_the_stages_we_know_exist():
    """A parser that silently matched nothing would make every test here pass."""
    writes = _stage_names_written_by_code()
    for known in ("url_validation", "website_scan", "llm_analysis", "audit_saved"):
        assert known in writes, f"parser missed {known}; it is not finding track_stage calls"
