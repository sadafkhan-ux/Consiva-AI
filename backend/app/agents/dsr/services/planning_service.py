"""Action planning (prompt §21).

The rule this module exists to enforce: never go from a search result straight to a
delete. A plan is PROPOSED work -- one action per record, each carrying its source,
target, operation, reason, expected result, risk and whether it needs approval --
and nothing in it has happened yet.

Three properties the tests hold this to:

  * Every piece of evidence produces exactly one action. A record that cannot be
    acted on becomes a `blocked` or `retain` action with a reason, never an absent
    one. An action that vanished from the plan is a record the requester was never
    told about (§47).

  * A blocked action stays in the plan. That is what lets a response say "we deleted
    four records and kept one, because your invoice is under a 7-year retention
    rule" instead of quietly doing four.

  * Nothing here executes. The planner has no connector and no write path at all.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.connectors.authorization import SourceGrant
from app.agents.dsr.rules import constraints as rules
from app.agents.dsr.schemas import case
from app.db.models import DsrAction, DsrActionPlan, DsrEvidence, DsrRequest
from app.db.repositories import dsr_repository

# What each request type proposes to do with a record it found.
_OPERATION_FOR_TYPE = {
    case.ACCESS: case.OP_DISCLOSE,
    case.EXPORT: case.OP_DISCLOSE,
    case.INFORMATION: case.OP_DISCLOSE,
    case.CORRECTION: case.OP_UPDATE_FIELD,
    case.DELETION: case.OP_DELETE_RECORD,
}

_RISK_FOR_OPERATION = {
    case.OP_DISCLOSE: "low",
    case.OP_RETAIN: "low",
    case.OP_NO_OP: "low",
    case.OP_UPDATE_FIELD: "medium",
    case.OP_ANONYMIZE_FIELD: "medium",
    case.OP_DELETE_RECORD: "high",
}


async def build_plan(
    db: AsyncSession,
    request: DsrRequest,
    *,
    grants: dict[str, SourceGrant],
    corrections: dict[str, Any] | None = None,
    retention_rules: tuple[rules.RetentionRule, ...] = (),
) -> tuple[DsrActionPlan, list[DsrAction]]:
    """Turn this case's evidence into a proposed plan.

    `grants` maps a source name to its resolved authorization. `corrections` is the
    {column: new_value} a CORRECTION request asks for -- supplied by the case, never
    inferred from the request text, because guessing what someone meant to change is
    not something a correction may do.

    A previous plan for the same case is superseded, not edited: an approved plan is
    an approval of specific actions, and rewriting it in place would leave that
    approval attached to work nobody agreed to.
    """
    evidence = await dsr_repository.list_evidence(db, request.id, request.org_id)
    previous = await dsr_repository.get_current_plan(db, request.id, request.org_id)
    if previous is not None:
        previous.status = "superseded"
        await db.flush()

    version = await dsr_repository.next_plan_version(db, request.id, request.org_id)
    actions: list[DsrAction] = []
    all_constraints: list[dict] = []

    for item in evidence:
        grant = grants.get(item.source_name)
        action, evaluated = _plan_one(
            request=request, evidence=item, grant=grant,
            corrections=corrections or {}, retention_rules=retention_rules,
        )
        actions.append(action)
        all_constraints.extend(
            {**c.as_dict(), "evidence_id": str(item.id), "table": item.table_name}
            for c in evaluated
        )

    requires_approval = any(a.requires_approval for a in actions)
    plan = await dsr_repository.create_plan(
        db,
        org_id=request.org_id,
        request_id=request.id,
        version=version,
        summary=_summarize(request, actions),
        constraints_evaluated=all_constraints,
        requires_approval=requires_approval,
    )
    for action in actions:
        action.plan_id = plan.id
    await dsr_repository.add_actions(db, actions)
    return plan, actions


def _plan_one(
    *,
    request: DsrRequest,
    evidence: DsrEvidence,
    grant: SourceGrant | None,
    corrections: dict[str, Any],
    retention_rules: tuple[rules.RetentionRule, ...],
) -> tuple[DsrAction, tuple[rules.Constraint, ...]]:
    """One evidence row in, exactly one action out. Never None."""
    intended = _OPERATION_FOR_TYPE.get(request.request_type, case.OP_NO_OP)
    payload = _payload_for(intended, evidence, corrections)

    if grant is None:
        # The source that produced this evidence has since lost its authorization.
        # The record is still reported; it just cannot be acted on.
        return (
            _action(
                request, evidence, operation=case.OP_RETAIN, payload={},
                reason=(
                    f"source {evidence.source_name!r} no longer has a DSR authorization, "
                    "so no action can be taken against this record"
                ),
                expected_result="record left unchanged and reported to the requester",
                status="blocked",
                blocked_reason=f"source {evidence.source_name!r} is not currently authorized",
                requires_approval=True,
            ),
            (),
        )

    evaluated = rules.evaluate(
        operation=intended,
        table_name=evidence.table_name,
        grant=grant,
        payload=payload,
        record_snapshot=evidence.record_snapshot,
        retention_rules=retention_rules,
    )
    effect = rules.verdict(evaluated)

    if effect == rules.EFFECT_BLOCK:
        reason = rules.blocking_reason(evaluated) or "blocked by a constraint"
        return (
            _action(
                request, evidence, operation=case.OP_RETAIN, payload={},
                reason=(
                    f"the requested {request.request_type} cannot be performed on this "
                    f"record: {reason}"
                ),
                expected_result="record retained; the requester is told why",
                status="blocked",
                blocked_reason=reason,
                requires_approval=True,
            ),
            evaluated,
        )

    # A disclosure needs no approval unless something flagged it: reading a record to
    # answer an access request is the request being fulfilled, not a change to review.
    needs_approval = (
        intended in case.MUTATING_OPERATIONS or effect == rules.EFFECT_REVIEW
    )
    return (
        _action(
            request, evidence, operation=intended, payload=payload,
            reason=_reason_for(request, evidence, intended),
            expected_result=_expected_for(intended, evidence, payload),
            status="proposed",
            blocked_reason=None,
            requires_approval=needs_approval,
        ),
        evaluated,
    )


def _payload_for(
    operation: str, evidence: DsrEvidence, corrections: dict[str, Any]
) -> dict[str, Any]:
    """What the write will set. Only columns the case explicitly asked to correct,
    intersected with what this record actually has."""
    if operation != case.OP_UPDATE_FIELD:
        return {}
    snapshot = evidence.record_snapshot or {}
    return {column: value for column, value in corrections.items() if column in snapshot}


def _action(
    request: DsrRequest,
    evidence: DsrEvidence,
    *,
    operation: str,
    payload: dict[str, Any],
    reason: str,
    expected_result: str,
    status: str,
    blocked_reason: str | None,
    requires_approval: bool,
) -> DsrAction:
    return DsrAction(
        id=uuid.uuid4(),
        org_id=request.org_id,
        request_id=request.id,
        plan_id=None,  # set once the plan row exists
        evidence_id=evidence.id,
        source_name=evidence.source_name,
        table_name=evidence.table_name,
        record_reference=evidence.record_reference,
        operation=operation,
        operation_payload=payload,
        reason=reason,
        expected_result=expected_result,
        risk=_RISK_FOR_OPERATION.get(operation, "medium"),
        requires_approval=requires_approval,
        status=status,
        blocked_reason=blocked_reason,
    )


def _reason_for(request: DsrRequest, evidence: DsrEvidence, operation: str) -> str:
    where = f"{evidence.source_name}.{evidence.table_name}"
    matched = f"matched on {evidence.matched_column} ({evidence.match_type})"
    if operation == case.OP_DISCLOSE:
        return f"record in {where} holds the requester's personal data; {matched}"
    if operation == case.OP_UPDATE_FIELD:
        return f"the requester asked for a correction to their record in {where}; {matched}"
    if operation == case.OP_DELETE_RECORD:
        return f"the requester asked for erasure of their record in {where}; {matched}"
    return f"record in {where}; {matched}"


def _expected_for(operation: str, evidence: DsrEvidence, payload: dict[str, Any]) -> str:
    if operation == case.OP_DISCLOSE:
        columns = sorted(evidence.record_snapshot or {})
        return (
            f"the requester receives {len(columns)} field(s) from "
            f"{evidence.table_name}: {', '.join(columns)}"
            if columns
            else f"the existence of a record in {evidence.table_name} is disclosed"
        )
    if operation == case.OP_UPDATE_FIELD:
        return f"{', '.join(sorted(payload))} updated on the matched record, then read back to confirm"
    if operation == case.OP_DELETE_RECORD:
        return f"the matched record is removed from {evidence.table_name}, then confirmed absent"
    return "no change to the source"


def _summarize(request: DsrRequest, actions: list[DsrAction]) -> str:
    """A one-paragraph plan summary a reviewer reads before approving.

    Deterministic prose assembled from counts -- not model output. A summary that
    disagreed with the actions beneath it would be worse than none.
    """
    if not actions:
        return (
            f"No record matched the identifiers supplied for case {request.reference}. "
            "There is nothing to act on; the requester should be told that no data was found."
        )
    by_operation: dict[str, int] = {}
    for action in actions:
        by_operation[action.operation] = by_operation.get(action.operation, 0) + 1
    blocked = sum(1 for a in actions if a.status == "blocked")
    parts = [f"{count} x {op}" for op, count in sorted(by_operation.items())]
    summary = (
        f"Case {request.reference} ({request.request_type}): {len(actions)} action(s) "
        f"proposed across {len({a.source_name for a in actions})} source(s) -- {', '.join(parts)}."
    )
    if blocked:
        summary += (
            f" {blocked} action(s) are blocked by a constraint and will NOT be executed; "
            "their reasons must be included in the response to the requester."
        )
    return summary
