"""
What this process runs, in one place: the rules versions, the scoring engine and the approval setting.

Logged when the API and the worker start, and returned by GET /api/v1/system/version, so two deployments (or the API and the worker of one deployment) can be compared at a glance. A worker that
runs older code than the API shows a different line in its start-up log. Read-only: nothing here changes behaviour.
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.config import get_settings

_STARTED = datetime.now(UTC)


def snapshot() -> dict[str, str | bool]:
    from app.scoring import domain_score, requirements, scorecard

    settings = get_settings()
    return {
        "scorecard": scorecard.SCORECARD_VERSION,
        "scanner_score": domain_score.SCORE_VERSION,
        "requirements": str(getattr(requirements, "REQUIREMENTS_VERSION", "")),
        "scoring_engine": settings.scoring_engine,
        "report_require_approval": settings.report_require_approval,
        "report_quality_owner": settings.report_quality_owner,
        "process_started_at": _STARTED.isoformat(timespec="seconds"),
    }


def describe() -> str:
    s = snapshot()
    return f"scorecard {s['scorecard']}, scanner score {s['scanner_score']}, requirements {s['requirements']}, scoring engine {s['scoring_engine']}, report approval {'required' if s['report_require_approval'] else 'not required'}"
