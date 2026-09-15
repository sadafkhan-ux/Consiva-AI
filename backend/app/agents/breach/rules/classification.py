"""Deterministic classification of an incident into a controlled type (prompt §9).

Same rule as Agent 3's request classifier: deterministic logic first, a model only
where ambiguity genuinely needs reasoning. This module is the deterministic half and
declines explicitly on the rest, so the caller knows when a model is actually needed
rather than calling one every time.

WHY PHRASES BEFORE KEYWORDS
An incident report is written under pressure, often by someone who is not a security
specialist, and the same word means different things in different sentences. "The
laptop was stolen" is a lost-device incident; "credentials were stolen" is a
credential compromise; "data was stolen" is exfiltration. A bare keyword match on
"stolen" would classify all three identically, so phrases are matched first and a
keyword is only consulted when no phrase did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.breach.schemas import incident

CONF_PHRASE = 0.9
CONF_KEYWORD = 0.65
CONF_NONE = 0.0

METHOD_DETERMINISTIC = "deterministic"
METHOD_LLM = "llm"
METHOD_MANUAL = "manual"


@dataclass(frozen=True)
class ClassificationResult:
    """A verdict, or an explicit refusal to guess.

    `incident_type` is None only when `ambiguous` is True. It is never
    None-because-nothing-matched: an unrecognised report classifies as `other`, which
    is a real reviewable outcome rather than a gap.
    """

    incident_type: str | None
    confidence: float
    method: str
    evidence: tuple[str, ...]
    ambiguous: bool = False
    candidates: tuple[str, ...] = ()

    @property
    def needs_reasoning(self) -> bool:
        return self.ambiguous or self.incident_type == incident.TYPE_OTHER


# Phrases that state a kind of incident outright. Order matters: the more specific
# reading is listed first where two could both match.
_PHRASES: tuple[tuple[str, str], ...] = (
    # Credential compromise -- before "unauthorized access", since a compromised
    # credential is usually reported as somebody logging in as someone else.
    (r"(credential|password|api key|token|secret)s? (were |was |been )?(stolen|leaked|compromised|exposed|phish)", incident.TYPE_CREDENTIAL_COMPROMISE),
    (r"(phishing|phished)", incident.TYPE_CREDENTIAL_COMPROMISE),
    (r"account (was |been )?(taken over|compromised)", incident.TYPE_CREDENTIAL_COMPROMISE),
    (r"(mfa|2fa) bypass", incident.TYPE_CREDENTIAL_COMPROMISE),
    # Ransomware / malware.
    (r"(ransomware|ransom note|encrypted our|malware|trojan|virus|crypto.?locker)", incident.TYPE_MALWARE),
    # Lost or stolen device.
    (r"(laptop|phone|device|usb|drive|tablet)[^.]{0,30}(lost|stolen|missing|left)", incident.TYPE_LOST_DEVICE),
    (r"(lost|stolen|missing)[^.]{0,20}(laptop|phone|device|usb|drive|tablet)", incident.TYPE_LOST_DEVICE),
    # Misconfiguration -- before data exposure, because a public bucket is usually
    # reported as a configuration mistake and the remedy differs.
    (r"(publicly accessible|public(ly)? exposed|no authentication|misconfigur|open to the internet|world.?readable)", incident.TYPE_MISCONFIGURATION),
    (r"(bucket|storage|database) (was |is )?(public|open|unsecured)", incident.TYPE_MISCONFIGURATION),
    # Third-party.
    (r"(vendor|supplier|third.?party|processor|sub.?processor) (breach|incident|notified|reported)", incident.TYPE_THIRD_PARTY),
    # Insider.
    (r"(employee|staff|insider|contractor)[^.]{0,40}(downloaded|exfiltrat|took|copied|sold|leaked)", incident.TYPE_INSIDER),
    (r"(disgruntled|leaving|departing) (employee|staff)", incident.TYPE_INSIDER),
    # Accidental disclosure.
    (r"(sent|emailed|shared|mailed) (to the )?(wrong|incorrect) (recipient|person|address|customer)", incident.TYPE_ACCIDENTAL_DISCLOSURE),
    (r"(cc|bcc)[^.]{0,20}(mistake|error|instead)", incident.TYPE_ACCIDENTAL_DISCLOSURE),
    (r"accidental(ly)? (sent|disclosed|shared|exposed)", incident.TYPE_ACCIDENTAL_DISCLOSURE),
    # Exfiltration -- data actually leaving.
    (r"(data|records|database)[^.]{0,45}(exfiltrat|downloaded|copied out|dumped|extracted|posted online|posted publicly|for sale)", incident.TYPE_DATA_LEAKAGE),
    (r"(dark web|leak site|pastebin|posted publicly)", incident.TYPE_DATA_LEAKAGE),
    # Exposure -- data reachable, not necessarily taken.
    (r"(data|records|table|customer information)[^.]{0,45}(exposed|visible|accessible|disclosed)", incident.TYPE_DATA_EXPOSURE),
    # Unauthorized access -- the broadest, so it is matched last among phrases.
    (r"unauthori[sz]ed (access|login|entry|query|read)", incident.TYPE_UNAUTHORIZED_ACCESS),
    (r"(suspicious|anomalous|unexpected|unusual) (login|access|activity|query|connection)", incident.TYPE_UNAUTHORIZED_ACCESS),
    (r"(accessed|logged in|queried)[^.]{0,30}without (permission|authori)", incident.TYPE_UNAUTHORIZED_ACCESS),
    (r"brute.?force", incident.TYPE_UNAUTHORIZED_ACCESS),
)

# Single words that hint at a type without stating one. Weaker, consulted only when
# no phrase matched.
_KEYWORDS: dict[str, str] = {
    "ransomware": incident.TYPE_MALWARE,
    "malware": incident.TYPE_MALWARE,
    "phishing": incident.TYPE_CREDENTIAL_COMPROMISE,
    "credentials": incident.TYPE_CREDENTIAL_COMPROMISE,
    "password": incident.TYPE_CREDENTIAL_COMPROMISE,
    "misconfiguration": incident.TYPE_MISCONFIGURATION,
    "misconfigured": incident.TYPE_MISCONFIGURATION,
    "exfiltration": incident.TYPE_DATA_LEAKAGE,
    "leak": incident.TYPE_DATA_LEAKAGE,
    "leaked": incident.TYPE_DATA_LEAKAGE,
    "exposed": incident.TYPE_DATA_EXPOSURE,
    "exposure": incident.TYPE_DATA_EXPOSURE,
    "unauthorized": incident.TYPE_UNAUTHORIZED_ACCESS,
    "unauthorised": incident.TYPE_UNAUTHORIZED_ACCESS,
    "intrusion": incident.TYPE_UNAUTHORIZED_ACCESS,
    "vendor": incident.TYPE_THIRD_PARTY,
    "insider": incident.TYPE_INSIDER,
    "stolen": incident.TYPE_LOST_DEVICE,
}

_COMPILED = tuple((re.compile(p), t) for p, t in _PHRASES)
_WORD = re.compile(r"[a-z]+")

# Pairs where one reading contains the other and the narrower one is right. An
# incident is usually reported as a chain -- "a phished credential was used to access
# the database and download records" -- and the useful classification is the ROOT
# CAUSE, because that is what the containment action has to address.
_SUBSUMES: dict[frozenset[str], str] = {
    frozenset({incident.TYPE_CREDENTIAL_COMPROMISE, incident.TYPE_UNAUTHORIZED_ACCESS}):
        incident.TYPE_CREDENTIAL_COMPROMISE,
    frozenset({incident.TYPE_MISCONFIGURATION, incident.TYPE_DATA_EXPOSURE}):
        incident.TYPE_MISCONFIGURATION,
    frozenset({incident.TYPE_UNAUTHORIZED_ACCESS, incident.TYPE_DATA_EXPOSURE}):
        incident.TYPE_UNAUTHORIZED_ACCESS,
    frozenset({incident.TYPE_DATA_EXPOSURE, incident.TYPE_DATA_LEAKAGE}):
        incident.TYPE_DATA_LEAKAGE,
    frozenset({incident.TYPE_MALWARE, incident.TYPE_UNAUTHORIZED_ACCESS}):
        incident.TYPE_MALWARE,
    frozenset({incident.TYPE_INSIDER, incident.TYPE_DATA_LEAKAGE}):
        incident.TYPE_INSIDER,
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def classify(title: str, description: str = "") -> ClassificationResult:
    """Classify an incident from its title and description.

    Never raises, and never returns a type outside the closed vocabulary. The worst
    case is `ambiguous=True`, which routes to a model or a human.
    """
    text = _normalize(f"{title} {description}")
    if not text:
        return ClassificationResult(
            incident_type=incident.TYPE_OTHER, confidence=CONF_NONE,
            method=METHOD_DETERMINISTIC, evidence=("no incident text supplied",),
        )

    hits: list[tuple[str, str]] = []
    for pattern, kind in _COMPILED:
        found = pattern.search(text)
        if found:
            hits.append((kind, found.group(0)))

    if hits:
        kinds = {k for k, _ in hits}
        resolved = _resolve(kinds)
        evidence = tuple(f"matched {matched!r} -> {k}" for k, matched in hits[:5])
        if resolved is None:
            return ClassificationResult(
                incident_type=None, confidence=CONF_NONE, method=METHOD_DETERMINISTIC,
                evidence=evidence + (f"competing readings: {sorted(kinds)}",),
                ambiguous=True, candidates=tuple(sorted(kinds)),
            )
        return ClassificationResult(
            incident_type=resolved, confidence=CONF_PHRASE,
            method=METHOD_DETERMINISTIC, evidence=evidence,
        )

    words = set(_WORD.findall(text))
    keyword_hits = {(_KEYWORDS[w], w) for w in words & _KEYWORDS.keys()}
    if keyword_hits:
        kinds = {k for k, _ in keyword_hits}
        resolved = _resolve(kinds)
        evidence = tuple(
            f"keyword {w!r} -> {k}" for k, w in sorted(keyword_hits, key=lambda h: h[1])[:5]
        )
        if resolved is None:
            return ClassificationResult(
                incident_type=None, confidence=CONF_NONE, method=METHOD_DETERMINISTIC,
                evidence=evidence + (f"competing readings: {sorted(kinds)}",),
                ambiguous=True, candidates=tuple(sorted(kinds)),
            )
        return ClassificationResult(
            incident_type=resolved, confidence=CONF_KEYWORD,
            method=METHOD_DETERMINISTIC, evidence=evidence,
        )

    return ClassificationResult(
        incident_type=incident.TYPE_OTHER, confidence=CONF_NONE,
        method=METHOD_DETERMINISTIC,
        evidence=("no deterministic rule matched the incident text",),
    )


def _resolve(kinds: set[str]) -> str | None:
    """One type, or None where the report genuinely describes competing things."""
    if len(kinds) == 1:
        return next(iter(kinds))
    if len(kinds) == 2 and (winner := _SUBSUMES.get(frozenset(kinds))):
        return winner
    # Three or more readings, or two that do not subsume: a human decides. Picking the
    # most frequent would be guessing, and the type drives the containment plan.
    return None


def coerce_model_type(value: str | None) -> str | None:
    """Hold a model's answer to the same closed vocabulary the rules use.

    Returns None for anything outside it, so an invented type is discarded rather than
    written to the incident. `unclassified` is rejected too: it is the pre-
    classification default, never an outcome a model may choose.
    """
    if not value:
        return None
    candidate = value.strip().lower().replace(" ", "_").replace("-", "_")
    return candidate if candidate in incident.CLASSIFIABLE_TYPES else None
