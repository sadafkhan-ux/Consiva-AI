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


# What a data subject is told when an action could not be completed for a reason that
# is about OUR configuration rather than about their data. It says what happened to
# their record and what happens next, and nothing about which credential is missing --
# that is our internal state, it tells them nothing they can act on, and it reads as
# an excuse rather than an answer.
REFERRED_TO_TEAM = (
    "We were not able to complete this automatically. It has been referred to our "
    "team, who will action it manually and confirm the outcome to you."
)


@dataclass(frozen=True)
class Constraint:
    """One evaluated rule against one action.

    TWO AUDIENCES, TWO TEXTS
    ------------------------
    `reason` is written for the reviewer who has to decide what to do about this. It
    names tables, columns, credentials and configuration, because that is what makes
    it actionable.

    `requester_explanation` is written for the data subject, and the split is not
    cosmetic. Where the blocker is a POLICY -- a retention requirement -- the
    substance is disclosed in full, because why an erasure was refused on policy
    grounds is precisely what a data principal is entitled to know. Where the blocker
    is our own configuration, they are told their record was not changed and that a
    human is handling it, and nothing about our internals.
    """

    code: str
    kind: str          # KIND_SYSTEM | KIND_POLICY
    effect: str        # EFFECT_BLOCK | EFFECT_REVIEW | EFFECT_ALLOW
    reason: str
    source: str        # where the rule came from: 'allowlist', 'retention_policy', ...
    evidence: tuple[str, ...] = ()
    # None for anything that is not a blocker -- there is nothing to explain.
    requester_explanation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "kind": self.kind, "effect": self.effect,
            "reason": self.reason, "source": self.source, "evidence": list(self.evidence),
            "requester_explanation": self.requester_explanation,
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
            requester_explanation=REFERRED_TO_TEAM,
        ))
    elif not grant.write_credential_ref:
        found.append(Constraint(
            code=case.ERR_SOURCE_NOT_AUTHORIZED, kind=KIND_SYSTEM, effect=EFFECT_BLOCK,
            reason=(
                f"source {grant.source_name!r} permits execution but names no write "
                "credential; refusing to write using the read credential"
            ),
            source="source_authorization",
            requester_explanation=REFERRED_TO_TEAM,
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
                requester_explanation=REFERRED_TO_TEAM,
            ))
        if not payload:
            found.append(Constraint(
                code=case.ERR_ACTION_BLOCKED, kind=KIND_SYSTEM, effect=EFFECT_BLOCK,
                reason=f"a {operation} on {table_name} named no columns to change",
                source="column_allowlist",
                requester_explanation=REFERRED_TO_TEAM,
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
            requester_explanation=(
                f"We are checking whether a retention requirement ({rule.authority}) "
                "applies to this record before we action it, and will confirm shortly."
            ),
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
            requester_explanation=(
                f"We are checking whether a retention requirement ({rule.authority}) "
                "applies to this record before we action it, and will confirm shortly."
            ),
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
            requester_explanation=(
                f"This record is kept under a retention requirement ({rule.authority}) "
                f"and cannot be deleted until {retain_until.date().isoformat()}. "
                "We will delete it once that period ends."
            ),
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


def requester_explanation(constraints: tuple[Constraint, ...]) -> str:
    """What the data subject is told about a blocked action.

    Policy explanations come first and are disclosed in full; a configuration blocker
    contributes only the referral sentence, and only when nothing more substantive
    applies. De-duplicated, because the same referral sentence repeated three times
    reads as noise rather than as an answer.
    """
    policy = [
        c.requester_explanation for c in constraints
        if c.effect == EFFECT_BLOCK and c.kind == KIND_POLICY and c.requester_explanation
    ]
    if policy:
        return " ".join(dict.fromkeys(policy))
    system = [
        c.requester_explanation for c in constraints
        if c.effect == EFFECT_BLOCK and c.requester_explanation
    ]
    return " ".join(dict.fromkeys(system)) if system else REFERRED_TO_TEAM


def blocking_reason(constraints: tuple[Constraint, ...]) -> str | None:
    """The reason to show a reviewer for a blocked action. Joins every blocker
    rather than reporting only the first -- an action blocked for two reasons that
    reports one looks fixable when it is not."""
    reasons = [c.reason for c in constraints if c.effect == EFFECT_BLOCK]
    return "; ".join(reasons) if reasons else None
