"""Mapping between the two purpose vocabularies this platform already speaks.

WHY THIS IS THE FIRST THING THE AGENT NEEDS

Consiva classifies purpose in two places, at two different granularities:

  * Agent 1 (Consent) labels a tracker or cookie with a `category` -- one of the four
    codes in `purpose_taxonomy`: analytics, marketing, functional, other. This is an
    OBSERVED purpose: it describes what a thing was seen doing in a browser.

  * A record of processing describes a BUSINESS purpose in prose -- "Customer Account
    Management", "Payroll / Compensation". This is a DECLARED purpose: what the
    organisation says the data is for.

These are not the same kind of statement, and comparing them naively produces nonsense:
"Customer Account Management" is not equal to "functional", but it is not a mismatch
either. So every comparison in this agent goes through here first, and this module is
allowed to answer "I cannot compare these" -- which is a real answer, not a failure.

THE RULE THIS MODULE EXISTS TO ENFORCE

A mismatch may only be reported when both sides map into the SAME vocabulary with
confidence. Anything else is `undetermined`. Reporting a mismatch that is really a
vocabulary artefact would be exactly the failure this codebase has been bitten by
before -- a confident, specific finding about something that was never true.
"""

from __future__ import annotations

from dataclasses import dataclass

# The four codes in `purpose_taxonomy`. Read as data at runtime where possible; listed
# here as the compile-time contract this module is written against.
TAXONOMY_CODES = ("analytics", "marketing", "functional", "other")

UNDETERMINED = "undetermined"

# Business purposes that map cleanly onto a taxonomy code.
#
# Deliberately incomplete. A business purpose absent from this table is NOT assumed to
# be "other" -- "other" is a real classification meaning "examined and does not fit",
# and using it as a dumping ground for anything unmapped would turn every unrecognised
# purpose into a false alignment with every genuinely-other tracker.
_BUSINESS_TO_TAXONOMY: dict[str, str] = {
    "marketing communications": "marketing",
    "advertising": "marketing",
    "retargeting": "marketing",
    "campaign management": "marketing",
    "analytics": "analytics",
    "usage measurement": "analytics",
    "product analytics": "analytics",
    "customer account management": "functional",
    "user account management": "functional",
    "authentication": "functional",
    "session management": "functional",
    "security": "functional",
    "fraud prevention": "functional",
    "payment processing": "functional",
    "support / ticketing": "functional",
    "support": "functional",
}

# Phrases that indicate a marketing purpose when they appear in free-text prose. Used
# only to enrich an already-mapped declaration, never to create one from nothing.
_MARKETING_HINTS = ("market", "advertis", "campaign", "promotion", "retarget")
_ANALYTICS_HINTS = ("analytic", "measurement", "telemetry", "statistic")


@dataclass(frozen=True)
class Mapping:
    """The outcome of trying to express one purpose in the taxonomy's terms."""

    code: str | None
    confidence: float
    basis: str

    @property
    def comparable(self) -> bool:
        """Whether this mapping is solid enough to base a mismatch claim on.

        0.7 is the floor because below it the mapping rests on a substring hint rather
        than a known phrase, and a substring is not enough to tell a customer their
        declared purpose and their actual behaviour disagree.
        """
        return self.code is not None and self.confidence >= 0.7


def to_taxonomy(purpose: str | None) -> Mapping:
    """Express any purpose string in the four-code taxonomy, or decline to.

    Returns a Mapping whose `code` is None when the purpose cannot be placed. That is
    the expected outcome for a large fraction of real business purposes -- "Recruitment"
    and "Vendor / Supplier Management" have no honest taxonomy equivalent, and inventing
    one would make every subsequent comparison meaningless.
    """
    if not purpose or not purpose.strip():
        return Mapping(None, 0.0, "no purpose supplied")

    text = purpose.strip().lower()

    # Already a taxonomy code.
    if text in TAXONOMY_CODES:
        return Mapping(text, 1.0, "already a taxonomy code")

    # A known business purpose.
    if text in _BUSINESS_TO_TAXONOMY:
        return Mapping(_BUSINESS_TO_TAXONOMY[text], 0.9, "known business purpose")

    # Prose containing an unambiguous signal. Lower confidence on purpose: this is a
    # substring match, and substring matching against free text is precisely how this
    # codebase previously produced a confident finding about a consent banner that did
    # not exist.
    if any(hint in text for hint in _MARKETING_HINTS):
        return Mapping("marketing", 0.6, "marketing wording in free text")
    if any(hint in text for hint in _ANALYTICS_HINTS):
        return Mapping("analytics", 0.6, "analytics wording in free text")

    return Mapping(None, 0.0, "no mapping to the taxonomy")


def compare(declared: str | None, observed: str | None) -> tuple[str, float, str]:
    """Compare a declared purpose with an observed one.

    Returns `(alignment, confidence, reason)` where alignment is one of
    `aligned` / `mismatch` / `undetermined`.

    `undetermined` is returned whenever either side cannot be mapped with confidence.
    It is not a degraded answer -- it is the correct answer, and the alternative
    (guessing) would put a compliance claim in front of a customer on the strength of a
    vocabulary coincidence.
    """
    # Identical purposes agree, whatever vocabulary they are written in.
    #
    # The taxonomy exists to compare DIFFERENT vocabularies. Routing an exact match
    # through it produced a real wrong answer: a table declared "Event Attendee
    # Management" and observed as "Event Attendee Management" came back `undetermined`,
    # because that business purpose has no taxonomy equivalent. Two identical
    # statements do not need a translator to be found equal.
    if declared and observed and declared.strip().lower() == observed.strip().lower():
        return "aligned", 1.0, "declared and observed purposes are identical"

    left = to_taxonomy(declared)
    right = to_taxonomy(observed)

    if not left.comparable and not right.comparable:
        return UNDETERMINED, 0.0, "neither purpose could be mapped to the taxonomy"
    if not left.comparable:
        return UNDETERMINED, 0.0, f"declared purpose could not be mapped ({left.basis})"
    if not right.comparable:
        return UNDETERMINED, 0.0, f"observed purpose could not be mapped ({right.basis})"

    # Both sides mapped. Confidence in the comparison is bounded by the weaker of the
    # two mappings -- a comparison is only as sound as its shakier half.
    confidence = round(min(left.confidence, right.confidence), 2)

    if left.code == right.code:
        return "aligned", confidence, f"both map to {left.code!r}"
    return (
        "mismatch",
        confidence,
        f"declared maps to {left.code!r} but observed maps to {right.code!r}",
    )
