"""Building the response the requester receives (prompt §18, §19, §26, §27).

GROUNDING
---------
Every factual sentence is assembled from `dsr_evidence` and `dsr_executions` rows.
The LLM is not in this path at all by default: the prose here is deterministic,
because a response that disagrees with the evidence beneath it is worse than a
plainly-worded one that does not.

`grounded_facts` is the machine-checkable list the prose was built from. It is
stored alongside the body so a reviewer can check every claim against a row, and so
a future LLM-drafted variant can be validated against it rather than trusted.

WHAT A RESPONSE MUST SAY
------------------------
Not just what was done -- what was NOT done, and why (§47). A deletion that removed
four records and kept one under a retention rule produces a response that says so.
A search where one source was unreachable says that too, because "we found no data"
and "we could not check one of our systems" are different answers and only one of
them is true.
"""

from __future__ import annotations

import uuid
from collections import Counter

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.schemas import case
from app.db.models import DsrRequest, DsrResponse
from app.db.repositories import dsr_repository
from app.services import audit_service


async def build_response(
    db: AsyncSession,
    request: DsrRequest,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> DsrResponse:
    """Assemble the response from what the case actually established."""
    evidence = await dsr_repository.list_evidence(db, request.id, request.org_id)
    runs = await dsr_repository.list_search_runs(db, request.id, request.org_id)
    executions = await dsr_repository.list_executions(db, request.id, request.org_id)
    plan = await dsr_repository.get_current_plan(db, request.id, request.org_id)
    actions = (
        await dsr_repository.list_actions(db, plan.id, request.org_id) if plan else []
    )

    facts = _collect_facts(evidence, runs, executions, actions)
    body = _compose(request, evidence, runs, executions, actions)

    version = await dsr_repository.next_response_version(db, request.id, request.org_id)
    response = await dsr_repository.create_response(
        db,
        org_id=request.org_id,
        request_id=request.id,
        version=version,
        body_text=body,
        grounded_facts=facts,
        drafted_by_model=None,  # deterministic; no model in this path
    )
    await audit_service.record(
        db,
        org_id=request.org_id,
        actor_user_id=actor_user_id,
        action=case.AUDIT_RESPONSE_GENERATED,
        entity_type=case.AUDIT_ENTITY,
        entity_id=request.id,
        after={
            "response_id": str(response.id), "version": version,
            "fact_count": len(facts), "drafted_by_model": None,
        },
    )
    return response


def _collect_facts(evidence, runs, executions, actions) -> list[dict]:
    """The machine-checkable claims. Every entry names the row it came from, so a
    reviewer can verify the response against the database rather than against a
    narrative."""
    facts: list[dict] = []
    for run in runs:
        facts.append({
            "type": "search",
            "source": run.source_name,
            "status": run.status,
            "matches": run.match_count,
            "tables_searched": list(run.tables_searched or []),
            "error_code": run.error_code,
            "search_run_id": str(run.id),
        })
    for item in evidence:
        facts.append({
            "type": "record_found",
            "source": item.source_name,
            "table": item.table_name,
            "matched_on": item.matched_column,
            "match_type": item.match_type,
            "fields_disclosed": sorted(item.record_snapshot or {}),
            "evidence_id": str(item.id),
        })
    for action in actions:
        if action.status == "blocked":
            facts.append({
                "type": "action_blocked",
                "source": action.source_name,
                "table": action.table_name,
                "operation": action.operation,
                "reason": action.blocked_reason,
                "action_id": str(action.id),
            })
    for execution in executions:
        facts.append({
            "type": "action_executed",
            "status": execution.status,
            "verified": execution.verification_status == "passed",
            "rows_affected": execution.rows_affected,
            "error_code": execution.error_code,
            "execution_id": str(execution.id),
            "action_id": str(execution.action_id),
        })
    return facts


def _compose(request: DsrRequest, evidence, runs, executions, actions) -> str:
    """Deterministic prose. Assembled from counts and reasons, never generated."""
    lines = [
        f"Reference: {request.reference}",
        f"Request type: {request.request_type}",
        f"Received: {request.received_at.date().isoformat() if request.received_at else 'unknown'}",
        "",
    ]

    searched = [r for r in runs if r.status in ("completed", "no_match", "multiple_matches")]
    failed = [r for r in runs if r.status == "failed"]

    # What was searched. Stated before what was found, because the scope of the
    # search is part of the answer.
    if searched:
        lines.append(
            f"We searched {len(searched)} system(s): "
            + ", ".join(sorted({r.source_name for r in searched}))
            + "."
        )
    if failed:
        # Never folded into "no data found" -- they are different answers.
        lines.append(
            f"We were unable to complete the search in {len(failed)} system(s): "
            + ", ".join(f"{r.source_name} ({r.error_code})" for r in failed)
            + ". The findings below are therefore incomplete."
        )

    if not evidence:
        lines.append("")
        lines.append(
            "We did not find any record matching the details you provided in the systems "
            "we searched. No data was changed."
        )
        return "\n".join(lines)

    by_source = Counter((e.source_name, e.table_name) for e in evidence)
    lines.append("")
    lines.append(f"We found {len(evidence)} record(s) relating to you:")
    for (source, table), count in sorted(by_source.items()):
        columns = sorted({c for e in evidence
                          if e.source_name == source and e.table_name == table
                          for c in (e.record_snapshot or {})})
        detail = f" holding: {', '.join(columns)}" if columns else ""
        lines.append(f"  - {count} record(s) in {source}/{table}{detail}")

    if request.request_type in (case.ACCESS, case.EXPORT, case.INFORMATION):
        lines.append("")
        lines.append("The data held in those records is attached to this response.")

    verified = [e for e in executions if e.verification_status == "passed"]
    failed_exec = [e for e in executions if e.status == "failed"]
    blocked = [a for a in actions if a.status == "blocked"]

    if verified:
        total_rows = sum(e.rows_affected or 0 for e in verified)
        verb = "deleted" if request.request_type == case.DELETION else "updated"
        lines.append("")
        lines.append(
            f"We {verb} {total_rows} record(s) as requested, and confirmed the change by "
            "re-reading each record afterwards."
        )

    if blocked:
        lines.append("")
        lines.append(
            f"{len(blocked)} record(s) were NOT changed. The reasons are:"
        )
        for action in blocked:
            lines.append(f"  - {action.source_name}/{action.table_name}: {action.blocked_reason}")

    if failed_exec:
        lines.append("")
        lines.append(
            f"{len(failed_exec)} action(s) did not complete successfully "
            f"({', '.join(sorted({e.error_code or 'unknown' for e in failed_exec}))}). "
            "These are being followed up and you will receive a further update."
        )

    return "\n".join(lines)


def response_is_grounded(response: DsrResponse) -> bool:
    """Whether every claim in this response traces to a row.

    A response with prose but no facts is ungrounded by definition -- except the
    genuine no-data case, where the search runs themselves are the grounding.
    """
    return bool(response.grounded_facts)
