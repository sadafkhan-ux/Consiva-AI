"""Deterministic classification of a DSR request into a controlled type (prompt §14).

The rule the prompt sets is "prefer deterministic logic where possible; use the LLM
only where ambiguity requires reasoning". This module is the deterministic half. It
returns a verdict for the clear cases and explicitly declines the rest, so the
caller knows when a model is actually needed rather than calling one every time.

Two things it deliberately does NOT do:

  * It never returns a type outside CLASSIFIABLE_TYPES. The vocabulary is closed, so
    neither a rule nor a model can invent "partial_deletion" as a request type.

  * It never resolves a genuine conflict by picking the higher score. "Send me my
    data and then delete it" is two requests, and quietly honouring the deletion half
    while dropping the access half is exactly the silent loss §47 forbids. Conflicts
    return `ambiguous` so a human (or the model, then a human) decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.dsr.schemas import case

# Confidence bands. A phrase match is strong evidence of intent; a bare keyword is
# weaker because the same word appears in unrelated sentences ("I deleted my
# account myself, but what do you still hold?" is an ACCESS request).
CONF_PHRASE = 0.95
CONF_KEYWORD = 0.75
CONF_AMBIGUOUS = 0.0

METHOD_DETERMINISTIC = "deterministic"
METHOD_LLM = "llm"
METHOD_MANUAL = "manual"


@dataclass(frozen=True)
class ClassificationResult:
    """A verdict, or an explicit refusal to guess.

    `request_type` is None only when `ambiguous` is True -- the caller must then
    either ask the model or route to human review. It is never None-because-nothing-
    matched: an unmatched request classifies as OTHER, which is a real, reviewable
    outcome rather than a gap.
    """

    request_type: str | None
    confidence: float
    method: str
    evidence: tuple[str, ...]
    ambiguous: bool = False
    candidates: tuple[str, ...] = ()

    @property
    def needs_reasoning(self) -> bool:
        """True when a model or a human should look at this before it proceeds."""
        return self.ambiguous or self.request_type == case.OTHER


# Phrases that state an intent outright. Matched on a word-boundary regex over the
# normalized text, so "erase my data" hits but "eraser" does not.
_PHRASES: tuple[tuple[str, str], ...] = (
    # Deletion / erasure.
    (r"delete (my|all my|any) (personal )?(data|information|details|account|records)", case.DELETION),
    (r"erase (my|all my|any) (personal )?(data|information|details|account|records)", case.DELETION),
    (r"remove (my|all my) (personal )?(data|information|details|account|records)", case.DELETION),
    (r"right to (be forgotten|erasure)", case.DELETION),
    (r"(close|deactivate) my account and delete", case.DELETION),
    (r"stop (processing|storing) my (personal )?(data|information)", case.DELETION),
    # Correction / rectification.
    (r"(correct|update|change|fix|amend|rectify) my", case.CORRECTION),
    (r"(is|are) (wrong|incorrect|outdated|out of date)", case.CORRECTION),
    (r"right to (rectification|correction)", case.CORRECTION),
    (r"should be (updated|corrected|changed)", case.CORRECTION),
    # Access.
    (r"(show|tell|send|give) me (what|all|a copy|the) ", case.ACCESS),
    (r"what (personal )?(data|information) do you (have|hold|store|keep)", case.ACCESS),
    (r"(copy|copies) of my (personal )?(data|information|records)", case.ACCESS),
    (r"right (of|to) access", case.ACCESS),
    (r"access (my|to my) (personal )?(data|information|records)", case.ACCESS),
    # Export / portability. Checked before ACCESS keywords because an export request
    # is a narrower, more specific form of access and its own controlled type.
    (r"(export|download|portab)", case.EXPORT),
    (r"machine[- ]readable", case.EXPORT),
    (r"transfer my data to", case.EXPORT),
    # Information about processing, as distinct from the data itself.
    (r"(why|how) (do|are) you (process|use|collect|share)", case.INFORMATION),
    (r"who (do you share|have you shared|else has)", case.INFORMATION),
    (r"(what is|explain) your (retention|privacy) (policy|period)", case.INFORMATION),
    (r"purpose(s)? (of|for) (processing|collecting)", case.INFORMATION),
    (r"how long do you (keep|store|retain)", case.INFORMATION),
)

# Single words that suggest a type without stating it. Weaker, and only consulted
# when no phrase matched.
_KEYWORDS: dict[str, str] = {
    "delete": case.DELETION,
    "deletion": case.DELETION,
    "erase": case.DELETION,
    "erasure": case.DELETION,
    "forgotten": case.DELETION,
    "correct": case.CORRECTION,
    "correction": case.CORRECTION,
    "rectify": case.CORRECTION,
    "rectification": case.CORRECTION,
    "update": case.CORRECTION,
    "wrong": case.CORRECTION,
    "incorrect": case.CORRECTION,
    "access": case.ACCESS,
    "copy": case.ACCESS,
    "disclose": case.ACCESS,
    "export": case.EXPORT,
    "download": case.EXPORT,
    "portability": case.EXPORT,
    "retention": case.INFORMATION,
    "purpose": case.INFORMATION,
}

_COMPILED_PHRASES = tuple((re.compile(pattern), rtype) for pattern, rtype in _PHRASES)
_WORD = re.compile(r"[a-z]+")

# Pairs that routinely appear together without being a real conflict, with the type
# that wins. These are cases where one intent is phrased THROUGH the other: "download
# a copy of my data" reads as both EXPORT and ACCESS, but it is one request, and the
# narrower type is the right answer.
_SUBSUMES: dict[frozenset[str], str] = {
    frozenset({case.DELETION, case.ACCESS}): case.DELETION,
    frozenset({case.CORRECTION, case.ACCESS}): case.CORRECTION,
    frozenset({case.EXPORT, case.ACCESS}): case.EXPORT,
    frozenset({case.INFORMATION, case.ACCESS}): case.INFORMATION,
}

# Subsumption above is only safe for ONE clause. "Download a copy of my data" is one
# request wearing two labels; "send me my data and then delete it" is two requests,
# and collapsing it to DELETION silently drops the access half (§47). A coordinating
# conjunction joining two intents is the deterministic signal that tells them apart,
# and it overrides subsumption rather than being weighed against it.
_MULTI_INTENT = re.compile(
    r"\band then\b|\band also\b|\balso,? please\b|\bas well as\b|\band i (also )?want\b"
    r"|\bafter that\b|\bsecondly\b|;"
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def classify(raw_request: str) -> ClassificationResult:
    """Classify a request from its text alone.

    Never raises and never returns an unknown type. The worst case is
    `ambiguous=True`, which routes to the model or a human.
    """
    normalized = _normalize(raw_request or "")
    if not normalized:
        return ClassificationResult(
            request_type=case.OTHER, confidence=CONF_AMBIGUOUS, method=METHOD_DETERMINISTIC,
            evidence=("empty request text",),
        )

    phrase_hits: list[tuple[str, str]] = []
    for pattern, request_type in _COMPILED_PHRASES:
        match = pattern.search(normalized)
        if match:
            phrase_hits.append((request_type, match.group(0)))

    joined = bool(_MULTI_INTENT.search(normalized))

    if phrase_hits:
        types = {t for t, _ in phrase_hits}
        resolved = _resolve(types, joined=joined)
        evidence = tuple(f"matched phrase {matched!r} -> {t}" for t, matched in phrase_hits[:5])
        if resolved is None:
            return ClassificationResult(
                request_type=None, confidence=CONF_AMBIGUOUS, method=METHOD_DETERMINISTIC,
                evidence=evidence + (f"conflicting intents: {sorted(types)}",),
                ambiguous=True, candidates=tuple(sorted(types)),
            )
        return ClassificationResult(
            request_type=resolved, confidence=CONF_PHRASE, method=METHOD_DETERMINISTIC,
            evidence=evidence,
        )

    words = set(_WORD.findall(normalized))
    keyword_hits = {(_KEYWORDS[w], w) for w in words & _KEYWORDS.keys()}
    if keyword_hits:
        types = {t for t, _ in keyword_hits}
        resolved = _resolve(types, joined=joined)
        evidence = tuple(f"keyword {w!r} -> {t}" for t, w in sorted(keyword_hits, key=lambda h: h[1])[:5])
        if resolved is None:
            return ClassificationResult(
                request_type=None, confidence=CONF_AMBIGUOUS, method=METHOD_DETERMINISTIC,
                evidence=evidence + (f"conflicting intents: {sorted(types)}",),
                ambiguous=True, candidates=tuple(sorted(types)),
            )
        return ClassificationResult(
            request_type=resolved, confidence=CONF_KEYWORD, method=METHOD_DETERMINISTIC,
            evidence=evidence,
        )

    # Nothing matched. OTHER is a real outcome that routes to review -- not a gap,
    # and not a guess.
    return ClassificationResult(
        request_type=case.OTHER, confidence=CONF_AMBIGUOUS, method=METHOD_DETERMINISTIC,
        evidence=("no deterministic rule matched the request text",),
    )


def _resolve(types: set[str], *, joined: bool) -> str | None:
    """One type, or None when the request genuinely asks for two different things.

    `joined` says the text coordinates two clauses ("... and then ..."). When it
    does, two matched types are two requests and subsumption must not apply -- that
    is the difference between "download a copy of my data" (one request) and "send
    me my data and then delete it" (two).
    """
    if len(types) == 1:
        return next(iter(types))
    if joined:
        return None
    if len(types) == 2 and (winner := _SUBSUMES.get(frozenset(types))):
        return winner
    return None


def coerce_model_type(value: str | None) -> str | None:
    """Hold an LLM's answer to the same closed vocabulary the rules use.

    Returns None for anything outside it, so an invented type ("partial_deletion",
    "gdpr_request") is discarded rather than written to the case. The caller treats
    None as "the model did not classify this" and routes to human review.
    """
    if not value:
        return None
    candidate = value.strip().lower()
    return candidate if candidate in case.CLASSIFIABLE_TYPES else None
