"""Retention signal, from evidence that already exists.

PHASE 1 SCOPE, STATED HONESTLY

This does not decide whether data may be kept. That requires jurisdiction-specific
retention periods and the organisation's own policy, neither of which this platform
holds, and the development plan is explicit that they must not be hardcoded before the
business and legal rules are defined.

What it does is flag the cases where the evidence alone is enough to say "a person
should look at this" -- principally a cookie whose lifetime is long relative to the
purpose it serves. An analytics cookie lasting two years is not automatically a
violation; it is a reasonable thing to ask a question about.

Every threshold below is a REVIEW trigger, never a verdict.
"""

from __future__ import annotations

from app.agents.purpose.services.observed_service import ObservedPurpose

NOT_EVALUATED = "not_evaluated"
WITHIN_EXPECTATION = "within_expectation"
REVIEW_REQUIRED = "review_required"
UNKNOWN = "unknown"

# Lifetimes beyond which a purpose is worth a second look, in days.
#
# These are conventions, not law. 13 months is the commonly cited ceiling for analytics
# identifiers in EU guidance and is used here as a discussion threshold; marketing is
# held to the same bar. `functional` has no entry at all -- a session or security cookie
# has no purpose-derived expectation this module could assert.
_REVIEW_BEYOND_DAYS = {
    "analytics": 395,   # ~13 months
    "marketing": 395,
}


def evaluate(observed: ObservedPurpose) -> tuple[str, str | None]:
    """Return `(retention_status, note)` for one observed item.

    Returns `not_evaluated` rather than a guess whenever the evidence does not support
    a statement -- which is most of the time in Phase 1, and is the correct output.
    """
    # Only cookies carry a lifetime. A tracker is a network request; it has no expiry,
    # and inventing one from its cookies would attribute a property it does not have.
    if observed.subject_type != "cookie":
        return NOT_EVALUATED, None

    if observed.expiry_days is None:
        return UNKNOWN, (
            "No usable expiry was recorded for this cookie, so its retention could not "
            "be assessed. This is a gap in the evidence, not a finding about the cookie."
        )

    # A session cookie expires with the browser session; there is nothing to review.
    if observed.expiry_days <= 0:
        return WITHIN_EXPECTATION, "Session cookie -- expires when the browser session ends."

    threshold = _REVIEW_BEYOND_DAYS.get(observed.purpose or "")
    if threshold is None:
        return NOT_EVALUATED, (
            f"No retention expectation is defined for the purpose "
            f"{observed.purpose!r}, so no assessment was made."
        )

    if observed.expiry_days > threshold:
        years = round(observed.expiry_days / 365, 1)
        return REVIEW_REQUIRED, (
            f"This {observed.purpose} cookie persists for about {years} year(s) "
            f"({observed.expiry_days} days), beyond the {threshold}-day point at which a "
            f"{observed.purpose} identifier is normally reconsidered. This is a prompt to "
            f"review the retention period against your own policy, not a finding that it "
            f"is excessive."
        )

    return WITHIN_EXPECTATION, (
        f"Lifetime of {observed.expiry_days} days is within the {threshold}-day "
        f"reconsideration point for a {observed.purpose} identifier."
    )
