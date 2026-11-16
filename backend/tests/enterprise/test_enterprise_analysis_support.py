"""Shared fakes for the Enterprise text-analysis tests: an in-memory repo, a scripted model, and a small realistic site. Contains no tests of its own."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from datetime import date

from app.enterprise.analysis import consolidate as co
from app.enterprise.analysis import sources as sr
from app.enterprise.analysis.extract import PageExtraction
from app.enterprise.analysis.llm import LLMResult
from app.enterprise.analysis.repo import ChunkRow, EvRow, FrontierRow, Key
from app.enterprise.scoring.engine import Assessment

SCAN = uuid.UUID("11111111-1111-1111-1111-111111111111")
ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
AS_OF = date(2026, 11, 13)

POLICY = """Privacy Policy

Information we collect

We collect your name, email address, phone number and IP address when you contact us.

How we use your information

We use your information to respond to your enquiries and to send you service updates.

Sharing

We share data with Google Analytics and Amazon Web Services.

Retention

We retain your personal data for as long as your account is active and delete it within 12 months afterwards.

Your rights

You may access, correct or erase your personal data by writing to privacy@acme.test or using the portal at https://acme.test/privacy/requests.
You may withdraw your consent at any time by using the settings page at https://acme.test/settings.
You may nominate another person to exercise your rights.

Grievances

Grievances can be sent to our Grievance Officer at grievance@acme.test and we will respond within 30 days.
You may complain to the Data Protection Board of India.

Security

We use encryption and access controls to protect your data.
In the event of a personal data breach we will notify affected users and the Board.

Contact

Our Data Protection Officer can be reached at dpo@acme.test.
"""

#: sentence pattern -> (topic, value, specific)
SENTENCE_FACTS = [
    (r"We collect your name", "data_category", "name, email address, phone number, IP address", True),
    (r"We use your information to respond", "purpose", "respond to enquiries", True),
    (r"We share data with Google Analytics", "sharing_recipient", "Google Analytics", True),
    (r"We retain your personal data", "retention", "while the account is active, then 12 months", True),
    (r"You may access, correct or erase", "right_access", "access", True),
    (r"You may access, correct or erase", "right_correction", "correction", True),
    (r"You may access, correct or erase", "right_erasure", "erasure", True),
    (r"You may access, correct or erase", "exercise_channel", "email and portal", True),
    (r"You may withdraw your consent", "withdraw_consent", "settings page", True),
    (r"You may nominate", "right_nominate", "nominate", True),
    (r"Grievances can be sent", "grievance_mechanism", "Grievance Officer by email", True),
    (r"Grievances can be sent", "grievance_period", "30 days", True),
    (r"You may complain to the Data Protection Board", "board_complaint", "Data Protection Board", True),
    (r"We use encryption", "security", "encryption", True),
    (r"personal data breach", "breach", "notify users and the Board", True),
    (r"Data Protection Officer can be reached", "dpo_contact", "DPO at dpo@acme.test", True),
]


class FakeLLM:
    """A scripted model. `extraction_facts` extends what it reads out of a passage; `invent` adds a fact whose quote is not in the text; `fail` makes chosen calls raise."""

    def __init__(self, *, verdicts: dict | None = None, invent: bool = False, fail_if=None, malformed_first: int = 0, page_kind: dict | None = None, extra_facts=None):
        self.calls: list[tuple[str, str]] = []
        self.verdicts = verdicts or {}
        self.invent, self.fail_if, self.malformed_first = invent, fail_if, malformed_first
        self.page_kind = page_kind or {}
        self.extra = extra_facts or []
        self.extraction_passages: list[str] = []

    async def __call__(self, system: str, user: str, schema):
        name = schema.__name__
        self.calls.append((name, user))
        if self.malformed_first > 0:
            self.malformed_first -= 1
            return "{not json"
        if self.fail_if and self.fail_if(name, system, user):
            raise RuntimeError("provider unavailable")
        if name == "PageExtraction":
            passage = user.split("<<<PASSAGE\n", 1)[1].rsplit("\nPASSAGE>>>", 1)[0]
            self.extraction_passages.append(passage)
            facts = []
            for line in re.split(r"(?<=[.])\s+|\n+", passage):
                for rx, topic, value, spec in SENTENCE_FACTS + self.extra:
                    if re.search(rx, line):
                        facts.append({"topic": topic, "value": value, "quote": line.strip(), "specific": spec})
            if self.invent:
                facts.append({"topic": "security", "value": "ISO 27001 certified", "quote": "We are ISO 27001 certified and audited yearly.", "specific": True})
            return LLMResult(PageExtraction.model_validate({"facts": facts}), "claude", "claude-test")
        if name == "Judgment":
            pid = re.search(r"Requirement \((\w+)\)", system).group(1)
            v = self.verdicts.get(pid, ("yes", "high"))
            ids = [int(x) for x in re.findall(r"^\[(\d+)\]", user, re.M)][:3]
            return LLMResult(schema.model_validate({"verdict": v[0], "confidence": v[1], "reason": "scripted", "fact_ids": ids}), "claude", "claude-test")
        if name == "FormBatch":
            n = len(re.findall(r"^Form \d+ on", user, re.M))
            return LLMResult(schema.model_validate({"items": [{"index": i, "necessary": "yes", "excess_fields": [], "reason": "ok", "confidence": "high"} for i in range(n)]}), "claude", "claude-test")
        if name == "Conflicts":
            return LLMResult(schema.model_validate({"items": []}), "claude", "claude-test")
        if name == "PageKind":
            url = re.search(r"URL: (\S+)", user).group(1)
            return LLMResult(schema.model_validate(self.page_kind.get(url, {"category": "other", "confidence": "high"})), "claude", "claude-test")
        raise AssertionError(name)


class FakeRepo:
    def __init__(self, evidence: list[EvRow], frontier: list[FrontierRow] | None = None):
        self.evidence, self.frontier = evidence, frontier or []
        self.chunks: dict[Key, ChunkRow] = {}
        self.scan_history: list[tuple[str, dict]] = []
        self.saved: list[tuple[str, list[Assessment], str | None, str | None]] = []
        self.frontier_status: dict[str, str] = {}

    async def set_scan_status(self, scan_id, status, *, started=False, completed=False, error=None):
        self.scan_history.append((status, {"started": started, "completed": completed, "error": error}))

    async def load_evidence(self, scan_id):
        return list(self.evidence)

    async def load_frontier(self, scan_id):
        return list(self.frontier)

    async def load_chunks(self, scan_id):
        return [replace(c) for c in self.chunks.values()]

    async def upsert_chunks(self, scan_id, rows):
        for r in rows:
            self.chunks[r.key] = replace(r)

    async def delete_chunks(self, scan_id, keys):
        for k in keys:
            self.chunks.pop(k, None)

    async def update_chunk(self, scan_id, key, **fields):
        c = self.chunks[key]
        for k, v in fields.items():
            setattr(c, k, v)

    async def set_frontier_status(self, scan_id, statuses):
        self.frontier_status.update(statuses)

    async def save_assessments(self, scan_id, assessments, *, assessor, provider, model):
        self.saved.append((assessor, list(assessments), provider, model))
        return len(assessments)

    def assessments(self) -> dict[str, Assessment]:
        return {a.parameter_id: a for _, items, _, _ in self.saved for a in items}


_n = [0]


def ev(kind: str, url: str, content, *, page: int = 0, method: str = "x", frontier: str | None = None) -> EvRow:
    _n[0] += 1
    body = content if isinstance(content, str) else json.dumps(content)
    return EvRow(f"e{_n[0]:04d}", kind, url, page, frontier, method, body)


def site(*, policy_text: str = POLICY, with_blog: bool = True, extra: list[EvRow] | None = None) -> tuple[list[EvRow], list[FrontierRow]]:
    rows = [
        ev("page_text", "https://acme.test/", "Welcome to Acme. We build tools for factories.", frontier="f1"),
        ev("page_html", "https://acme.test/", "<title>Acme</title><h1>Acme</h1>"),
        ev("page_text", "https://acme.test/privacy-policy", policy_text, frontier="f2"),
        ev("page_html", "https://acme.test/privacy-policy", "<title>Privacy Policy | Acme</title><h1>Privacy Policy</h1><h2>Information we collect</h2><h2>Your rights</h2>"),
        ev("page_text", "https://acme.test/contact", "Contact us. Write to hello@acme.test.", frontier="f3"),
        ev("form", "https://acme.test/contact", {"page_url": "https://acme.test/contact", "forms": [{
            "index": 0, "page_url": "https://acme.test/contact", "action": "https://acme.test/contact", "action_host": "acme.test", "method": "post", "fields": [
                {"type": "text", "name": "full_name", "label": "Name", "checked": False}, {"type": "email", "name": "email", "label": "Email", "checked": False},
                {"type": "textarea", "name": "message", "label": "Message", "checked": False},
                {"type": "checkbox", "name": "agree", "label": "I agree to the privacy policy", "checked": False}],
            "collects": ["email", "message", "name"], "consent_controls": [{"type": "checkbox", "name": "agree", "label": "I agree to the privacy policy", "checked": False}], "preticked": [],
            "privacy_link": True, "text": "Contact us", "label_text": "Name | Email | Message | I agree to the privacy policy"}]}, frontier="f3"),
        ev("network", "https://acme.test/", {"page_url": "https://acme.test/", "hosts": [{"host": "www.google-analytics.com", "third_party": True, "requests": 2}],
                                              "non_essential": [{"vendor": "Google Analytics", "category": "analytics", "host": "www.google-analytics.com"}]}),
        ev("cookie", "https://acme.test/", {"page_url": "https://acme.test/", "cookies": [{"identity": "_ga|acme.test|/", "name": "_ga", "domain": "acme.test", "first_party": True}]}),
        ev("header", "https://acme.test/privacy-policy", {"page_url": "https://acme.test/privacy-policy", "lang": "en", "alternates": []}, method="response_headers"),
    ]
    if with_blog:
        rows += [ev("page_text", "https://acme.test/blog/dpdp-act-2023-compliance-guide", "Posted on 3 May 2024 by Jane Doe. 7 min read. Your privacy policy must be ready for the DPDP Act. " * 40, frontier="f4"),
                 ev("page_html", "https://acme.test/blog/dpdp-act-2023-compliance-guide", "<title>DPDP Act 2023 compliance guide</title><h1>DPDP Act 2023 compliance guide</h1>")]
    rows += extra or []
    fr = [FrontierRow("f1", "https://acme.test/", "https://acme.test/", "done", "text/html"), FrontierRow("f2", "https://acme.test/privacy-policy", "https://acme.test/privacy-policy", "done", "text/html"),
          FrontierRow("f3", "https://acme.test/contact", "https://acme.test/contact", "done", "text/html"), FrontierRow("f4", "https://acme.test/blog/dpdp-act-2023-compliance-guide",
                                                                                                                          "https://acme.test/blog/dpdp-act-2023-compliance-guide", "done", "text/html"),
          FrontierRow("f5", "https://acme.test/logo.png", None, "excluded", "image/png")]
    return rows, fr


def ctx_from(rows: list[EvRow], facts: list[dict] | None = None, *, unread: dict | None = None, judgments: dict | None = None, unresolved: list[str] | None = None) -> co.Ctx:
    loaded = sr.build(rows, [])
    fs = []
    for i, f in enumerate(facts or []):
        f = {"specific": True, "page_number": 0, "kind": "page_text", **f}
        if "evidence_id" not in f or "url" not in f:
            pol = next(s for s in loaded.texts if s.category == "privacy_policy")
            f.setdefault("evidence_id", pol.evidence_id)
            f.setdefault("url", pol.url)
        f.setdefault("category", "privacy_policy")
        fs.append({**f, "id": i})
    return co.Ctx(loaded, fs, unread or {}, AS_OF, judgments or {}, unresolved or [])
