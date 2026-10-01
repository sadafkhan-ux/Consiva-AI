"""Tracker classification stays deterministic, and the LLM cannot widen it.

§9 of the brief: if the deterministic rules say Google Fonts is functional, nothing
downstream may turn that into an analytics/marketing tracker without evidence. The
guarantee here is structural rather than advisory -- there is no code path by which
LLM output can write a tracker's category at all -- so these tests pin that absence,
which is the kind of property that is silently lost the moment somebody adds a
convenient "let the model fix the unclassified ones" step.
"""

import inspect

from app.rules.consent_rules import classify_tracker
from app.scanner.schemas import TrackerRecord


def _classify(src: str, resource_type: str | None = None) -> TrackerRecord:
    return classify_tracker(TrackerRecord(
        local_id="tracker-0", script_src=src, resource_type=resource_type,
    ))


# ── the deterministic answers the LLM is not allowed to move ────────────────────

def test_a_rendering_asset_is_functional_not_analytics():
    """The named example in §8/§9. Google Fonts is a rendering dependency; classifying
    it as analytics would inflate the pre-consent violation count with something that
    carries no behavioural tracking signal."""
    fonts = _classify("https://fonts.googleapis.com/css2?family=Inter")
    assert fonts.vendor == "Google Fonts"
    assert fonts.category == "functional"


def test_genuine_analytics_and_marketing_are_still_caught():
    """The distinction has to cut both ways -- being conservative about Google Fonts is
    only correct if the real trackers are still detected."""
    ga = _classify("https://www.google-analytics.com/analytics.js")
    assert (ga.vendor, ga.category) == ("Google Analytics", "analytics")

    ads = _classify("https://googleads.g.doubleclick.net/pagead/viewthroughconversion/123/")
    assert ads.category == "marketing"

    clicky = _classify("https://static.getclicky.com/js")
    assert (clicky.vendor, clicky.category) == ("Clicky Analytics", "analytics")


def test_an_unknown_third_party_is_left_unclassified_rather_than_guessed():
    """§8: do not classify every third-party request as a privacy tracker. Unknown means
    unknown -- R-008 reports it for a human, it is not defaulted into a category."""
    unknown = _classify("https://some-unknown-vendor.example/widget.js")
    assert unknown.vendor is None
    assert unknown.category is None


# ── the LLM has no path to overwrite any of that ────────────────────────────────

def test_nothing_in_the_llm_response_schema_can_carry_a_tracker_category():
    """The model returns findings (prose + risk + citations). It does not return
    trackers, so there is no field through which a reclassification could arrive."""
    from app.llm.schemas import ConsentFindingLLM

    fields = set(ConsentFindingLLM.model_fields)
    assert "trackers" not in fields
    assert "cookies" not in fields
    # `category` on a FINDING is the finding's own subject area, not a tracker's
    # classification -- it is never read back onto a Tracker row.
    assert fields == {
        "finding", "category", "risk_level", "priority", "evidence",
        "dpdp_reference", "recommendation", "requires_human_review",
    }


def test_the_findings_writer_never_touches_tracker_classification():
    """create_findings is the only node that persists LLM output. If it ever grew a
    write to a Tracker/Cookie category, the grounding guarantee would be gone and this
    is the test that should stop it."""
    from app.agents.consent_agent.nodes import create_findings as cf

    source = inspect.getsource(cf)
    for forbidden in ("tracker_repository", "Tracker(", ".category =", "update_tracker"):
        assert forbidden not in source, (
            f"create_findings now references {forbidden!r} -- LLM output must never "
            "write a tracker/cookie classification"
        )


def test_llm_interpretation_remains_an_unused_source_value():
    """schemas.py documents "llm_interpretation" as a possible `source`. Nothing sets
    it, and that is the point: every classification currently on a record came from a
    deterministic rule or a lookup table. If a code path starts setting it, the
    grounding story changes and this test should be revisited deliberately."""
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    setters = []
    for path in app_dir.rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Strip any trailing comment first: schemas.py documents the value in an
            # inline comment on the `source` field, which is the declaration this test
            # exists to protect, not an assignment of it.
            code = line.split("#", 1)[0]
            if "llm_interpretation" in code:
                setters.append(f"{path.relative_to(app_dir)}:{i}")
    assert not setters, f"llm_interpretation is now assigned somewhere: {setters}"


def test_the_model_is_shown_the_deterministic_category_it_must_respect():
    """Grounding only works if the classification actually reaches the prompt -- the
    model cannot respect a verdict it was never shown."""
    from app.llm.prompts import compact_scan_evidence

    compacted = compact_scan_evidence({
        "trackers": [{"id": "db-uuid", "script_src": "https://fonts.googleapis.com/css2",
                      "vendor": "Google Fonts", "category": "functional",
                      "consent_states": ["pre_consent"]}],
    })
    rendered = str(compacted)
    assert "Google Fonts" in rendered
    assert "functional" in rendered
