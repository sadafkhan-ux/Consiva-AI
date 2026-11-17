"""The scan-data cleanup: what it may and may not touch, and when it refuses to run.

The behaviour against a real database (both agents, history kept, guards, repeatable, integrity) is checked by backend/tools/cleanup_integration.py inside the Docker network, because a fake
database cannot show it. These tests pin the rules that must never change quietly.
"""
import asyncio
import re
from pathlib import Path

import pytest

from app.cleanup import core
from app.cleanup import registry as reg

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "migrations").glob("*.sql")))

#: History and business tables: never in the list of what is cleared.
PROTECTED = ("consent_scans", "ent_scans", "websites", "users", "audit_logs", "actions", "approvals", "report_approvals", "webhook_deliveries", "scan_schedules", "knowledge_documents",
             "knowledge_chunks", "checkpoint_migrations", "agent_jobs", "organizations", "organisations", "schema_migrations")


def test_the_history_and_business_tables_are_never_cleared():
    cleared = reg.all_cleared_tables()
    assert not (cleared & set(PROTECTED)), cleared & set(PROTECTED)
    assert not (cleared & set(reg.NEVER_CLEARED))


def test_every_table_it_clears_exists_in_the_migrations():
    for t in sorted(reg.all_cleared_tables()):
        assert re.search(rf"create table if not exists {t}\b", MIGRATIONS, re.I) or re.search(rf"create table {t}\b", MIGRATIONS, re.I) or t.startswith("checkpoint") or t == "policy_analysis_cache", t


def test_both_agents_are_covered_and_each_keeps_its_scan_row():
    assert {a.key for a in reg.AGENTS} == {"five_page", "enterprise"}
    assert reg.FIVE_PAGE.scans_table == "consent_scans" and reg.ENTERPRISE.scans_table == "ent_scans"
    for agent in reg.AGENTS:
        assert agent.scans_table not in {s.table for s in agent.steps}


def test_the_reports_of_both_agents_are_in_the_list():
    assert "report_pdfs" in {s.table for s in reg.FIVE_PAGE.steps} and "ent_reports" in {s.table for s in reg.ENTERPRISE.steps}


def test_children_come_before_their_parents():
    order = [s.table for s in reg.FIVE_PAGE.steps]
    for child, parent in (("consent_forms", "website_pages"), ("trackers", "website_pages"), ("cookies", "trackers"), ("consent_findings", "agent_runs"), ("agent_run_stages", "agent_runs"),
                          ("consent_recommendations", "consent_findings"), ("checkpoints", "agent_runs")):
        assert order.index(child) < order.index(parent), (child, parent)
    e = [s.table for s in reg.ENTERPRISE.steps]
    assert e.index("ent_analysis_chunks") < e.index("ent_evidence") < e.index("ent_frontier")
    site = [s.table for s in reg.ENTERPRISE_SITE_STEPS]
    assert site.index("ent_policy_chunks") < site.index("ent_policy_versions") < site.index("ent_policy_docs")


def test_the_code_never_alters_the_database_structure():
    for f in ("core.py", "registry.py", "__main__.py"):
        src = (ROOT / "app" / "cleanup" / f).read_text(encoding="utf-8").lower()
        assert not re.search(r"\b(drop|alter|truncate|create)\s+(table|column|schema|index|constraint|database|extension)\b", src), f
        assert "disable trigger" not in src and "session_replication_role" not in src, f


def test_every_delete_is_scoped_to_scans_organisations_or_a_cache():
    for agent in reg.AGENTS:
        for s in agent.steps:
            assert ":ids" in s.where, (agent.key, s.table)
    for s in reg.ENTERPRISE_SITE_STEPS + reg.ENTERPRISE_ORG_STEPS:
        assert ":orgs" in s.where, s.table


def test_the_five_page_code_does_not_import_the_cleanup():
    """The cleanup is outside the five-page agent: nothing in it imports app.cleanup."""
    for sub in ("scanner", "scoring", "rules", "policy", "llm", "rag"):
        for f in (ROOT / "app" / sub).rglob("*.py"):
            assert "app.cleanup" not in f.read_text(encoding="utf-8", errors="ignore"), f


# ---- the target must be verified ---------------------------------------------------------------------------------------------------------------------------------

class FakeDb:
    def __init__(self, name="consiva", recovery=False):
        self.name, self.recovery = name, recovery

    async def scalar(self, stmt, params=None):
        s = str(stmt)
        if "current_database" in s:
            return self.name
        if "pg_is_in_recovery" in s:
            return self.recovery
        if "inet_server_addr" in s:
            return "172.18.0.5"
        return "16.1"


def verify(url="postgresql+asyncpg://u:p@db:5432/consiva", settings=None, db=None, monkeypatch=None):
    monkeypatch.setattr(core, "_database_url", lambda: url)
    return asyncio.run(core.verify_target(db or FakeDb(), settings or core.Settings(confirm="consiva@db")))


def test_the_intended_local_database_is_accepted(monkeypatch):
    monkeypatch.delenv("AGENT_WEBHOOK_URL", raising=False)
    assert verify(monkeypatch=monkeypatch)["database"] == "consiva"


@pytest.mark.parametrize("url,settings,why", [
    ("postgresql+asyncpg://u:p@db.supabase.co:5432/consiva", None, "not in CLEANUP_ALLOWED_DB_HOSTS"),
    ("postgresql+asyncpg://u:p@db:5432/consiva", core.Settings(confirm=""), "CLEANUP_CONFIRM_DATABASE"),
    ("postgresql+asyncpg://u:p@db:5432/consiva", core.Settings(confirm="consiva@elsewhere"), "CLEANUP_CONFIRM_DATABASE"),
    ("postgresql+asyncpg://u:p@db:5432/postgres", core.Settings(confirm="postgres@db"), "CLEANUP_ALLOWED_DB_NAMES"),
])
def test_anything_else_is_refused_with_the_reason(monkeypatch, url, settings, why):
    monkeypatch.delenv("AGENT_WEBHOOK_URL", raising=False)
    with pytest.raises(core.TargetNotVerified, match=why):
        verify(url=url, settings=settings, monkeypatch=monkeypatch)


def test_a_standby_replica_is_refused(monkeypatch):
    monkeypatch.delenv("AGENT_WEBHOOK_URL", raising=False)
    with pytest.raises(core.TargetNotVerified, match="standby"):
        verify(db=FakeDb(recovery=True), monkeypatch=monkeypatch)


def test_a_deployment_that_notifies_the_partner_production_api_is_refused_unless_confirmed(monkeypatch):
    monkeypatch.setenv("AGENT_WEBHOOK_URL", "https://api.consiva.ai/api/v1/webhooks/agents")
    monkeypatch.delenv("CLEANUP_ALLOW_PRODUCTION_WEBHOOK", raising=False)
    with pytest.raises(core.TargetNotVerified, match="partner"):
        verify(monkeypatch=monkeypatch)
    monkeypatch.setenv("CLEANUP_ALLOW_PRODUCTION_WEBHOOK", "yes")
    assert verify(monkeypatch=monkeypatch)["database"] == "consiva"
    monkeypatch.setenv("CLEANUP_ALLOW_PRODUCTION_WEBHOOK", "")
    monkeypatch.setenv("AGENT_WEBHOOK_URL", "https://uatapi.consiva.ai/api/v1/webhooks/agents")
    assert verify(monkeypatch=monkeypatch)["database"] == "consiva"


def test_the_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("CLEANUP_GRACE_MINUTES", "0")
    monkeypatch.setenv("CLEANUP_CONFIRM_DATABASE", "consiva@db")
    monkeypatch.setenv("CLEANUP_ENABLED", "false")
    s = core.Settings.from_env()
    assert (s.grace_minutes, s.confirm, s.enabled) == (0, "consiva@db", False)
