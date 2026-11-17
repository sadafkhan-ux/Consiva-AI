"""Migration 0050 stays additive, isolated by tenant, and in step with the models."""
import re
from pathlib import Path

import pytest

from app.enterprise.models import EntEvidence, EntFrontier, EntScan

SQL = (Path(__file__).resolve().parents[2] / "migrations" / "0050_enterprise_foundation.sql").read_text(encoding="utf-8")
TABLES = ("ent_scans", "ent_frontier", "ent_evidence")
LATER = "\n".join((Path(__file__).resolve().parents[2] / "migrations" / f).read_text(encoding="utf-8") for f in ("0055_enterprise_assessment.sql", "0056_enterprise_frontier_priority.sql", "0057_enterprise_page_tracking.sql", "0058_enterprise_policy_kb.sql", "0059_enterprise_slug_triage.sql", "0060_enterprise_retention.sql"))


def _added_later(table: str) -> set[str]:
    """Columns that migration 0055 adds to a table 0050 created."""
    cols: set[str] = set()
    for m in re.finditer(rf"alter table {table}\s+(.*?);", LATER, re.S):
        cols |= set(re.findall(r"add column if not exists (\w+)", m.group(1)))
    return cols


def _block(table: str) -> str:
    m = re.search(rf"create table if not exists {table} \((.*?)\n\);", SQL, re.S)
    assert m, table
    return m.group(1)


@pytest.mark.parametrize("model", [EntScan, EntFrontier, EntEvidence])
def test_every_model_column_is_in_the_migration(model):
    block = _block(model.__tablename__)
    for col in model.__table__.columns:
        assert re.search(rf"^\s+{col.name}\s", block, re.M) or col.name in _added_later(model.__tablename__), f"{model.__tablename__}.{col.name} missing from the migrations"


@pytest.mark.parametrize("table", TABLES)
def test_row_level_security_is_enabled_forced_and_has_a_policy(table):
    assert f"alter table {table} enable row level security;" in SQL
    assert f"alter table {table} force row level security;" in SQL
    assert re.search(rf"create policy tenant_isolation on {table}\b", SQL)


def test_the_migration_only_creates_ent_tables():
    created = set(re.findall(r"create table if not exists (\w+)", SQL))
    assert created == set(TABLES)
    assert not re.search(r"alter table (?!ent_)\w+", SQL), "the migration must not alter an existing table"
    assert not re.search(r"drop |truncate |delete from", SQL, re.I)


def test_failures_and_exclusions_must_say_why():
    assert "ent_frontier_failed_has_error" in SQL and "ent_frontier_excluded_has_reason" in SQL and "ent_scans_failed_has_error" in SQL
