"""What actually happened to a case, stage by stage, and how it should read to a portal.

A single status word hides the difference between "we found records", "a reviewer approved
the plan", "the write ran" and "a read-back confirmed it" -- and that difference is exactly
what a portal showing `Resolved` against an agent showing `Failed` could not see. This module
reports the stages separately and derives ONE closure verdict from them, so a portal can
show "resolved" only when the verdict is `verified_complete`.

Everything here is read-only. Nothing in this module changes a case, and the reconciliation
helper reports a disagreement rather than "fixing" either side: which system is right is a
decision for a person, and silently overwriting the portal's record would hide the evidence.
"""

from __future__ import annotations

from typing import Any

from app.agents.dsr.schemas import case
from app.agents.dsr.services import lifecycle

# Closure verdicts. `verified_complete` is the only one a portal may show as "resolved".
CLOSURE_OPEN = "open"
CLOSURE_VERIFIED_COMPLETE = "verified_complete"
CLOSURE_NOTHING_TO_DO = "completed_nothing_to_do"
CLOSURE_PARTIAL = "partially_complete"
CLOSURE_FAILED = "failed"
CLOSURE_WAITING_FOR_SETUP = "waiting_for_setup"
CLOSURE_ENDED_UNFULFILLED = "ended_unfulfilled"  # rejected / cancelled / expired


def search_stage(runs: list[Any], evidence_count: int) -> dict[str, Any]:
    """How the search went, without letting evidence stand in for success."""
    if not runs:
        return {"state": "not_started", "sources_ok": 0, "sources_failed": 0, "records_found": 0}
    failed = [r for r in runs if r.status == "failed"]
    answered = [r for r in runs if r.status in ("completed", "no_match", "multiple_matches")]
    if failed and not answered:
        state = "failed"
    elif failed:
        state = "partial"
    elif evidence_count == 0:
        state = "no_match"
    else:
        state = "succeeded"
    return {
        "state": state, "sources_ok": len(answered), "sources_failed": len(failed),
        "records_found": evidence_count,
    }


def approval_stage(actions: list[Any]) -> dict[str, int]:
    needing = [a for a in actions if a.requires_approval]
    return {
        "required": len(needing),
        "approved": sum(1 for a in needing if a.status in ("approved", "executing", "executed")),
        "rejected": sum(1 for a in needing if a.status == "rejected"),
        "pending": sum(1 for a in needing if a.status == "proposed"),
    }


def execution_stage(actions: list[Any], executions: list[Any]) -> dict[str, int]:
    """`executed_verified` counts an action only when its recorded verification PASSED -- an
    action marked executed whose read-back is missing or failed is not verified."""
    passed = {e.action_id for e in executions if e.verification_status == "passed"}
    return {
        "executed_verified": sum(1 for a in actions if a.status == "executed" and a.id in passed),
        "executed_unverified": sum(1 for a in actions if a.status == "executed" and a.id not in passed),
        "failed": sum(1 for a in actions if a.status == "failed"),
        "blocked": sum(1 for a in actions if a.status == "blocked"),
        "rejected": sum(1 for a in actions if a.status == "rejected"),
        "awaiting": sum(
            1 for a in actions
            if a.status in ("approved", "executing")
            or (a.status == "proposed" and not a.requires_approval)
        ),
    }


def closure(status: str, error_code: str | None, execution: dict[str, int]) -> str:
    """One verdict from the stored status and the execution counts."""
    if status == case.COMPLETED:
        clean = not any(execution[k] for k in ("failed", "blocked", "rejected", "awaiting", "executed_unverified"))
        if not clean:
            # COMPLETED with unresolved work should be impossible (/complete refuses it); if it
            # ever happens it must not read as a clean success.
            return CLOSURE_PARTIAL
        return CLOSURE_VERIFIED_COMPLETE if execution["executed_verified"] else CLOSURE_NOTHING_TO_DO
    if status == case.PARTIALLY_COMPLETED:
        return CLOSURE_PARTIAL
    if status == case.FAILED:
        return (
            CLOSURE_WAITING_FOR_SETUP
            if lifecycle.public_status(status, error_code) == "WAITING_FOR_SETUP"
            else CLOSURE_FAILED
        )
    if status in (case.REJECTED, case.CANCELLED, case.EXPIRED):
        return CLOSURE_ENDED_UNFULFILLED
    return CLOSURE_OPEN


def build_outcome(request: Any, runs: list[Any], evidence_count: int, actions: list[Any], executions: list[Any]) -> dict[str, Any]:
    execution = execution_stage(actions, executions)
    verdict = closure(request.status, request.error_code, execution)
    return {
        "request_id": str(request.id),
        "reference": request.reference,
        "public_status": lifecycle.public_status(request.status, request.error_code),
        "status": request.status,
        "error_code": request.error_code,
        "search": search_stage(runs, evidence_count),
        "approval": approval_stage(actions),
        "execution": execution,
        "closure": verdict,
        # What a portal may say. "resolved" is reserved for a verified outcome; finding data,
        # drafting a response, or attempting an action never earns it.
        "portal_may_show_resolved": verdict in (CLOSURE_VERIFIED_COMPLETE, CLOSURE_NOTHING_TO_DO),
        "closed_at": request.closed_at.isoformat() if getattr(request, "closed_at", None) else None,
    }


# ── Portal status contract (reconciliation, read-only) ───────────────────────────
#
# The portal's own vocabulary is not defined in this repository. This table is the DSR side of
# the CONTRACT: for each portal status, the agent public statuses it is consistent with. A
# portal status not listed here is reported as unmapped, never guessed.
PORTAL_EXPECTATIONS: dict[str, frozenset[str]] = {
    "resolved": frozenset({"COMPLETED"}),
    "partially_resolved": frozenset({"PARTIALLY_COMPLETED"}),
    "partial": frozenset({"PARTIALLY_COMPLETED"}),
    "closed": frozenset({"COMPLETED", "PARTIALLY_COMPLETED", "REJECTED", "CANCELLED", "EXPIRED"}),
    "open": frozenset({"PENDING", "VERIFICATION_REQUIRED", "SEARCHING", "AWAITING_APPROVAL", "EXECUTING", "ESCALATED"}),
    "in_progress": frozenset({"SEARCHING", "EXECUTING"}),
    "pending": frozenset({"PENDING", "VERIFICATION_REQUIRED"}),
    "decision_required": frozenset({"AWAITING_APPROVAL"}),
    "failed": frozenset({"FAILED", "WAITING_FOR_SETUP"}),
    "rejected": frozenset({"REJECTED"}),
    "cancelled": frozenset({"CANCELLED"}),
    "expired": frozenset({"EXPIRED"}),
}


def _key(portal_status: str) -> str:
    return "_".join((portal_status or "").strip().lower().replace("-", " ").split())


def reconcile_item(
    *, client_reference: str, portal_status: str, portal_identity_verified: bool | None,
    request: Any | None, identity_satisfied: bool | None,
) -> dict[str, Any]:
    """Compare one portal record with the agent's case. Reports; never changes either side."""
    if request is None:
        return {
            "client_reference": client_reference, "found": False, "agrees": None,
            "reason": "no agent case exists for this reference; the portal request may not have "
                      "been imported, or was imported under a different idempotency key",
        }
    agent = lifecycle.public_status(request.status, request.error_code)
    expected = PORTAL_EXPECTATIONS.get(_key(portal_status))
    problems: list[str] = []
    agrees: bool | None
    if expected is None:
        agrees = None
        problems.append(f"portal status {portal_status!r} has no mapping in the contract")
    elif agent in expected:
        agrees = True
    else:
        agrees = False
        problems.append(
            f"portal says {portal_status!r} but the agent case is {agent} "
            f"(expected one of {sorted(expected)})"
        )
    if portal_identity_verified is not None and identity_satisfied is not None \
            and portal_identity_verified != identity_satisfied:
        agrees = False
        problems.append(
            f"identity differs: portal says {'verified' if portal_identity_verified else 'not verified'}, "
            f"agent says {'verified' if identity_satisfied else 'not verified'}"
        )
    return {
        "client_reference": client_reference, "found": True,
        "request_id": str(request.id), "reference": request.reference,
        "agent_public_status": agent, "agent_status": request.status,
        "portal_status": portal_status, "agrees": agrees,
        "reason": "; ".join(problems) if problems else "consistent",
    }
