"""Regression tests for the token-usage optimisations: what each change saves, and what it must not change.

The measurements behind them (23 stored scans, 40 model calls, 239,783 input / 74,422 output tokens): 9 scans needed a validation
retry, every one of them re-sending the whole prompt; the repeated failure was the vendor-timing check rejecting findings that
only DENY a timing; and about a fifth of the knowledge base is unreadable legacy-encoded Hindi that was still being retrieved and
sent to the model."""

import json
from types import SimpleNamespace

import pytest

from app.agents.consent_agent.nodes.validate_output import _unsupported_vendor_claims
from app.config import Settings
from app.llm.prompts import _lean_schema, build_analysis_prompt
from app.llm.schemas import ConsentAnalysisResponse
from app.rag import retriever
from app.rag.chunker import chunk_pages
from app.rag.text_quality import is_readable_text


def finding(text: str, recommendation: str = ""):
    return SimpleNamespace(finding=text, recommendation=recommendation)


# ---------------------------------------------------------------------------------------------------------------------------
# 1. The vendor-timing check rejects a false ASSERTION, not a sentence that denies the timing.
# ---------------------------------------------------------------------------------------------------------------------------

DENIALS = [
    "No analytics or marketing tracker fired before consent or after Reject. Google Analytics, Google Tag Manager and Google Ads loaded only after Accept.",
    "Google Analytics loaded only after the visitor clicked Accept; nothing continued after Reject.",
    "The scan did not observe Google Ads before consent.",
    "Neither Google Analytics nor Google Ads was seen before consent.",
    "Google Tag Manager was never observed after the visitor rejected consent.",
]

ASSERTIONS = [
    "Google Ads fired before consent.",
    "Google Analytics and Google Tag Manager continued firing after Reject.",
    "Google Analytics did not stop firing after the visitor clicked Reject.",
    "Google Ads failed to stop after Reject.",
    "Google Ads was not blocked, but it fired before consent.",
    "Nothing fired before consent except Google Analytics.",
    "No banner was shown. Google Ads fired before consent.",
]


@pytest.mark.parametrize("text", DENIALS)
def test_a_finding_that_denies_a_timing_is_not_flagged(text):
    assert _unsupported_vendor_claims(finding(text), [], []) == []


@pytest.mark.parametrize("text", ASSERTIONS)
def test_a_finding_that_asserts_a_timing_for_an_unlisted_vendor_is_still_flagged(text):
    assert _unsupported_vendor_claims(finding(text), [], []), text


def test_a_negation_in_an_earlier_clause_does_not_hide_a_new_assertion():
    text = "No consent banner was shown, and Google Analytics fired before consent."
    assert _unsupported_vendor_claims(finding(text), [], [])
    assert _unsupported_vendor_claims(finding(text), ["Google Analytics"], []) == []


def test_an_asserted_timing_for_a_vendor_that_is_in_the_list_is_accepted():
    assert _unsupported_vendor_claims(finding("Google Ads fired before consent."), ["Google Ads"], []) == []
    assert _unsupported_vendor_claims(finding("Google Ads continued firing after Reject."), [], ["Google Ads"]) == []


def test_the_recommendation_text_is_checked_too():
    flagged = _unsupported_vendor_claims(finding("Tracking was found.", "Stop Google Ads firing before consent."), [], [])
    assert flagged and "before consent" in flagged[0]


# ---------------------------------------------------------------------------------------------------------------------------
# 2. The schema in the prompt is leaner; the schema the provider enforces is untouched.
# ---------------------------------------------------------------------------------------------------------------------------

def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_the_prompt_schema_drops_titles_and_model_docstrings_but_keeps_every_instruction():
    full = ConsentAnalysisResponse.model_json_schema()
    lean = _lean_schema(full)

    assert any("title" in n for n in _walk(full))                      # pydantic does add them
    assert not any(isinstance(n.get("title"), str) for n in _walk(lean))
    assert len(json.dumps(lean, separators=(",", ":"))) <= len(json.dumps(full, separators=(",", ":"))) - 200

    # everything the model needs survives: field names, required lists, enums and the field-level descriptions
    assert lean["$defs"]["ConsentFindingLLM"]["required"] == full["$defs"]["ConsentFindingLLM"]["required"]
    assert lean["$defs"]["ConsentFindingLLM"]["properties"]["risk_level"]["enum"] == ["low", "medium", "high"]
    assert "local_id" in lean["$defs"]["ConsentFindingLLM"]["properties"]["evidence"]["description"]
    assert "chunk" in lean["$defs"]["ConsentFindingLLM"]["properties"]["dpdp_reference"]["description"]
    assert "description" not in lean["$defs"]["ConsentFindingLLM"]            # the docstring: an implementation note

    # the input is not modified
    assert any(isinstance(n.get("title"), str) for n in _walk(full))


def test_the_prompt_uses_the_lean_schema_and_the_shorter_vendor_note():
    prompt = build_analysis_prompt(scan_summary={"trackers": [], "cookies": []}, rule_findings=[], rag_chunks=[])
    assert '"title"' not in prompt
    assert "Risk Level" not in prompt
    assert "What the LLM itself must return." not in prompt
    assert "Saying that nothing fired in a state is fine." in prompt
    assert "## Vendors confirmed by state" in prompt                          # the ground-truth lists are still there


def test_provider_schema_still_carries_its_own_structure():
    from app.llm.client import _strict_json_schema
    strict = _strict_json_schema(ConsentAnalysisResponse.model_json_schema())
    assert "findings" in strict["properties"]


# ---------------------------------------------------------------------------------------------------------------------------
# 3. Unreadable (legacy-encoded Hindi) knowledge text is neither indexed nor sent.
# ---------------------------------------------------------------------------------------------------------------------------

HINDI_GARBLED = "ती”से ऐसा कोई मध्यवती अजभप्रेत है िो अपिे क ं प्यूटर संसािि के उपयोर्कतागओं को एक या अडिक ऑिलाइि र्ेम का अजभगम करिे में सक्षम बिाता है"
ENGLISH = "A Data Fiduciary shall give each Data Principal a notice, in clear and plain language, itemising the personal data and the purpose."


def test_readable_text_tells_english_from_garbled_hindi():
    assert is_readable_text(ENGLISH)
    assert is_readable_text("Section 6 — consent (the “Act”) café")           # punctuation and accents are fine
    assert not is_readable_text(HINDI_GARBLED)
    assert not is_readable_text("")
    assert not is_readable_text("1234 5678 ...")


def test_chunking_skips_an_unreadable_page_and_keeps_the_english_one():
    chunks = chunk_pages([{"page_number": 1, "text": ENGLISH}, {"page_number": 2, "text": HINDI_GARBLED}])
    assert [c.metadata["page_number"] for c in chunks] == [1]
    assert chunks[0].chunk_index == 0


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self.rows, self.statement = rows, None

    async def execute(self, stmt):
        self.statement = stmt
        return _Rows(self.rows)


def _row(i, content, dist):
    chunk = SimpleNamespace(id=f"c{i}", document_id="d1", content=content, chunk_metadata={})
    return chunk, "DPDP Rules", None, dist


async def test_search_drops_unreadable_chunks_and_still_returns_top_k_in_rank_order():
    rows = [_row(1, HINDI_GARBLED, 0.70), _row(2, ENGLISH, 0.72), _row(3, HINDI_GARBLED, 0.73), _row(4, ENGLISH + " Two.", 0.75),
            _row(5, ENGLISH + " Three.", 0.78), _row(6, ENGLISH + " Four.", 0.80)]
    db = _FakeDb(rows)

    found = await retriever._search([0.0] * 4, db=db, top_k=3, max_distance=0.95)

    assert [c.chunk_id for c in found] == ["c2", "c4", "c5"]                      # unreadable skipped, next-best took their place
    assert db.statement._limit == 3 + retriever._UNREADABLE_HEADROOM               # fetched extra so the query is not left short


async def test_search_still_applies_the_distance_ceiling():
    db = _FakeDb([_row(1, ENGLISH, 0.60), _row(2, ENGLISH + " Two.", 0.99)])
    found = await retriever._search([0.0] * 4, db=db, top_k=3, max_distance=0.95)
    assert [c.chunk_id for c in found] == ["c1"]


# ---------------------------------------------------------------------------------------------------------------------------
# 4. The reasoning-effort knob exists and defaults to the behaviour the project has always run with.
# ---------------------------------------------------------------------------------------------------------------------------

def test_anthropic_effort_defaults_to_the_existing_behaviour():
    assert Settings.model_fields["anthropic_effort"].default == "medium"


# ---------------------------------------------------------------------------------------------------------------------------
# 5. The vendor lists hold only what consent is about, so the model cannot be told a font or the consent tool "fired before consent".
# ---------------------------------------------------------------------------------------------------------------------------

from app.llm.prompts import compact_scan_evidence, pre_post_reject_vendor_lists  # noqa: E402


def _tracker(src, vendor=None, category=None, states=("pre_consent",)):
    return {"script_src": src, "vendor": vendor, "category": category, "consent_states": list(states)}


def test_functional_items_are_not_listed_as_vendors_that_fired_before_consent_or_after_reject():
    evidence = {"trackers": [
        _tracker("https://api.consiva.ai/sdk.js", "Consiva Consent SDK", "functional", ("pre_consent", "post_reject")),
        _tracker("https://fonts.googleapis.com/css2", "Google Fonts", "functional", ("pre_consent", "post_reject")),
        _tracker("https://cdn.example.net/app.js", "Static CDN Asset", "functional", ("pre_consent",)),
        _tracker("https://www.googletagmanager.com/gtm.js", "Google Tag Manager", "analytics", ("pre_consent",)),
        _tracker("https://ads.example/pixel", "Some Ads", "marketing", ("post_reject",)),
        _tracker("https://unknown.example/x.js", None, None, ("pre_consent",)),
    ], "cookies": []}

    pre, rej = pre_post_reject_vendor_lists(compact_scan_evidence(evidence))

    assert pre == ["Google Tag Manager", "unknown.example"]       # analytics, and what could not be classified
    assert rej == ["Some Ads"]
    assert not {"Consiva Consent SDK", "Google Fonts", "Static CDN Asset"} & set(pre + rej)


def test_a_finding_that_calls_a_functional_item_tracking_before_consent_is_now_caught():
    evidence = {"trackers": [_tracker("https://fonts.googleapis.com/css2", "Google Fonts", "functional", ("pre_consent", "post_reject"))], "cookies": []}
    pre, rej = pre_post_reject_vendor_lists(compact_scan_evidence(evidence))
    claim = finding("Google Fonts was observed firing before consent.")
    assert _unsupported_vendor_claims(claim, pre, rej) == ["Google Fonts (named as firing before consent)"]
    assert _unsupported_vendor_claims(finding("Google Fonts continued firing after Reject."), pre, rej)


def test_the_prompt_tells_the_model_what_is_not_an_issue():
    prompt = build_analysis_prompt(scan_summary={"trackers": [], "cookies": []}, rule_findings=[], rag_chunks=[])
    assert "cat=functional" in prompt and "not tracking" in prompt
    assert "Tracking seen only after the visitor accepted is expected and is not an issue" in prompt
    assert "Do not report an issue that neither the rule findings nor the vendor lists below support" in prompt
