"""The Enterprise package is a separate product: it must not reach into the existing score or the existing API/service layers, and nothing existing may depend on it."""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ENT = APP / "enterprise"
# The existing score and the existing business-logic services. The Enterprise API and job layers may use the shared plumbing (auth, database session, job queue) like every other route does.
FORBIDDEN_FOR_ENTERPRISE = ("app.scoring", "app.services")
# The only existing modules that may import the Enterprise package: the two registration points.
REGISTRATION_POINTS = {Path("api/v1/router.py"), Path("jobs/worker.py")}


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_enterprise_never_imports_the_existing_score_or_services():
    bad = [(str(p.relative_to(APP)), m) for p in ENT.rglob("*.py") for m in _imports(p) if m.startswith(FORBIDDEN_FOR_ENTERPRISE)]
    assert bad == [], f"enterprise code reaches into the existing score/services: {bad}"


def test_only_the_two_registration_points_import_the_enterprise_package():
    """api/v1/router.py (one include_router) and jobs/worker.py (one job branch) are the only existing files that know the Enterprise package exists."""
    importers = {p.relative_to(APP) for p in APP.rglob("*.py") if ENT not in p.parents and any(m.startswith("app.enterprise") for m in _imports(p))}
    assert importers == REGISTRATION_POINTS, f"unexpected importers: {importers ^ REGISTRATION_POINTS}"


SCORE_ENGINE_FILES = ("scorecard.py", "domain_score.py", "ten_checks.py", "ten_inputs.py", "disclosure.py")
#: Changes to the 5-page score the OWNER asked for after the freeze (backend/frozen/CHANGES.md). Any other change to these files still fails here, and the frozen manifest test guards them all.
OWNER_APPROVED = ("scorecard.py",)             # 16 Nov 2026: coverage no longer withholds the score


def test_the_existing_score_engines_are_not_changed():
    """The files that COMPUTE the score stay exactly as git has them. (scoring/requirements.py, findings.py and review.py are the requirements reading: statuses and wording, no number.)"""
    import subprocess

    paths = [f"backend/app/scoring/{n}" for n in SCORE_ENGINE_FILES if n not in OWNER_APPROVED]
    out = subprocess.run(["git", "status", "--porcelain", "--", *paths], cwd=APP.parents[1], capture_output=True, text=True)
    if out.returncode != 0:
        import pytest

        pytest.skip("not a git checkout")
    assert out.stdout.strip() == "", f"score engine files changed: {out.stdout}"
