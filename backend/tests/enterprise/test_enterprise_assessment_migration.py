"""Migration 0055 stays additive, isolated by tenant, and in step with the models."""
import re
from pathlib import Path

import pytest

from app.enterprise.models import EntAnalysisChunk, EntAssessment, EntAuditEvent, EntFinding, EntReport, EntScoreResult

SQL = (Path(__file__).resolve().parents[2] / "migrations" / "0055_enterprise_assessment.sql").read_text(encoding="utf-8")
_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
#: Columns later migrations add to these tables (0058 the policy knowledge base, 0063 the report's method 3.0.0 link).
LATER = "\n".join((_MIGRATIONS / f).read_text(encoding="utf-8") for f in ("0058_enterprise_policy_kb.sql", "0063_enterprise_report_method3_link.sql"))
NEW = ("ent_assessments", "ent_score_results", "ent_findings", "ent_analysis_chunks", "ent_reports", "ent_audit_events")


def _block(table: str) -> str:
    m = re.search(rf"create table if not exists {table} \((.*?)\n\);", SQL, re.S)
    assert m, table
    return m.group(1)


@pytest.mark.parametrize("model", [EntAssessment, EntScoreResult, EntFinding, EntAnalysisChunk, EntReport, EntAuditEvent])
def test_every_model_column_is_in_the_migration(model):
    block = _block(model.__tablename__)
    for col in model.__table__.columns:
        later = [c for m in re.findall(rf"alter table {model.__tablename__}\s+(.*?);", LATER, re.S) for c in re.findall(r"add column if not exists (\w+)", m)]
        assert re.search(rf"^\s+{col.name}\s", block, re.M) or col.name in later, f"{model.__tablename__}.{col.name} missing from the migration"


def test_the_migration_creates_only_the_new_ent_tables_and_only_adds_to_the_old_ones():
    assert set(re.findall(r"create table if not exists (\w+)", SQL)) == set(NEW)
    altered = set(re.findall(r"alter table (\w+)", SQL))
    assert altered <= set(NEW) | {"ent_scans", "ent_frontier"}, "no table outside the Enterprise product is altered"
    assert not re.search(r"drop table|truncate|delete from|drop column", SQL, re.I)


@pytest.mark.parametrize("table", NEW)
def test_row_level_security_is_enabled_and_forced_on_every_new_table(table):
    assert f"alter table {table} enable row level security;" in SQL and f"alter table {table} force row level security;" in SQL
    assert table in SQL.split("foreach t in array")[1].split("loop")[0], "the table is in the list that gets the tenant_isolation policy"


def test_there_is_one_current_assessment_per_parameter_and_history_is_kept():
    assert "ent_assessments_one_current" in SQL and "where status = 'current'" in SQL


def test_a_review_names_who_and_why():
    assert "ent_assessments_review_complete" in SQL and "reviewed_by is not null and review_reason is not null" in SQL
