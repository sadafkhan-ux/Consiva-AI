"""The negative test master prompt §9/§12 requires: no code path lets a finding take
effect without passing through human review. `finding_repository.update_finding_status`
(for approve/reject) and `apply_edit` (for edit, which also applies the human's
whitelisted field corrections) are the only sanctioned entry points for changing a
consent_finding's status — this test proves they're only ever called from
review_service.py's approve/reject/edit functions, statically, by walking the actual
syntax tree of every file under app/ (not by convention or code review). Scope/limits:
this proves no OTHER function calls them outside review_service.py; it does not (and
cannot, via static analysis alone) prove no code anywhere does a raw ORM attribute
assignment instead — that's why `create_finding`, `update_finding_status`, and
`apply_edit` are the only three places in finding_repository.py that touch `.status`
at all, both reviewed here.
"""

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"
REPOSITORY_FILE = APP_ROOT / "db" / "repositories" / "finding_repository.py"
REVIEW_SERVICE_FILE = APP_ROOT / "services" / "review_service.py"


def _call_sites(function_name: str) -> list[tuple[Path, int]]:
    hits = []
    for path in APP_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == function_name:
                hits.append((path, node.lineno))
    return hits


def _assigns_status(node: ast.AST) -> bool:
    """True if `node` is an assignment whose target is a `.status` attribute — a
    write, not a read (e.g. `.where(ConsentFinding.status == "pending")` is a
    comparison, not an assignment, and must not count)."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AugAssign) else []
    return any(isinstance(t, ast.Attribute) and t.attr == "status" for t in targets)


SANCTIONED_STATUS_WRITERS = ("create_finding", "update_finding_status", "apply_edit")


def test_finding_repository_only_touches_status_in_three_known_functions():
    """`.status` on a ConsentFinding is WRITTEN in exactly three places in the
    repository: creation (always "pending") and the two sanctioned update functions
    (approve/reject via update_finding_status, edit via apply_edit). If a fourth
    function starts assigning it, this fails loudly rather than silently expanding the
    bypass surface. (Reading `.status` elsewhere — e.g. in a query filter — is fine and
    not what this checks.)"""
    tree = ast.parse(REPOSITORY_FILE.read_text(encoding="utf-8"), filename=str(REPOSITORY_FILE))
    offending_functions = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name not in SANCTIONED_STATUS_WRITERS:
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Assign, ast.AugAssign)) and _assigns_status(inner):
                    offending_functions.append(node.name)
    assert not offending_functions, (
        f"Unexpected function(s) assigning ConsentFinding.status: {offending_functions}"
    )


def test_update_finding_status_and_apply_edit_only_called_from_review_service():
    for fn_name in ("update_finding_status", "apply_edit"):
        hits = _call_sites(fn_name)
        assert hits, f"expected {fn_name} to be called somewhere — did the function get renamed?"
        offending = [(path, line) for path, line in hits if path != REVIEW_SERVICE_FILE]
        assert not offending, (
            f"{fn_name} called outside review_service.py: {offending} — "
            "every finding status change must go through the human-review gate."
        )


def test_review_service_decision_functions_all_change_status():
    """The inverse check: every decision function in review_service.py that's
    supposed to change status actually does, via a sanctioned repository call."""
    tree = ast.parse(REVIEW_SERVICE_FILE.read_text(encoding="utf-8"), filename=str(REVIEW_SERVICE_FILE))
    expected = {
        "approve_finding": "update_finding_status",
        "reject_finding": "update_finding_status",
        "edit_finding": "apply_edit",
    }
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in expected:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                        and inner.func.attr == expected[node.name]:
                    found[node.name] = inner.func.attr
    assert found == expected, f"missing sanctioned status-write call in: {set(expected) - set(found)}"
