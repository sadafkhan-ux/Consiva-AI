"""Is this regulatory change relevant to THIS organisation? (spec §4 step 5, §15)

Deterministic, and deliberately reluctant. Three outcomes, not two:

    relevant      -- a rule positively established a connection to this organisation
    not_relevant  -- a rule positively established there is none
    undetermined  -- nothing established either way

`undetermined` is the DEFAULT and the most common answer, and that is the point. A
two-valued filter has to call everything it does not recognise irrelevant, which is how
a regulatory change affecting an organisation gets quietly dropped. The spec is
explicit that relevance conclusions must show evidence and confidence; an answer with
no evidence behind it is `undetermined` with `unknown` confidence, and it goes to a
person.

WHAT THIS DOES NOT DO
---------------------
It does not decide whether an obligation APPLIES. That is a legal determination, it
needs the approved corpus and a human, and nothing in this module reaches it. This
only answers the narrower operational question: is there anything in this
organisation that this change could plausibly touch?
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.regwatch.schemas import watch

# Jurisdiction strings are written by hand on both sides -- on the source, and on the
# organisation's profile -- so they are compared loosely. "IN", "India" and "in-IN"
# are the same place, and a mismatch on spelling must not read as a positive finding
# of irrelevance.
_JURISDICTION_ALIASES: dict[str, frozenset[str]] = {
    "india": frozenset({"in", "ind", "india", "in-in", "bharat"}),
    "eu": frozenset({"eu", "eea", "europe", "european union"}),
    "uk": frozenset({"uk", "gb", "united kingdom", "great britain"}),
    "us": frozenset({"us", "usa", "united states", "u.s."}),
    "global": frozenset({"global", "worldwide", "international", "any"}),
}

# Topics on a source, mapped to the agents that would care. Used to explain WHY
# something is relevant, not to decide that it is.
_TOPIC_SIGNALS: dict[str, tuple[str, ...]] = {
    "consent": ("consent", "cookie", "tracking", "opt-in", "opt in"),
    "breach": ("breach", "incident", "notification", "security incident"),
    "rights": ("access request", "erasure", "rectification", "data principal", "dsr",
               "subject request", "portability"),
    "retention": ("retention", "storage limitation", "deletion schedule"),
    "transfer": ("cross-border", "transfer", "localisation", "localization"),
    "children": ("child", "children", "minor", "age verification"),
    "security": ("safeguard", "encryption", "security measure", "reasonable security"),
}

_WORD = re.compile(r"[a-z0-9][a-z0-9\-]*")


def normalise_jurisdiction(value: str | None) -> str:
    """Collapse a hand-written jurisdiction to a canonical key, or return it lowered."""
    lowered = (value or "").strip().lower()
    for canonical, aliases in _JURISDICTION_ALIASES.items():
        if lowered in aliases:
            return canonical
    return lowered


@dataclass(frozen=True)
class RelevanceResult:
    relevance: str
    confidence: str
    reason: str
    # Which topics the change text appears to touch. Descriptive, and used downstream
    # to aim impact mapping at the right agent.
    topics: tuple[str, ...] = field(default_factory=tuple)
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_a_person(self) -> bool:
        """Anything not positively established goes to review. So does anything the
        rules called relevant -- being told a change matters is the beginning of a
        decision, not the end of one."""
        return self.relevance != watch.NOT_RELEVANT or self.confidence == watch.UNKNOWN


def assess(
    *,
    source_jurisdiction: str | None,
    org_jurisdictions: tuple[str, ...],
    change_text: str | None,
    change_kind: str,
    source_topic: str | None = None,
) -> RelevanceResult:
    """Decide whether a detected change plausibly touches this organisation.

    `change_kind` is checked first: a source that could not be collected has no text
    to assess, and guessing at relevance from an absent document would be inventing
    the very thing this module refuses to invent.
    """
    if change_kind == watch.CHANGE_UNREACHABLE:
        return RelevanceResult(
            relevance=watch.UNDETERMINED,
            confidence=watch.UNKNOWN,
            reason=(
                "The source could not be collected, so there is no content to assess. "
                "Relevance is unknown -- this is not a finding that the change is "
                "irrelevant."
            ),
            evidence=("collection failed; no document was retrieved",),
        )

    source_key = normalise_jurisdiction(source_jurisdiction)
    org_keys = {normalise_jurisdiction(j) for j in org_jurisdictions if j}

    text = (change_text or "").lower()
    topics = tuple(
        topic for topic, markers in _TOPIC_SIGNALS.items()
        if any(marker in text for marker in markers)
    )

    # A jurisdiction the organisation does not operate in is the one case where a
    # rule can positively establish irrelevance -- and even then only when we actually
    # know where the organisation operates.
    if org_keys and source_key and source_key not in org_keys and source_key != "global":
        if "global" in org_keys:
            pass  # an org that says "global" operates everywhere; fall through
        else:
            return RelevanceResult(
                relevance=watch.NOT_RELEVANT,
                confidence=watch.PROBABLE,
                reason=(
                    f"The source covers {source_jurisdiction!r}, and this organisation "
                    f"is recorded as operating in {sorted(org_keys)}. Probable, not "
                    "confirmed: an organisation can acquire an obligation in a "
                    "jurisdiction before its profile is updated."
                ),
                topics=topics,
                evidence=(f"source jurisdiction {source_key!r} not in {sorted(org_keys)}",),
            )

    if not org_keys:
        return RelevanceResult(
            relevance=watch.UNDETERMINED,
            confidence=watch.UNKNOWN,
            reason=(
                "This organisation has no recorded jurisdictions, so nothing can be "
                "ruled in or out. Record them to make this assessment meaningful."
            ),
            topics=topics,
            evidence=("no org jurisdictions configured",),
        )

    jurisdiction_matches = source_key in org_keys or source_key == "global"

    if jurisdiction_matches and topics:
        return RelevanceResult(
            relevance=watch.RELEVANT,
            # PROBABLE, never CONFIRMED: the jurisdiction lines up and the text touches
            # a topic this platform handles, which is a good reason to look -- not a
            # finding that an obligation applies.
            confidence=watch.PROBABLE,
            reason=(
                f"The source covers {source_jurisdiction!r}, which this organisation "
                f"operates in, and the change text touches {', '.join(topics)}."
            ),
            topics=topics,
            evidence=(
                f"jurisdiction {source_key!r} matches",
                f"topics matched: {', '.join(topics)}",
            ),
        )

    if jurisdiction_matches:
        return RelevanceResult(
            relevance=watch.UNDETERMINED,
            confidence=watch.POSSIBLE,
            reason=(
                f"The source covers {source_jurisdiction!r}, which this organisation "
                "operates in, but the change text did not match any topic this "
                "platform tracks. It may still matter; a person should read it."
            ),
            topics=topics,
            evidence=(f"jurisdiction {source_key!r} matches", "no topic matched"),
        )

    return RelevanceResult(
        relevance=watch.UNDETERMINED,
        confidence=watch.UNKNOWN,
        reason=(
            "Neither jurisdiction nor topic could be established from the change. "
            "Undetermined rather than irrelevant -- nothing was ruled out."
        ),
        topics=topics,
        evidence=("no rule matched",),
    )


def priority_for(result: RelevanceResult, *, change_is_minor: bool) -> tuple[str, str]:
    """A starting priority and the confidence it is held with.

    Deliberately coarse. Priority is a scheduling hint for a human queue, not a risk
    score, and a precise-looking number here would invite exactly the false confidence
    the rest of the platform refuses.
    """
    if result.relevance == watch.NOT_RELEVANT:
        return watch.PRIORITY_LOW, watch.PROBABLE
    if result.relevance == watch.RELEVANT:
        # A relevant change to a topic the platform handles is worth reading soon,
        # regardless of how few lines moved -- a one-line amendment can be the change.
        return watch.PRIORITY_HIGH if not change_is_minor else watch.PRIORITY_MEDIUM, watch.POSSIBLE
    return watch.PRIORITY_MEDIUM, watch.UNKNOWN
