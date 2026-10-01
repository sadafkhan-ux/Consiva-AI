"""Post-reject validation: a finding that asserts a visitor rejected consent may only
exist when the scanner can show a rejection actually took effect.

The original defect was reporting "tracking continued after the visitor clicked Reject"
on scans where Reject was never clicked at all. That was gated on
reject_interaction == "clicked". This file covers the remaining half of the same
defect: "clicked" only means Playwright dispatched a click that did not raise, which a
"Decline" link in an unrelated modal, or a CMP opening a preferences panel instead of
recording a choice, both satisfy. Neither is a rejection, and R-003's wording asserts
one happened.

So the finding now requires BOTH the click and an independent observation that it took
effect, and that requirement is enforced in all three places the claim could originate:
the deterministic rule, the prompt, and the validation backstop on what the LLM returns.
"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.agents.consent_agent.nodes.validate_output import (
    _claims_tracking_after_reject,
    _reject_was_confirmed,
)
from app.rules.consent_rules import evaluate_consent_rules


def _evidence(reject_interaction, *, reject_confirmed, mechanism_type="cmp"):
    signal = {
        "mechanism_type": mechanism_type, "cmp_vendor": "OneTrust",
        "has_reject_all": True, "has_granular_choices": True,
        "evidence": {
            "accept_interaction": "clicked", "accept_click_confirmed": True,
            "reject_interaction": reject_interaction,
            "reject_click_confirmed": reject_confirmed,
        },
    }
    return {
        "cookies": [{"id": "c1", "name": "_fbp", "domain": "example.com",
                     "category": "marketing", "consent_states": ["post_reject"]}],
        "trackers": [], "forms": [],
        "policies": [
            {"id": "p1", "url": "https://example.com/privacy", "policy_type": "privacy_policy"},
            {"id": "p2", "url": "https://example.com/cookies", "policy_type": "cookie_policy"},
        ],
        "consent_signals": [signal],
    }


def _rule_ids(evidence) -> set[str]:
    return {f.rule_id for f in evaluate_consent_rules(evidence)}


# ── 9. the one case R-003's wording is actually true of ─────────────────────────

def test_a_confirmed_rejection_with_tracking_after_it_does_produce_the_finding():
    ids = _rule_ids(_evidence("clicked", reject_confirmed=True))
    assert "R-003" in ids
    assert "R-011" not in ids  # the "could not be tested" rule must not also fire


# ── 10/11. every other outcome must stay silent ─────────────────────────────────

@pytest.mark.parametrize(
    "outcome", ["click_failed", "cmp_not_automatable", "cmp_not_found", "page_unreachable"]
)
def test_r003_is_silent_when_reject_could_not_be_operated(outcome):
    """§6: if the reject action cannot be automated, no post-reject finding at all."""
    assert "R-003" not in _rule_ids(_evidence(outcome, reject_confirmed=False))


def test_r003_is_silent_when_the_click_was_dispatched_but_never_took_effect():
    """The case this change adds. A click happened, so R-009 ("could not be automated")
    is not true either -- but nothing observable changed, so the post_reject evidence
    was gathered in a state that was never established."""
    ids = _rule_ids(_evidence("clicked", reject_confirmed=False))
    assert "R-003" not in ids


def test_that_case_is_reported_rather_than_silently_dropped():
    """§6 again: report "Reject state could not be reliably tested" -- not nothing, and
    not a violation."""
    findings = evaluate_consent_rules(_evidence("clicked", reject_confirmed=False))
    r011 = next(f for f in findings if f.rule_id == "R-011")
    assert "could not be reliably tested" in r011.summary
    assert r011.confidence == "low"        # an automation limit, not confirmed non-compliance
    assert r011.risk_level != "high"       # never presented as a confirmed violation


@pytest.mark.parametrize("missing", [{}, {"reject_interaction": "clicked"}])
def test_a_missing_confirmation_flag_is_treated_as_unconfirmed(missing):
    """Scans and fixtures predating the flag must read as "not confirmed" -- never as
    "assume the click worked"."""
    evidence = _evidence("clicked", reject_confirmed=True)
    evidence["consent_signals"][0]["evidence"] = missing
    assert "R-003" not in _rule_ids(evidence)


# ── the LLM may not broaden an unverified reject state ──────────────────────────

def test_the_confirmation_reader_requires_both_halves():
    confirmed = {"consent_signals": [{"evidence": {
        "reject_interaction": "clicked", "reject_click_confirmed": True}}]}
    assert _reject_was_confirmed(confirmed) is True

    for weaker in (
        {"reject_interaction": "clicked", "reject_click_confirmed": False},
        {"reject_interaction": "clicked"},
        {"reject_interaction": "click_failed", "reject_click_confirmed": True},
        {},
    ):
        assert _reject_was_confirmed({"consent_signals": [{"evidence": weaker}]}) is False
    assert _reject_was_confirmed({}) is False
    assert _reject_was_confirmed(None) is False


@pytest.mark.parametrize("text", [
    "Analytics cookies continued firing after the visitor clicked Reject.",
    "Google Analytics still fired despite the user declining consent.",
    "Marketing pixels remained active following rejection of consent.",
])
def test_the_detector_catches_a_post_reject_claim_in_prose(text):
    assert _claims_tracking_after_reject(text) is True


@pytest.mark.parametrize("text", [
    "The Reject control could not be automated on this banner.",
    "No 'reject all' option was found on the consent banner.",
    "Analytics fired before any consent interaction took place.",
    "The reject state could not be reliably tested.",
])
def test_the_detector_leaves_honest_descriptions_of_the_limitation_alone(text):
    """A finding that correctly DESCRIBES the automation limitation must not be caught
    by the guard against asserting a violation."""
    assert _claims_tracking_after_reject(text) is False


@asynccontextmanager
async def _fake_stage(*args, **kwargs):
    yield {}


async def _run_validate(monkeypatch, *, scan_evidence, findings):
    from app.agents.consent_agent.nodes import validate_output as vo

    monkeypatch.setattr(vo, "track_stage", _fake_stage)
    captured: dict = {}

    class _Response:
        def __init__(self, items):
            self.findings = items

        def model_dump(self):
            return {"findings": self.findings}

    monkeypatch.setattr(
        vo.ConsentAnalysisResponse, "model_validate",
        classmethod(lambda cls, _payload: _Response(findings)),
    )
    state = vo.AgentState(
        scan_id=str(uuid.uuid4()), org_id=str(uuid.uuid4()), agent_run_id=str(uuid.uuid4()),
        scan_evidence=scan_evidence, rag_chunks=[{"chunk_id": "c1", "content": "text"}],
        llm_output={"findings": []},
    )
    result = await vo.validate_output(state)
    captured["result"] = result
    return result


def _llm_finding(text, *, risk="medium"):
    return SimpleNamespace(
        risk_level=risk, requires_human_review=False, dpdp_reference=["c1"],
        finding=text, recommendation="Stop it.",
    )


async def test_an_unconfirmed_reject_forces_a_post_reject_claim_to_human_review(monkeypatch):
    """The model is handed the raw post_reject evidence regardless, and "it was told not
    to" in the prompt has never been a control. This is the enforcement."""
    claim = _llm_finding("Analytics continued firing after the visitor clicked Reject.")
    benign = _llm_finding("No cookie policy was published.")

    await _run_validate(
        monkeypatch,
        scan_evidence={"consent_signals": [{"evidence": {
            "reject_interaction": "clicked", "reject_click_confirmed": False}}]},
        findings=[claim, benign],
    )

    assert claim.requires_human_review is True
    assert benign.requires_human_review is False, "only the post-reject claim is escalated"


async def test_a_confirmed_reject_leaves_the_same_claim_alone(monkeypatch):
    """The guard must not fire when the rejection genuinely was confirmed -- otherwise
    it would bury every legitimate R-003-shaped finding in review."""
    claim = _llm_finding("Analytics continued firing after the visitor clicked Reject.")

    await _run_validate(
        monkeypatch,
        scan_evidence={"consent_signals": [{"evidence": {
            "reject_interaction": "clicked", "reject_click_confirmed": True}}]},
        findings=[claim],
    )

    assert claim.requires_human_review is False


async def test_a_low_risk_post_reject_claim_is_caught_too(monkeypatch):
    """Not redundant with the high-risk backstop: a model that files this claim as low
    risk would otherwise sail straight past that one."""
    claim = _llm_finding(
        "Trackers still fired after the user declined consent.", risk="low"
    )

    await _run_validate(
        monkeypatch,
        scan_evidence={"consent_signals": [{"evidence": {"reject_interaction": "cmp_not_automatable"}}]},
        findings=[claim],
    )

    assert claim.requires_human_review is True


# ── the scanner-side half: what "confirmed" actually measures ───────────────────


class _FakeLocator:
    def __init__(self, count: int, visible: bool, text: str = "", raises: bool = False):
        self._count, self._visible, self._text, self._raises = count, visible, text, raises

    async def count(self):
        if self._raises:
            from playwright.async_api import Error as PlaywrightError
            raise PlaywrightError("frame detached")
        return self._count

    @property
    def first(self):
        return self

    def nth(self, _i):
        return self

    async def is_visible(self, timeout=None):
        return self._visible

    async def inner_text(self, timeout=None):
        return self._text


class _FakeBannerFrame:
    """One frame whose consent control is either still on screen or gone."""

    def __init__(self, *, visible: bool, raises: bool = False):
        self.url = "https://example.com/"
        self._visible, self._raises = visible, raises

    def locator(self, selector):
        if selector.startswith("#"):          # a catalogued CMP selector
            return _FakeLocator(1 if self._visible else 0, self._visible, raises=self._raises)
        return _FakeLocator(1, self._visible, text="Reject all", raises=self._raises)


class _FakeBannerPage:
    def __init__(self, *, visible: bool, raises: bool = False):
        self.frames = [_FakeBannerFrame(visible=visible, raises=raises)]


async def test_a_dismissed_banner_confirms_the_interaction():
    from app.scanner.consent_interactor import confirm_interaction_completed

    assert await confirm_interaction_completed(_FakeBannerPage(visible=False), accept=False) is True


async def test_a_banner_still_on_screen_does_not_confirm_it():
    """The defect this closes: the click returned "clicked" but nothing happened."""
    from app.scanner.consent_interactor import confirm_interaction_completed

    assert await confirm_interaction_completed(_FakeBannerPage(visible=True), accept=False) is False


async def test_an_unobservable_page_is_never_reported_as_confirmed():
    """Conservative in the safe direction: doubt suppresses the finding, never asserts
    it."""
    from app.scanner.consent_interactor import confirm_interaction_completed

    page = _FakeBannerPage(visible=False, raises=True)
    assert await confirm_interaction_completed(page, accept=False) is False


def test_the_crawler_records_confirmation_next_to_the_interaction_status():
    """The wiring: both keys must reach consent_signals.evidence, which is what
    consent_rules and the prompt both read."""
    import inspect

    from app.scanner import crawler

    source = inspect.getsource(crawler)
    assert '"reject_click_confirmed": reject_confirmed' in source
    assert '"accept_click_confirmed": accept_confirmed' in source
    assert "confirm_interaction_completed" in source
