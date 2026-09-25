"""Detects the presence/shape of a consent mechanism (banner or CMP) on the site.

Not one of the five detector modules named in docs/architecture §5, but a direct,
necessary split from policy_detector.py: that module finds policy *documents*, this
one finds the interactive consent *mechanism* — `consent_signals` is explicitly part
of the required scan output shape.
"""

from app.rules.tracker_catalog import match_cmp
from app.scanner.page_parser import ParsedScript
from app.scanner.schemas import ConsentSignalRecord

# Phrases that, on a clickable control, evidence a consent mechanism BY THEMSELVES.
# Each names the consent action explicitly; none is ordinary page furniture.
_ACCEPT_HINTS = ("accept all", "allow all", "accept cookies", "allow cookies", "agree and continue")
_REJECT_HINTS = ("reject all", "decline all", "reject non-essential", "deny all",
                 "reject cookies", "refuse all")
_GRANULAR_HINTS = ("manage preferences", "cookie settings", "cookie preferences",
                   "manage cookies", "privacy preferences")

# Words that REFINE a consent mechanism once one is established, but cannot establish
# one alone. "agree", "customize" and "preferences" are ordinary English that appear on
# perfectly normal controls -- an account page's "Preferences" link, a "Customize" button
# on a product configurator. Treating them as sufficient is what produced a fabricated
# consent banner on news.ycombinator.com; they are still read, but only to fill in
# has_granular_choices once a strong phrase has established the banner exists.
_WEAK_GRANULAR_HINTS = ("customize", "preferences")
_WEAK_ACCEPT_HINTS = ("agree", "got it", "ok")


def detect_consent_signal(
    *,
    scripts: list[ParsedScript],
    visible_text: str,
    detected_global_vars: list[str],
    control_text: str | None = None,
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
    guessed from the generic keyword path.

    `control_text` is the text of clickable controls and consent-dialog containers
    (page_parser.extract_control_text). The keyword paths read THAT, not the page's
    prose. Reading prose reported a consent banner on news.ycombinator.com -- a site
    with no consent UI at all and zero matches for cookie/consent/gdpr in its HTML --
    because a user comment said "both parties agree on this", and the pipeline turned
    that into a high-priority finding about a Reject control that does not exist.
    A consent banner is a thing you click, so only clickable things are evidence of one.

    Falls back to `visible_text` when `control_text` is not supplied, so existing
    callers keep working; every caller in the scanner passes it."""
    text = (control_text if control_text is not None else visible_text).lower()

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
    has_accept = any(h in text for h in _ACCEPT_HINTS)
    # A strong granular phrase stands alone; a weak one only counts once something
    # else has already established that a consent mechanism is present.
    has_strong_granular = any(h in text for h in _GRANULAR_HINTS)
    established = has_reject_all or has_accept or has_strong_granular
    has_granular = has_strong_granular or (
        established and any(h in text for h in _WEAK_GRANULAR_HINTS)
    )
    if established:
        has_accept = has_accept or any(h in text for h in _WEAK_ACCEPT_HINTS)

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
    if established:
        return ConsentSignalRecord(
            mechanism_type="banner",
            has_reject_all=has_reject_all,
            has_granular_choices=has_granular,
            evidence={
                "matched_keywords": [
                    h for h in _ACCEPT_HINTS + _REJECT_HINTS + _GRANULAR_HINTS if h in text
                ],
                "confidence": 0.5, "detection_source": "control_text_keywords",
                # Said plainly in the record, because a 0.5 signal was being read
                # downstream as an established fact about the page.
                "caveat": (
                    "Matched consent wording on clickable controls; no CMP vendor "
                    "signature was found. Treat as a POSSIBLE consent mechanism to be "
                    "confirmed by a person, not as a confirmed banner."
                ),
            },
        )
    return ConsentSignalRecord(
        mechanism_type="none", has_reject_all=None, has_granular_choices=None,
        evidence={"confidence": 0.0, "detection_source": None},
    )
