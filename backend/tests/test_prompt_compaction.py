"""The evidence compaction must shrink the prompt without losing the finding.

Measured baseline this file defends, on a real hubspot.com scan (828 trackers, 124
cookies, 173 policies, 25 pages), counted with the model's own tokenizer:

    before   23,017 tokens   (84% of it scan evidence)
    after     6,672 tokens

and on a typical site (prepmyevent.com, 142 trackers) 3,978 tokens.

The risk in every one of those cuts is the same: that something which WAS evidence of
a violation stopped reaching the model. So these tests are about what survives, not
about the size. The one test that asserts a size does it as a ratio against the old
representation, because an absolute token count would pin the suite to one tokenizer.
"""

import json

import pytest

from app.llm import prompts


def _tracker(host="ads.example.com", states=("pre_consent",), category=None, vendor=None):
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "scan_id": "00000000-0000-0000-0000-000000000002",
        "script_src": f"https://{host}/static/bundle.a9f3c2e1b8d7.js",
        "consent_states": list(states),
        "category": category,
        "vendor": vendor,
    }


def _cookie(name="_ga", expiry="2027-09-24 04:56:11.076507+00:00", states=("pre_consent",)):
    return {
        "id": "00000000-0000-0000-0000-000000000003",
        "scan_id": "00000000-0000-0000-0000-000000000002",
        "name": name,
        "domain": "example.com",
        "path": "/",
        "expiry": expiry,
        "is_first_party": False,
        "category": "marketing",
        "vendor": "Google",
        "source": "lookup",
        "consent_states": list(states),
    }


# ── What must never be lost ─────────────────────────────────────────────────────

def test_a_post_reject_tracker_survives_compaction():
    """The product exists to find tracking that continues after the visitor says no.
    If a cut ever loses this, nothing else in the optimization matters."""
    evidence = {"trackers": [_tracker(states=("post_reject",))]}
    rendered = prompts.render_evidence(prompts.compact_scan_evidence(evidence))
    assert "ads.example.com" in rendered
    assert "rej" in rendered


def test_the_violating_items_are_the_ones_kept_when_the_cap_bites():
    """A cap must drop post-accept context, never pre-consent or post-reject evidence."""
    cap = prompts._ITEM_CAPS["trackers"]
    benign = [_tracker(host=f"cdn{i}.example.com", states=("post_accept",)) for i in range(cap * 2)]
    violating = _tracker(host="tracker-that-matters.example.com", states=("post_reject",))
    rendered = prompts.render_evidence(prompts.compact_scan_evidence({"trackers": [*benign, violating]}))
    assert "tracker-that-matters.example.com" in rendered


def test_a_capped_collection_says_so_in_the_prompt():
    """A prompt that quietly drops items invites the model to conclude the site is
    cleaner than it is."""
    cap = prompts._ITEM_CAPS["cookies"]
    compacted = prompts.compact_scan_evidence({"cookies": [_cookie(name=f"c{i}") for i in range(cap + 25)]})
    assert compacted["cookies_total"] == cap + 25
    assert compacted["cookies_omitted"] == 25
    rendered = prompts.render_evidence(compacted)
    assert "do not conclude they are absent" in rendered
    assert f"of {cap + 25} collected" in rendered


def test_consent_states_survive_the_abbreviation():
    """`pre`/`acc`/`rej` are only safe if they are complete and unambiguous."""
    row = prompts.compact_scan_evidence(
        {"trackers": [_tracker(states=("pre_consent", "post_accept", "post_reject"))]}
    )["trackers"][0]
    assert row["states"] == "pre,acc,rej"


def test_tracker_grouping_preserves_the_count_not_just_the_host():
    """Collapsing 83 webpack chunks into one row must not lose that there were 83."""
    evidence = {"trackers": [_tracker() for _ in range(83)]}
    rows = prompts.compact_scan_evidence(evidence)["trackers"]
    assert len(rows) == 1
    assert rows[0]["n"] == 83


def test_the_same_host_in_different_consent_states_is_not_merged():
    """A host seen only after Accept and a host seen before consent are different
    compliance facts. Merging them would erase a violation."""
    evidence = {"trackers": [_tracker(states=("pre_consent",)), _tracker(states=("post_accept",))]}
    rows = prompts.compact_scan_evidence(evidence)["trackers"]
    assert {r["states"] for r in rows} == {"pre", "acc"}


def test_every_item_keeps_a_citable_local_id():
    """Findings cite evidence by local_id; an item with none cannot be referenced."""
    compacted = prompts.compact_scan_evidence(
        {"trackers": [_tracker()], "cookies": [_cookie()], "pages": [{"url": "https://x.test"}]}
    )
    for key in ("trackers", "cookies", "pages"):
        assert all(item["local_id"] for item in compacted[key])


# ── What must be gone ───────────────────────────────────────────────────────────

def test_database_identifiers_never_reach_the_model():
    rendered = prompts.render_evidence(
        prompts.compact_scan_evidence({"trackers": [_tracker()], "cookies": [_cookie()]})
    )
    assert "00000000-0000-0000-0000-000000000002" not in rendered


def test_the_bundle_hash_is_dropped_but_the_host_is_kept():
    """`host` already carries the vendor signal; the rest of the path is a build
    artefact that no compliance judgment can use."""
    rendered = prompts.render_evidence(prompts.compact_scan_evidence({"trackers": [_tracker()]}))
    assert "ads.example.com" in rendered
    assert "bundle.a9f3c2e1b8d7.js" not in rendered


def test_cookie_expiry_becomes_a_duration_not_a_timestamp():
    row = prompts.compact_scan_evidence({"cookies": [_cookie()]})["cookies"][0]
    assert "2027-09-24" not in json.dumps(row)
    assert row["ttl"]


@pytest.mark.parametrize("expiry,expected", [
    (None, "session"), ("", "session"), ("session", "session"), ("not-a-date", "unknown"),
])
def test_ttl_handles_missing_and_malformed_expiry(expiry, expected):
    """Scanner data is not guaranteed well-formed, and a crash here would take down
    the whole analysis for one bad cookie row."""
    assert prompts._ttl(expiry) == expected


def test_policy_urls_lose_their_campaign_query_strings():
    compacted = prompts.compact_scan_evidence(
        {"policies": [{"url": "https://x.test/privacy?hubs_content=a&hubs_content-cta=b",
                       "policy_type": "privacy_policy",
                       "extracted_text_ref": "s3://bucket/very/long/key/0001"}]}
    )
    row = compacted["policies"][0]
    assert row["url"] == "https://x.test/privacy"
    # A database pointer the model cannot dereference.
    assert "extracted_text_ref" not in json.dumps(row)


def test_double_encoded_json_columns_are_decoded_once():
    """`"domains":"[\\"bing.com\\"]"` spends a token on every backslash escaping an
    escape."""
    rendered = prompts.render_evidence(prompts.compact_scan_evidence(
        {"third_party_services": [{"service_name": "x.test", "domains": '["a.test","b.test"]'}]}
    ))
    assert "\\" not in rendered


def test_a_service_named_after_its_only_domain_is_not_stated_twice():
    row = prompts.compact_scan_evidence(
        {"third_party_services": [{"service_name": "bing.com", "domains": '["bing.com"]'}]}
    )["third_party_services"][0]
    assert "domains" not in row


# ── The table format itself ─────────────────────────────────────────────────────

def test_the_table_states_its_columns_once_and_rows_line_up():
    rendered = prompts.render_evidence(prompts.compact_scan_evidence(
        {"cookies": [_cookie(name="a"), _cookie(name="b")]}
    ))
    lines = [ln for ln in rendered.splitlines() if "|" in ln]
    header, *rows = lines
    assert header.startswith("local_id|")
    assert rows, "no data rows rendered"
    assert all(len(r.split("|")) == len(header.split("|")) for r in rows), (
        "a row has a different column count than the header, so values are "
        "attributed to the wrong fields"
    )


def test_a_value_containing_a_pipe_cannot_break_the_columns():
    rendered = prompts.render_evidence(prompts.compact_scan_evidence(
        {"cookies": [{"name": "we|ird", "domain": "x.test", "expiry": None}]}
    ))
    lines = [ln for ln in rendered.splitlines() if "|" in ln]
    header, *rows = lines
    assert all(len(r.split("|")) == len(header.split("|")) for r in rows)


def test_a_column_nothing_populates_is_not_emitted():
    """An always-empty column is a header plus one delimiter per row for no
    information."""
    rendered = prompts.render_evidence(prompts.compact_scan_evidence(
        {"pages": [{"url": "https://x.test", "title": "T", "http_status": 200}]}
    ))
    # 200 is the default and is deliberately not stated.
    assert "status" not in rendered.splitlines()[1]


def test_empty_collections_are_stated_rather_than_omitted():
    """"none detected" is a finding; a missing section is ambiguous."""
    assert "none detected" in prompts.render_evidence(prompts.compact_scan_evidence({"cookies": []}))


# ── The size claim ──────────────────────────────────────────────────────────────

def test_compaction_is_a_large_reduction_against_the_raw_evidence():
    """Ratio, not an absolute token count, so this does not pin the suite to one
    tokenizer. Characters track tokens closely enough for a 5x claim."""
    raw = {"trackers": [_tracker(host=f"h{i}.example.com") for i in range(200)],
           "cookies": [_cookie(name=f"ck{i}") for i in range(120)]}
    before = len(json.dumps(raw, default=str))
    after = len(prompts.render_evidence(prompts.compact_scan_evidence(raw)))
    assert after * 5 < before, f"expected >5x reduction, got {before} -> {after}"


def test_consent_signals_stay_structured():
    """Their nested accept/reject interaction evidence decides whether a consent state
    was ever really established -- flattening it into columns would lose the nesting
    that the prompt's own rules depend on."""
    signals = [{"cmp_name": "OneTrust",
                "evidence": {"accept_interaction": "clicked", "reject_interaction": "click_failed"}}]
    rendered = prompts.render_evidence(prompts.compact_scan_evidence({"consent_signals": signals}))
    assert "accept_interaction" in rendered
    assert "click_failed" in rendered


def test_evidence_stats_report_collected_versus_sent():
    """Phase 12 instrumentation: a scan has to be diagnosable from its own audit trail."""
    cap = prompts._ITEM_CAPS["cookies"]
    raw = {"cookies": [_cookie(name=f"c{i}") for i in range(cap + 10)]}
    stats = prompts.evidence_stats(raw, prompts.compact_scan_evidence(raw))
    assert stats["cookies"] == {"collected": cap + 10, "sent": cap, "capped": True}
