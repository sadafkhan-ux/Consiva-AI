"""The migration, and the rule that monitoring stays out of the scanner's way."""
import ast
import re
from pathlib import Path

import pytest

from app.monitoring.models import SystemAlert, SystemHealth

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
SQL = (ROOT / "migrations" / "0053_system_monitoring.sql").read_text(encoding="utf-8")
CODE = "\n".join(line for line in SQL.splitlines() if not line.strip().startswith("--"))


def _block(table):
    m = re.search(rf"create table if not exists {table} \((.*?)\n\);", SQL, re.S)
    assert m, table
    return m.group(1)


@pytest.mark.parametrize("model", [SystemHealth, SystemAlert])
def test_every_model_column_is_in_the_migration(model):
    block = _block(model.__tablename__)
    for col in model.__table__.columns:
        assert re.search(rf"^\s+{col.name}\s", block, re.M), f"{model.__tablename__}.{col.name}"


def test_there_is_at_most_one_live_alert_per_source_and_the_migration_only_adds():
    assert "system_alerts_one_live_per_source" in SQL and "where status in ('open', 'acknowledged')" in SQL
    assert not re.search(r"alter table (?!system_)\w+", CODE)
    assert not re.search(r"drop table|truncate|delete from", CODE, re.I)
    assert "org_id" not in CODE            # operational data about the deployment, not tenant data


def _imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_monitoring_imports_no_scoring_and_no_scan_logic():
    forbidden = ("app.scoring", "app.services", "app.scanner", "app.enterprise", "app.agents")
    bad = [(p.name, m) for p in (APP / "monitoring").glob("*.py") for m in _imports(p) if m.startswith(forbidden)]
    assert bad == []


def test_only_the_router_and_the_worker_know_about_monitoring():
    importers = {p.relative_to(APP).as_posix() for p in APP.rglob("*.py") if "monitoring" not in p.parts and any(m.startswith("app.monitoring") for m in _imports(p))}
    assert importers <= {"api/v1/router.py", "jobs/worker.py"}, importers


def test_every_monitoring_route_is_admin_only():
    src = (APP / "monitoring" / "api.py").read_text(encoding="utf-8")
    handlers = re.findall(r"@router\.(?:get|post)\([^\n]*\)\nasync def (\w+)\(", src)
    assert len(handlers) == 4
    for name in handlers:
        body = src.split(f"async def {name}(")[1].split("\n@router")[0]
        assert "_admin(user)" in body, f"{name} does not check for an administrator"


def test_no_function_in_the_worker_imports_a_name_that_the_module_already_imports():
    """The regression of 13 Nov: importing `monitoring_service` inside the maintenance loop made Python treat that name as local for the whole function, so the existing
    Continuous Monitoring dispatcher (the module-level `monitoring_service`) failed with UnboundLocalError on every tick."""
    tree = ast.parse((APP / "jobs" / "worker.py").read_text(encoding="utf-8"))
    module_level = {a.asname or a.name.split(".")[0] for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    clashes = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                clashes += [(fn.name, a.asname or a.name) for a in node.names if (a.asname or a.name.split(".")[0]) in module_level]
    assert clashes == [], clashes
