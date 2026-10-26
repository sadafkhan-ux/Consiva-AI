"""One answer, everywhere: the same consent evidence gives the same verdict in the stored row, the evidence API, the consent rules and the DPDP score, for every consent tool.

Each scenario is run through the detector (as the scanner records it), through a stored row as an OLDER scanner would have written it (a footer link recorded as "no reject-all"), and through
the consumers. The invariants are the point: no consumer may say more than was observed.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.db.repositories import scan_repository
from app.rules.consent_observation import banner_displayed, banner_not_displayed, observed_signal
from app.rules.consent_rules import build_consent_context, evaluate_consent_rules
from app.rules.tracker_catalog import CMP_CATALOG
from app.scanner import crawler
from app.scanner.consent_signal_detector import detect_consent_signal
from app.scanner.page_parser import ParsedScript
from app.scoring.dpdp_score import assess

TODAY = datetime(2026, 10, 27, tzinfo=UTC).date()


def script(cmp):
    return ParsedScript(src=f"https://{cmp.domain_substrings[0]}/consent.js", inline_snippet=None, executes=True)


def evidence(signal, *, accept, reject, confirmed=False, pages=1, legacy=False):
    """The evidence dict of one scan. `legacy` writes the signal the way the scanner did before it learned that a footer link is not a banner."""
    has_reject, has_granular = signal.has_reject_all, signal.has_granular_choices
    if legacy and signal.evidence.get("banner_ui_observed") is False:
        has_reject, has_granular = False, True
    row = {
        "mechanism_type": signal.mechanism_type, "cmp_vendor": signal.cmp_vendor, "has_reject_all": has_reject, "has_granular_choices": has_granular,
        "evidence": {**signal.evidence, "accept_interaction": accept, "reject_interaction": reject, "reject_click_confirmed": confirmed, "accept_click_confirmed": accept == "clicked"},
    }
    return {
        "pages": [{"id": f"p{i}", "url": f"https://x.test/{i}", "discovered_via": "seed" if i == 0 else "link", "http_status": 200} for i in range(pages)],
        "forms": [], "cookies": [], "trackers": [], "third_party_services": [],
        "policies": [{"id": "pol", "url": "https://x.test/privacy", "policy_type": "privacy_policy"}],
        "consent_signals": [observed_signal(row)],
    }


def results(score):
    for attr in ("requirements", "results", "checks"):
        if hasattr(score, attr):
            return {r.id: r for r in getattr(score, attr)}
    raise AssertionError("the score carries no per-requirement results")


SCENARIOS = {
    # name: (page control text, accept outcome, reject outcome, reject confirmed, banner shown?, reject-all offered?)
    "tool loaded, nothing shown": ("", "cmp_not_automatable", "cmp_not_automatable", False, False, None),
    "tool loaded, footer link only": ("manage cookies", "cmp_not_automatable", "cmp_not_automatable", False, False, None),
    "banner with reject-all": ("accept all reject all manage preferences", "clicked", "clicked", True, True, True),
    "banner without reject-all, accept clicked": ("accept all manage preferences", "clicked", "not_found", False, True, False),
    "banner shown but not operable": ("accept all manage preferences", "cmp_not_automatable", "cmp_not_automatable", False, True, False),
}


@pytest.mark.parametrize("cmp", CMP_CATALOG, ids=lambda c: c.cmp_vendor)
@pytest.mark.parametrize("name", list(SCENARIOS))
@pytest.mark.parametrize("legacy", [False, True], ids=["stored-now", "stored-by-older-scanner"])
def test_every_consumer_agrees_on_what_was_observed(cmp, name, legacy):
    text, accept, reject, confirmed, shown, reject_offered = SCENARIOS[name]
    signal = detect_consent_signal(scripts=[script(cmp)], visible_text="", detected_global_vars=[], control_text=text)
    crawler._reconcile_banner_presence(signal, accept, reject)
    ev = evidence(signal, accept=accept, reject=reject, confirmed=confirmed, legacy=legacy)
    stored = ev["consent_signals"][0]
    ctx = build_consent_context(ev)
    rules = {f.rule_id for f in evaluate_consent_rules(ev)}

    if not shown:
        # nothing was observed about a banner: nothing may be said about it, anywhere
        assert ctx.banner_not_displayed and stored["banner_displayed"] is False
        assert stored["has_reject_all"] is None and stored["has_granular_choices"] is None
        assert "R-005" not in rules, "no missing-Reject finding for a banner that was never seen"
        by_id = results(assess(ev, TODAY))
        for rid in ("DPDP-C3", "DPDP-C4", "DPDP-C5"):
            assert by_id[rid].status == "unable_to_verify", (rid, by_id[rid].status)
    else:
        assert not ctx.banner_not_displayed
        assert stored["has_reject_all"] is reject_offered
        assert ("R-005" in rules) == (reject_offered is False), "a displayed banner that lacks Reject is reported, and one that has it is not"


class TestTheSharedRule:
    def test_only_a_consent_tool_with_an_unseen_banner_and_no_click_is_not_displayed(self):
        seen = {"banner_ui_observed": False}
        assert banner_not_displayed("cmp", seen)
        assert not banner_not_displayed("cmp", {**seen, "accept_interaction": "clicked"})
        assert not banner_not_displayed("cmp", {**seen, "reject_interaction": "clicked"})
        assert not banner_not_displayed("cmp", {"banner_ui_observed": True})
        assert not banner_not_displayed("cmp", {})                      # a scan from before it was recorded: unknown
        assert not banner_not_displayed("banner", seen)                 # a keyword-matched banner is judged on its wording
        assert not banner_not_displayed(None, None)

    def test_banner_displayed_is_three_valued(self):
        assert banner_displayed("cmp", {"banner_ui_observed": False}) is False
        assert banner_displayed("cmp", {"banner_ui_observed": True}) is True
        assert banner_displayed("cmp", {"banner_ui_observed": False, "accept_interaction": "clicked"}) is True
        assert banner_displayed("cmp", {}) is None
        assert banner_displayed("none", {}) is None

    def test_observed_signal_changes_only_what_a_banner_offers(self):
        row = {"mechanism_type": "cmp", "cmp_vendor": "X", "has_reject_all": False, "has_granular_choices": True, "evidence": {"banner_ui_observed": False}}
        out = observed_signal(row)
        assert out["has_reject_all"] is None and out["has_granular_choices"] is None and out["cmp_vendor"] == "X" and out["evidence"] == row["evidence"]
        assert row["has_reject_all"] is False, "the stored row itself is not modified"


class TestTheEvidenceApiAppliesIt:
    def test_serialised_evidence_has_no_observed_reject_all_for_an_unseen_banner(self):
        sig = SimpleNamespace(mechanism_type="cmp", cmp_vendor="X", has_reject_all=False, has_granular_choices=True, evidence={"banner_ui_observed": False})
        s = scan_repository._serialize_evidence([], [], [], [], [], [], [sig])["consent_signals"][0]
        assert s["has_reject_all"] is None and s["has_granular_choices"] is None and s["banner_displayed"] is False

    def test_a_displayed_banner_keeps_what_it_offers(self):
        sig = SimpleNamespace(mechanism_type="cmp", cmp_vendor="X", has_reject_all=False, has_granular_choices=True, evidence={"banner_ui_observed": True})
        s = scan_repository._serialize_evidence([], [], [], [], [], [], [sig])["consent_signals"][0]
        assert s["has_reject_all"] is False and s["has_granular_choices"] is True and s["banner_displayed"] is True


class TestMissingEvidenceIsNeverAVerdict:
    def test_no_consent_information_at_all_is_unable_to_verify_not_a_pass_or_fail(self):
        ev = {"pages": [{"id": "p", "url": "https://x.test/", "discovered_via": "seed", "http_status": 200}], "forms": [], "cookies": [], "trackers": [],
              "third_party_services": [], "policies": [], "consent_signals": []}
        by_id = results(assess(ev, TODAY))
        for rid in ("DPDP-C3", "DPDP-C4", "DPDP-C5"):
            assert by_id[rid].status in ("unable_to_verify", "not_applicable"), (rid, by_id[rid].status)      # never met, partially met or not met on no evidence

    def test_a_scan_with_no_loaded_page_has_no_score_and_nothing_verified(self):
        sig = detect_consent_signal(scripts=[script(CMP_CATALOG[0])], visible_text="", detected_global_vars=[], control_text="")
        s = assess(evidence(sig, accept="cmp_not_automatable", reject="cmp_not_automatable", pages=0), TODAY).readiness
        assert s.status == "insufficient_evidence" and s.score_percent is None
