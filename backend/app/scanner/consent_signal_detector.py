"""Detects the presence/shape of a consent mechanism (banner or CMP) on the site.

Not one of the five detector modules named in docs/architecture §5, but a direct,
necessary split from policy_detector.py: that module finds policy *documents*, this
one finds the interactive consent *mechanism* — `consent_signals` is explicitly part
of the required scan output shape.
"""

from app.rules.tracker_catalog import match_cmp
from app.scanner.page_parser import ParsedScript
from app.scanner.schemas import ConsentSignalRecord

_ACCEPT_HINTS = ("accept all", "allow all", "agree", "accept cookies")
_REJECT_HINTS = ("reject all", "decline all", "reject non-essential", "deny all")
_GRANULAR_HINTS = ("manage preferences", "cookie settings", "customize", "preferences", "manage cookies")


def detect_consent_signal(
    *,
    scripts: list[ParsedScript],
    visible_text: str,
    detected_global_vars: list[str],
) -> ConsentSignalRecord:
    """`evidence["confidence"]`/`evidence["detection_source"]` reflect how the
    mechanism was identified -- never a fabricated precision score, just an honest,
    documented ranking of the three detection paths in this function, from most to
    least specific:
      script_src (0.95)  -- an actual CMP loader script was observed; the strongest
                            signal since it names the vendor directly.
      global_var (0.85)  -- the CMP's own runtime object exists on the page, but no
                            matching script tag was seen (e.g. loaded some other way).
      text_keywords (0.5) -- generic accept/reject/preferences wording found; a real
                            banner-like UI, but the specific vendor is NOT claimed.
      none (0.0)          -- no consent mechanism detected at all.
    `cmp_vendor` is only ever set when match_cmp() actually identified one -- never
    guessed from the generic keyword path."""
    text = visible_text.lower()

    cmp_match = None
    detection_source = None
    for script in scripts:
        cmp_match = match_cmp(script_src=script.src, global_js_vars=tuple(detected_global_vars))
        if cmp_match:
            detection_source = "script_src"
            break
    if cmp_match is None and detected_global_vars:
        cmp_match = match_cmp(global_js_vars=tuple(detected_global_vars))
        if cmp_match:
            detection_source = "global_var"

    has_reject_all = any(h in text for h in _REJECT_HINTS)
    has_granular = any(h in text for h in _GRANULAR_HINTS)
    has_accept = any(h in text for h in _ACCEPT_HINTS)

    if cmp_match:
        confidence = 0.95 if detection_source == "script_src" else 0.85
        return ConsentSignalRecord(
            mechanism_type="cmp",
            cmp_vendor=cmp_match.cmp_vendor,
            has_reject_all=has_reject_all,
            has_granular_choices=has_granular,
            evidence={
                "matched_cmp": cmp_match.cmp_vendor, "global_vars": detected_global_vars,
                "confidence": confidence, "detection_source": detection_source,
            },
        )
    if has_accept or has_reject_all or has_granular:
        return ConsentSignalRecord(
            mechanism_type="banner",
            has_reject_all=has_reject_all,
            has_granular_choices=has_granular,
            evidence={
                "matched_keywords": [h for h in _ACCEPT_HINTS + _REJECT_HINTS + _GRANULAR_HINTS if h in text],
                "confidence": 0.5, "detection_source": "text_keywords",
            },
        )
    return ConsentSignalRecord(
        mechanism_type="none", has_reject_all=None, has_granular_choices=None,
        evidence={"confidence": 0.0, "detection_source": None},
    )
