"""Consent parameters C4 to C8, assessed from what the browser observed in separate consent sessions. No model is involved.

A test that could not be carried out (no banner found, a control not found, the page did not load) is NOT ASSESSED, never a failure of the site. Only an observation that a tracker fired,
or that a control was absent from a banner that was found, can lose points, and a "not found" that may be the scanner's own miss goes to a person (low confidence), not to Not met.

These are the first reading of each parameter's pass, partial and fail. They are objective, but they have not yet been reviewed parameter by parameter (see
docs/ENTERPRISE_SCORING_METHOD.md), and they are written here, in one place, so that review can change them. Requests are classified by the catalogue the 5-page scanner uses; cookies by
the Open Cookie Database; neither is judged by a model.

  C4  No analytics or advertising before a choice. Every crawled page was loaded in a fresh session that had given no consent.
      not met: any page contacted an analytics or advertising service, or set a cookie classified analytics or marketing, before any choice.
      met: none did, on every crawled page. not assessed: no page was read, or the evidence predates classification.
  C5  Reject blocks them. Reject, then watch the entry page, then up to two more pages in the same session.
      not met: an analytics or advertising service was contacted after Reject on the entry page. partly met: only on a later page. met: none. not assessed: Reject could not be given.
  C6  (supporting) Reject offered with the same ease as Accept. met: the two first-layer controls are comparable in size and prominence. not met: they are not.
      not assessed: no banner, or either control was not found.
  C7  Withdrawal as easy as giving. Accept, find the control that reopens the choice, then the control that withdraws it.
      met: both found. partly met: the choice reopens but the dialog offers no control that withdraws it. not met (low confidence, so a person reviews it): no control to reopen the choice was
      found after Accept. not assessed: Accept could not be given, or the page did not load.
  C8  After withdrawal, processing stops. Watch the page after withdrawing, then up to two more pages.
      not met: analytics or advertising is contacted after withdrawal. partly met: only on a later page. met: nothing is, and Accept had started something to stop.
      not applicable: Accept started nothing, so there was nothing to stop. not assessed: withdrawal was not tested.
  C10 A consent mechanism wherever analytics or advertising runs. met: a banner was found (Accept or Reject could be given). not met: analytics or advertising was observed and no
      banner was found. not applicable: no analytics or advertising was observed. not assessed: the entry page did not load.
  C11 Consent for each purpose separately (the purposes test). met: the preferences offer a choice per purpose. partly met: a banner, but no choice per purpose. not met: analytics or
      advertising runs with no banner at all. not applicable: no analytics or advertising was observed and no banner was found.

Method version 2 (owner, 19 Nov 2026): where no analytics or advertising was observed at all and no banner was found, C5 to C8, C10 and C11 do not apply (a site with nothing that needs
consent needs no banner) and are recorded as not applicable, which the engine reports as Pass with that reason. Where analytics or advertising runs with no banner, there is no way to
refuse or withdraw, so C5, C7 and C8 are not met on the evidence of what ran.
"""

from __future__ import annotations

from collections.abc import Callable

from app.enterprise.assess.technical import MAX_REFS, Ev
from app.enterprise.scoring.engine import MET, NOT_APPLICABLE, NOT_ASSESSED, NOT_MET, PARTLY, Assessment

REASONS = {
    "homepage_not_loaded": "the entry page did not load", "page_not_loaded": "the entry page did not load", "accept_not_clicked": "Accept could not be given",
    "reject_not_clicked": "Reject could not be given", "control_not_found": "no banner control was found", "click_failed": "the control was found but could not be clicked",
    "no_reopen_control": "no control to reopen the choice was found", "no_withdraw_control": "the reopened dialog offered no control that withdraws consent",
    "banner_or_controls_not_found": "no banner, or one of its controls, was found", "error": "the test could not be completed", "no_preferences_control": "no preferences control was found",
}
Classifier = Callable[[str, str | None], str | None]


def _why(doc: dict) -> str:
    return REASONS.get(doc.get("reason") or "", doc.get("reason") or "the test did not run")


def _refs(*items: Ev | None) -> tuple[str, ...]:
    return tuple(f"ev:{i[0]}" for i in items if i)[:MAX_REFS]


def _vendors(items: list[dict]) -> str:
    return ", ".join(sorted({f"{v['vendor'] or v['host']} ({v['category']})" for v in items}))[:300]


def _c4(networks: list[Ev], cookies: list[Ev], classify: Classifier) -> Assessment:
    if not networks:
        return Assessment("C4", NOT_ASSESSED, note="no page was read")
    if any("non_essential" not in n for _, n in networks):
        return Assessment("C4", NOT_ASSESSED, note="this evidence predates the classification of requests before consent")
    firing = [(i, n) for i, n in networks if n["non_essential"]]
    cookie_hits: list[tuple[str, str]] = []
    for ev_id, e in cookies:
        for c in e.get("cookies", []):
            if c.get("consent_state") == "pre_consent" and classify(c["name"], c["domain"]) in ("analytics", "marketing"):
                cookie_hits.append((ev_id, c["name"]))
    if firing or cookie_hits:
        parts = []
        if firing:
            parts.append(f"{len(firing)} of {len(networks)} pages contacted analytics or advertising services before any choice ({_vendors([v for _, n in firing for v in n['non_essential']])})")
        if cookie_hits:
            parts.append(f"{len({n for _, n in cookie_hits})} analytics or marketing cookies were set before any choice ({', '.join(sorted({n for _, n in cookie_hits})[:6])})")
        refs = tuple(dict.fromkeys([f"ev:{i}" for i, _ in firing] + [f"ev:{i}" for i, _ in cookie_hits]))[:MAX_REFS]
        return Assessment("C4", NOT_MET, refs, note="; ".join(parts))
    return Assessment("C4", MET, tuple(f"ev:{i}" for i, _ in networks[:MAX_REFS]), note=f"none of {len(networks)} pages contacted an analytics or advertising service or set such a cookie before a choice")


def _c5(tests: dict[str, Ev]) -> Assessment:
    ev = tests.get("reject_persistence")
    if ev is None or not ev[1].get("tested"):
        return Assessment("C5", NOT_ASSESSED, note=f"Reject was not tested: {_why(ev[1]) if ev else 'the consent tests did not run'}")
    d = ev[1]
    after, later = d.get("non_essential_after_reject", []), [x for p in d.get("further_pages", []) for x in p.get("non_essential", [])]
    refs = _refs(ev, tests.get("reject"))
    if after:
        return Assessment("C5", NOT_MET, refs, note=f"after Reject the entry page contacted {_vendors(after)}")
    if later:
        return Assessment("C5", PARTLY, refs, note=f"nothing after Reject on the entry page, but a later page contacted {_vendors(later)}")
    return Assessment("C5", MET, refs, note=f"nothing analytics or advertising after Reject on the entry page and {len(d.get('further_pages', []))} further pages")


def _c6(tests: dict[str, Ev]) -> Assessment:
    ev = tests.get("button_comparison")
    if ev is None or "comparable" not in ev[1]:
        return Assessment("C6", NOT_ASSESSED, note=f"the buttons were not compared: {_why(ev[1]) if ev else 'the consent tests did not run'}")
    return Assessment("C6", MET if ev[1]["comparable"] else NOT_MET, _refs(ev), note=str(ev[1].get("why") or ""))


def _c7(tests: dict[str, Ev]) -> Assessment:
    ev = tests.get("withdrawal")
    if ev is None:
        return Assessment("C7", NOT_ASSESSED, note="the consent tests did not run")
    d, reason = ev[1], ev[1].get("reason")
    if d.get("tested"):
        return Assessment("C7", MET, _refs(ev), note=f"after Accept the choice was reopened ({d.get('reopen_control')}) and withdrawn ({d.get('withdraw_control')})")
    if reason == "no_withdraw_control":
        return Assessment("C7", PARTLY, _refs(ev), note=f"the choice reopened ({d.get('reopen_control')}) but the dialog offered no control that withdraws consent")
    if reason == "no_reopen_control":
        return Assessment("C7", NOT_MET, _refs(ev), confidence="low", note="after Accept, no control to reopen the choice was found; a person should confirm this on the site")
    return Assessment("C7", NOT_ASSESSED, note=f"withdrawal was not tested: {_why(d)}")


def _c8(tests: dict[str, Ev]) -> Assessment:
    ev = tests.get("withdrawal")
    if ev is None or not ev[1].get("tested"):
        return Assessment("C8", NOT_ASSESSED, note=f"withdrawal was not tested: {_why(ev[1]) if ev else 'the consent tests did not run'}")
    d = ev[1]
    started = d.get("accepted_non_essential", [])
    if not started:
        return Assessment("C8", NOT_APPLICABLE, _refs(ev), note="Accept started no analytics or advertising, so there was nothing for withdrawal to stop")
    after, later = d.get("after_withdrawal", []), [x for p in d.get("further_pages", []) for x in p.get("non_essential", [])]
    refs = _refs(ev)
    if after:
        return Assessment("C8", NOT_MET, refs, note=f"after withdrawal the page still contacted {_vendors(after)} (Accept had started {len(started)} services)")
    if later:
        return Assessment("C8", PARTLY, refs, note=f"stopped on the page, but a later page contacted {_vendors(later)}")
    return Assessment("C8", MET, refs, note=f"Accept started {len(started)} services ({_vendors(started)}); after withdrawal none was contacted, on the page or on {len(d.get('further_pages', []))} further pages")


_NOT_LOADED = ("page_not_loaded", "homepage_not_loaded", "error")


def _banner_seen(tests: dict[str, Ev]) -> bool | None:
    """True when Accept or Reject could be given (a banner exists); False when both tests ran and found no control; None when the entry page did not load or the tests did not run."""
    results = [tests[n][1] for n in ("accept", "reject") if n in tests]
    if any(r.get("tested") for r in results):
        return True
    if not results or all((r.get("reason") or "") in _NOT_LOADED for r in results):
        return None
    return False


def _consent_dependent(networks: list[Ev], tests: dict[str, Ev]) -> list[tuple[str, list[dict]]]:
    """(evidence id, the analytics or advertising services) wherever such services ran: before any choice on a crawled page, or after Accept."""
    hits = [(i, n["non_essential"]) for i, n in networks if n.get("non_essential")]
    acc = tests.get("accept")
    if acc and acc[1].get("non_essential"):
        hits.append((acc[0], acc[1]["non_essential"]))
    return hits


def _c10(networks: list[Ev], tests: dict[str, Ev]) -> Assessment:
    banner, hits = _banner_seen(tests), _consent_dependent(networks, tests)
    if banner:
        return Assessment("C10", MET, _refs(tests.get("accept"), tests.get("reject")), note="a consent banner was found: Accept or Reject could be given on the entry page")
    if hits:
        refs = tuple(dict.fromkeys(f"ev:{i}" for i, _ in hits))[:MAX_REFS]
        return Assessment("C10", NOT_MET, refs, note=f"analytics or advertising ran ({_vendors([v for _, vs in hits for v in vs])}) and no consent banner was found")
    if banner is None:
        return Assessment("C10", NOT_ASSESSED, note="the entry page did not load in the consent sessions, so whether a banner exists could not be verified")
    return Assessment("C10", NOT_APPLICABLE, tuple(f"ev:{i}" for i, _ in networks[:MAX_REFS]), note="no analytics or advertising was observed, so no consent mechanism is needed")


def _c11(networks: list[Ev], tests: dict[str, Ev]) -> Assessment:
    banner, hits = _banner_seen(tests), _consent_dependent(networks, tests)
    ev = tests.get("purposes")
    d = ev[1] if ev else {}
    if d.get("tested") and d.get("purposes"):
        kinds = ", ".join(sorted({p.get("label") or p.get("kind") or "?" for p in d["purposes"]}))[:200]
        return Assessment("C11", MET, _refs(ev), note=f"the consent preferences offer a choice per purpose ({kinds})")
    if banner:
        why = {"no_preferences_control": "no preferences control was found", "no_purpose_choices": "the preferences offer no choice per purpose"}.get(d.get("reason") or "", _why(d) if d else "the purposes test did not run")
        return Assessment("C11", PARTLY, _refs(ev, tests.get("accept"), tests.get("reject")), note=f"a consent banner was found, but no choice per purpose: {why}")
    if hits:
        refs = tuple(dict.fromkeys(f"ev:{i}" for i, _ in hits))[:MAX_REFS]
        return Assessment("C11", NOT_MET, refs, note="analytics or advertising ran with no consent banner, so no choice of any purpose was offered")
    if banner is None:
        return Assessment("C11", NOT_ASSESSED, note="the entry page did not load in the consent sessions, so the choices offered could not be verified")
    return Assessment("C11", NOT_APPLICABLE, tuple(f"ev:{i}" for i, _ in networks[:MAX_REFS]), note="no analytics or advertising was observed, so there is no purpose to choose")


def _no_banner_needed(networks: list[Ev], tests: dict[str, Ev], a: Assessment) -> Assessment:
    """C5 to C8 when no banner was found: they do not apply if nothing needs consent; if analytics or advertising ran, there was no way to refuse or withdraw."""
    if a.state != NOT_ASSESSED or _banner_seen(tests) is not False:
        return a
    hits = _consent_dependent(networks, tests)
    if not hits:
        return Assessment(a.parameter_id, NOT_APPLICABLE, tuple(f"ev:{i}" for i, _ in networks[:MAX_REFS]),
                          note="no analytics or advertising was observed and no consent banner exists, so there is nothing to refuse or withdraw")
    if a.parameter_id == "C6":
        return a                                                # the buttons cannot be compared; C10 records the missing banner
    refs = tuple(dict.fromkeys(f"ev:{i}" for i, _ in hits))[:MAX_REFS]
    return Assessment(a.parameter_id, NOT_MET, refs, note=f"analytics or advertising ran ({_vendors([v for _, vs in hits for v in vs])}) with no consent banner, so it could not be refused or withdrawn")


def assess_consent(*, networks: list[Ev], cookies: list[Ev], tests: dict[str, Ev], classify: Classifier) -> list[Assessment]:
    """`networks` and `cookies` are the per-page pre-consent records of the crawl; `tests` the consent suite's results by name."""
    adjusted = [_no_banner_needed(networks, tests, a) for a in (_c5(tests), _c6(tests), _c7(tests), _c8(tests))]
    return [_c4(networks, cookies, classify), *adjusted, _c10(networks, tests), _c11(networks, tests)]
