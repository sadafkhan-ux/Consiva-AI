"""The constraint engine: may this action be performed at all? (prompt §20)

SYSTEM FACT vs POLICY INTERPRETATION
------------------------------------
These are kept rigorously apart, because conflating them is how a compliance tool
starts inventing law. A constraint produced here is one of two kinds:

  * `system` -- something this codebase can verify: the column is not in the
    erasable allowlist, the source does not permit execution, the record is
    referenced by another table. These are facts about the system as configured.

  * `policy` -- something an administrator CONFIGURED as a rule for their
    organization ("invoices are retained 7 years"). Consiva stores and applies it;
    it did not decide it.

There is deliberately no third kind that says "the law requires X". This module
never asserts a legal requirement. Where legal interpretation is genuinely needed,
that is a RAG-assisted note attached for a human to read (§20), never a machine
decision that blocks or permits an action on its own authority.

A constraint either BLOCKS an action or REQUIRES REVIEW of it. Nothing here silently
drops an action -- a blocked action stays in the plan carrying its reason, which is
what makes a partial fulfilment explainable to the requester (§47).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents.dsr.schemas import case

KIND_SYSTEM = "system"
KIND_POLICY = "policy"

EFFECT_BLOCK = "block"        # the action must not be executed
EFFECT_REVIEW = "review"      # the action may proceed only after a human says so
EFFECT_ALLOW = "allow"        # explicitly permitted, recorded for the audit trail


@dataclass(frozen=True)
class Constraint:
    """One evaluated rule against one action."""

    code: str
    kind: str          # KIND_SYSTEM | KIND_POLICY
    effect: str        # EFFECT_BLOCK | EFFECT_REVIEW | EFFECT_ALLOW
    reason: str
    source: str        # where the rule came from: 'allowlist', 'retention_policy', ...
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "kind": self.kind, "effect": self.effect,
            "reason": self.reason, "source": self.source, "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class RetentionRule:
    """An administrator-configured retention rule.

    `minimum_retention` is how long records in this table must be kept. Consiva does
    not decide this value -- it is configuration, and the `authority` field records
    who said so, so a reviewer reading a blocked deletion can see whose rule it was.
    """

    table_name: str
    minimum_retention: timedelta
    date_column: str
    authority: str
    applies_to_operations: frozenset[str] = frozenset({case.OP_DELETE_RECORD})


def evaluate(
    *,
    operation: str,
    table_name: str,
    grant,
    payload: dict[str, Any] | None = None,
    record_snapshot: dict[str, Any] | None = None,
    retention_rules: tuple[RetentionRule, ...] = (),
    now: datetime | None = None,
) -> tuple[Constraint, ...]:
    """Evaluate every constraint against one proposed action.

    Returns everything that applied, including EFFECT_ALLOW entries -- a plan that
    records only its blockers cannot show a reviewer what was checked and passed.
    """
    moment = now or datetime.now(UTC)
    found: list[Constraint] = []

    if operation not in case.MUTATING_OPERATIONS:
        found.append(Constraint(
            code="NON_MUTATING", kind=KIND_SYSTEM, effect=EFFECT_ALLOW,
            reason=f"{operation} does not modify the source",
            source="operation_type",
        ))
        return tuple(found)

    # ── System facts ─────────────────────────────────────────────────────────────
    if not grant.allow_execution:
        found.append(Constraint(
            code=case.ERR_SOURCE_NOT_AUTHORIZED, kind=KIND_SYSTEM, effect=EFFECT_BLOCK,
            reason=(
                f"source {grant.source_name!r} is authorized for DSR search but not for "
                "execution; an administrator must enable execution and configure a write "
                "credential before this action can run"
            ),
            source="source_authorization",
        ))
    elif not grant.write_credential_ref:
        found.append(Constraint(
            code=case.ERR_SOURCE_NOT_AUTHORIZED, kind=KIND_SYSTEM, effect=EFFECT_BLOCK,
            reason=(
                f"source {grant.source_name!r} permits execution but names no write "
                "credential; refusing to write using the read credential"
            ),
            source="source_authorization",
        ))
    else:
        found.append(Constraint(
            code="EXECUTION_AUTHORIZED", kind=KIND_SYSTEM, effect=EFFECT_ALLOW,
            reason=f"source {grant.source_name!r} is authorized for DSR execution",
            source="source_authorization",
        ))

    if operation in (case.OP_UPDATE_FIELD, case.OP_ANONYMIZE_FIELD):
        unauthorized = sorted(
            column for column in (payload or {})
            if column not in grant.erasable_columns.get(table_name, ())
        )
        if unauthorized:
            found.append(Constraint(
                code=case.ERR_ACTION_BLOCKED, kind=KIND_SYSTEM, effect=EFFECT_BLOCK,
                reason=(
                    f"column(s) {unauthorized} on {table_name} are not in the writable "
                    "allowlist for this source; the action cannot be executed as planned"
                ),
                source="column_allowlist",
                evidence=tuple(f"{table_name}.{c}" for c in unauthorized),
            ))
        if not payload:
            found.append(Constraint(
                code=case.ERR_ACTION_BLOCKED, kind=KIND_SYSTEM, effect=EFFECT_BLOCK,
                reason=f"a {operation} on {table_name} named no columns to change",
                source="column_allowlist",
            ))

    if operation == case.OP_DELETE_RECORD:
        # Deleting a whole record is the highest-impact thing this system does, and
        # it is never automatic regardless of what else passes.
        found.append(Constraint(
            code="HIGH_IMPACT", kind=KIND_SYSTEM, effect=EFFECT_REVIEW,
            reason="record deletion is irreversible and always requires human approval",
            source="operation_type",
        ))

    # ── Configured organizational policy ─────────────────────────────────────────
    for rule in retention_rules:
        if rule.table_name != table_name or operation not in rule.applies_to_operations:
            continue
        verdict = _retention_verdict(rule, record_snapshot, moment)
        found.append(verdict)

    return tuple(found)


def _retention_verdict(
    rule: RetentionRule, snapshot: dict[str, Any] | None, now: datetime
) -> Constraint:
    """Apply one configured retention rule.

    When the record's date cannot be read, this REVIEWS rather than allowing or
    blocking. Guessing either way would be wrong: allowing risks deleting something
    the organization said to keep, blocking risks refusing a lawful erasure on a
    technicality. A human decides.
    """
    raw = (snapshot or {}).get(rule.date_column)
    if raw is None:
        return Constraint(
            code=case.ERR_POLICY_REVIEW_REQUIRED, kind=KIND_POLICY, effect=EFFECT_REVIEW,
            reason=(
                f"retention rule on {rule.table_name} could not be evaluated: column "
                f"{rule.date_column!r} was not available on the record. A reviewer must "
                f"confirm whether the {rule.authority} retention requirement applies."
            ),
            source="retention_policy",
        )

    created = _parse_datetime(raw)
    if created is None:
        return Constraint(
            code=case.ERR_POLICY_REVIEW_REQUIRED, kind=KIND_POLICY, effect=EFFECT_REVIEW,
            reason=(
                f"retention rule on {rule.table_name} could not be evaluated: "
                f"{rule.date_column}={raw!r} is not a readable date"
            ),
            source="retention_policy",
        )

    retain_until = created + rule.minimum_retention
    if now < retain_until:
        return Constraint(
            code=case.ERR_ACTION_BLOCKED, kind=KIND_POLICY, effect=EFFECT_BLOCK,
            reason=(
                f"{rule.authority} requires records in {rule.table_name} to be retained "
                f"until {retain_until.date().isoformat()}; this record cannot be deleted yet"
            ),
            source="retention_policy",
            evidence=(f"{rule.date_column}={created.date().isoformat()}",),
        )
    return Constraint(
        code="RETENTION_SATISFIED", kind=KIND_POLICY, effect=EFFECT_ALLOW,
        reason=(
            f"{rule.authority} retention period for {rule.table_name} elapsed on "
            f"{retain_until.date().isoformat()}"
        ),
        source="retention_policy",
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        # A naive datetime from a `timestamp without time zone` column is read as UTC.
        # Guessing the server's local zone would silently shift a retention boundary.
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def verdict(constraints: tuple[Constraint, ...]) -> str:
    """Collapse a set of constraints to one effect, worst-case first.

    A single block beats any number of allows. This is deliberately not a score: a
    retention requirement is not outweighed by three things that happened to pass.
    """
    effects = {c.effect for c in constraints}
    if EFFECT_BLOCK in effects:
        return EFFECT_BLOCK
    if EFFECT_REVIEW in effects:
        return EFFECT_REVIEW
    return EFFECT_ALLOW


def blocking_reason(constraints: tuple[Constraint, ...]) -> str | None:
    """The reason to show a reviewer for a blocked action. Joins every blocker
    rather than reporting only the first -- an action blocked for two reasons that
    reports one looks fixable when it is not."""
    reasons = [c.reason for c in constraints if c.effect == EFFECT_BLOCK]
    return "; ".join(reasons) if reasons else None
