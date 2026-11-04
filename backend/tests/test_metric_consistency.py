"""One definition of each count, used everywhere (FR-20261027-D200: the finding said 9, the consent table 6 requests and 3 cookies).

Requests and cookies are different evidence and are counted apart; the finding text, the score check, the rule finding and the review item
all read the same evidence through `count_phrase`. The score is a pure function of the stored evidence."""

import random
from datetime import date

from app.rules.consent_rules import build_consent_context, count_phrase, evaluate_consent_rules
from app.scoring.requirements import assess

TODAY = date(2026, 10, 27)


def evidence(trackers, cookies):
    ev = {"accept_interaction": "clicked", "reject_interaction": "clicked", "reject_click_confirmed": True, "banner_ui_observed": True}
    return {
        "pages": [{"id": f"p{i}", "url": f"https://x.test/{i}", "title": None, "discovered_via": "link"} for i in range(5)],
        "forms": [], "third_party_services": [],
        "consent_signals": [{"mechanism_type": "cmp", "cmp_vendor": "Consiva", "has_reject_all": True, "has_granular_choices": True, "evidence": ev}],
        "policies": [{"id": "pol", "url": "https://x.test/p", "policy_type": "privacy_policy"}],
        "cookies": cookies, "trackers": trackers,
    }


def tracker(i, category="analytics", states=("pre_consent", "post_accept")):
    return {"id": f"t{i}", "script_src": f"https://t{i}.test/c?x={i}", "vendor": "V", "category": category, "source": "rule", "consent_states": list(states)}


def cookie(i, category="analytics", states=("pre_consent", "post_accept")):
    return {"id": f"c{i}", "name": f"_c{i}", "domain": "x.test", "category": category, "consent_states": list(states)}


# The D200 shape: 6 classified requests, 3 classified cookies, 3 unclassified requests, plenty of functional ones.
D200 = evidence(
    [tracker(i) for i in range(6)] + [tracker(10 + i, category=None) for i in range(3)] + [tracker(20 + i, "functional", ("pre_consent", "post_accept", "post_reject")) for i in range(5)],
    [cookie(i) for i in range(3)],
)


def test_requests_and_cookies_are_counted_apart():
    ctx = build_consent_context(D200)
    hits = [e for e in ctx.non_essential if "pre_consent" in e["consent_states"]]
    assert len(hits) == 9
    assert count_phrase(ctx, hits) == "6 analytics/marketing request(s) and 3 analytics/marketing cookie(s)"


def test_phrase_names_only_what_exists():
    only_requests = build_consent_context(evidence([tracker(1), tracker(2)], []))
    assert count_phrase(only_requests, only_requests.non_essential) == "2 analytics/marketing request(s)"
    only_cookies = build_consent_context(evidence([], [cookie(1)]))
    assert count_phrase(only_cookies, only_cookies.non_essential) == "1 analytics/marketing cookie(s)"


def test_c1_finding_and_rule_finding_state_the_same_counts():
    c1 = next(r for r in assess(D200, TODAY).requirements if r.id == "DPDP-C1")
    assert c1.status == "not_met"
    assert "6 analytics/marketing request(s) and 3 analytics/marketing cookie(s)" in c1.observations[0]
    assert len(c1.evidence_ids) == 9
    r001 = next(f for f in evaluate_consent_rules(D200) if f.rule_id == "R-001")
    assert "6 analytics/marketing request(s) and 3 analytics/marketing cookie(s) fired BEFORE any consent interaction" in r001.summary


def test_the_old_combined_wording_is_gone():
    c1 = next(r for r in assess(D200, TODAY).requirements if r.id == "DPDP-C1")
    assert "cookie(s)/script(s)" not in c1.observations[0]


def test_unclassified_requests_stay_unclassified():
    """The three unclassified requests are not counted as analytics or marketing, and not hidden either."""
    ctx = build_consent_context(D200)
    assert all(e["category"] in ("analytics", "marketing") for e in ctx.non_essential)
    assert sum(1 for e in (*ctx.trackers, *ctx.cookies) if e["category"] is None) == 3


def test_a_site_with_no_pre_consent_activity_is_met():
    """The 1DB8 shape: the same tags, but only after Accept. Nothing is suppressed to get there: the evidence is what differs."""
    quiet = evidence([tracker(i, states=("post_accept",)) for i in range(6)], [cookie(i, states=("post_accept",)) for i in range(3)])
    assert next(r for r in assess(quiet, TODAY).requirements if r.id == "DPDP-C1").status == "met"


def test_statuses_are_deterministic_and_independent_of_record_order():
    first = assess(D200, TODAY)
    for seed in range(5):
        shuffled = {**D200, "trackers": random.Random(seed).sample(D200["trackers"], len(D200["trackers"])), "cookies": list(reversed(D200["cookies"]))}
        again = assess(shuffled, TODAY)
        assert [(r.id, r.status) for r in again.requirements] == [(r.id, r.status) for r in first.requirements]


def test_statuses_move_only_for_the_checkpoint_that_changed():
    """D200 -> 1DB8: C1 Not Met -> Met is the whole difference."""
    before = assess(D200, TODAY)
    quiet = evidence(
        [tracker(i, states=("post_accept",)) for i in range(6)] + [tracker(10 + i, category=None, states=("post_accept",)) for i in range(3)]
        + [t for t in D200["trackers"] if t["category"] == "functional"],
        [cookie(i, states=("post_accept",)) for i in range(3)],
    )
    after = assess(quiet, TODAY)
    changed = {a.id for a, b in zip(after.requirements, before.requirements, strict=True) if a.status != b.status}
    assert changed == {"DPDP-C1"}
