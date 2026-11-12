"""A stage left "running" by a finished scan is closed; one belonging to a live scan, or a young one, never is."""
from datetime import timedelta

from sqlalchemy.dialects import postgresql

from app.db.repositories import stage_repository


class _Db:
    def __init__(self, rowcount=1):
        self.statements, self._rowcount = [], rowcount

    async def execute(self, stmt):
        self.statements.append(stmt)
        return type("R", (), {"rowcount": self._rowcount})()


async def test_only_old_running_stages_of_finished_scans_are_closed():
    db = _Db(rowcount=2)
    closed = await stage_repository.close_orphan_stages(db)
    assert closed == 2
    sql = str(db.statements[0].compile(dialect=postgresql.dialect())).lower()
    assert "update agent_run_stages" in sql and "status=" in sql.replace(" ", "")
    assert "agent_run_stages.status = %(status_1)s" in sql and "started_at <" in sql
    assert "consent_scans.status in" in sql and "scan_id in (select" in sql


def test_the_default_age_is_hours_not_minutes():
    assert stage_repository.ORPHAN_STAGE_AFTER >= timedelta(hours=1)        # a legitimately long stage is never cut short
