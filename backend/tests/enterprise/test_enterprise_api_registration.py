"""The Enterprise routes are registered, answer only to callers who are allowed, and the job type is allowed by the queue's constraint."""
from pathlib import Path

from app.main import app

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def test_the_enterprise_routes_are_registered_under_their_own_prefix():
    paths = set(app.openapi()["paths"])
    enterprise = {p for p in paths if p.startswith("/api/v1/enterprise")}
    assert {"/api/v1/enterprise/scans", "/api/v1/enterprise/scans/{scan_id}/status", "/api/v1/enterprise/scans/{scan_id}/coverage",
            "/api/v1/enterprise/scans/{scan_id}/cancel", "/api/v1/enterprise/scans/{scan_id}/resume"} <= enterprise


def test_the_existing_consent_routes_are_still_there():
    paths = set(app.openapi()["paths"])
    assert "/api/v1/consent-agent/scans" in paths and "/api/v1/consent/scans/{scan_id}/assessment" in paths


def test_the_queue_constraint_allows_the_enterprise_job_type_and_keeps_every_other():
    sql = (MIGRATIONS / "0051_enterprise_scan_job_type.sql").read_text(encoding="utf-8")
    previous = (MIGRATIONS / "0044_render_reports_job_type.sql").read_text(encoding="utf-8")
    import re

    def types(text):
        return set(re.findall(r"'(\w+)'", re.search(r"check \(job_type in \((.*?)\)\);", text, re.S).group(1)))

    old, new = types(previous), types(sql)
    assert old <= new and new - old == {"enterprise_scan"}


def test_the_worker_dispatches_the_enterprise_job():
    from app.jobs import worker

    assert 'job.job_type == "enterprise_scan"' in Path(worker.__file__).read_text(encoding="utf-8")
