"""Levels 2 to 4: group the page facts by kind of page, compare pages with each other and with what the browser observed, and map the result onto the text parameters.

What decides what. The model reads and quotes (level 1), and interprets a few questions that are about meaning (below). Everything that can be a rule is a rule: a count, a set
comparison, a number of days, a language. Legal references come from the catalogue only. A model's "low" confidence is passed on as it is: the scoring engine sends it to a person.

THE RULE FOR WHAT WAS NOT READ. A reading that finds something stands even if other passages failed to be analysed (the quote proves it). A reading that finds NOTHING, or only part, can be
overturned by a passage that was not read; so when any passage of the pages it relies on is failed or pending, "not met" and "partly met" become NOT ASSESSED, with the reason. A model, API
or parsing failure is never a finding against the site. "Read in full" below means: a privacy policy was found and every chunk of every page of the kinds listed was analysed.

Pages searched for absence are those the crawl reached; a policy that was never crawled is not seen (the coverage statement says what the crawl reached). Absence conclusions have confidence
"medium" for that reason. A positive reading backed by a verified quote has "high" unless the model itself was unsure.

Decision rules (met / partly met / not met / not assessed / not applicable):

  N1  Notice exists.   met: a page or document classified as a privacy policy was crawled. not met: the crawl read pages, none is a policy (a blog article about privacy is not one), and no
                       ambiguous page is left unresolved. not assessed: no text was read, or an ambiguous page that may be the policy could not be settled.
  N2  Itemised data.   Facts "data_category" in the privacy policy. met: three or more distinct specific items and the model agrees the notice itemises them. partly met: one or two specific
                       items, or the model sees only part of an itemisation. not met: read in full and only general categories ("personal information") or none. low confidence if the model dissents.
  N3  Specified purposes.  Facts "purpose". met: two or more distinct specific purposes and the model agrees they are specific. partly met: one, or generic ones next to a specific one.
                       not met: read in full and only generic purposes ("to improve our services") or none.
  N4  Plain language.  Model reading of readability figures over the whole policy plus eight sampled passages (stated in the note). Confidence never above medium (the catalogue marks it for
                       human review). met: clear and plain. not met: dense legalese. A middle verdict goes to a person. not assessed: policy not in English (the reading is English only).
  N5  Withdraw consent and rights, with a link.  Three components: a statement on withdrawing consent, a stated way to exercise rights, and a link, portal or form for it (a bare email address
                       is not a link). met: all three. partly met: one or two. not met: none, read in full.
  N6  Complain to the Board.  met: a verified quote names the Data Protection Board (word "Board"). not met: read in full and none.
  N7  Language.        Deterministic. met: the policy text is English, or in an Eighth Schedule language's script, or the policy page declares an English or Eighth Schedule alternate (hreflang or a
                       stated language option). not met: the policy is in neither and no option is declared. not assessed: no policy.
  N8  Notice at the request.  Forms that collect personal data (email, phone, name, address, date of birth, company, message; login forms excluded). met: every one has a link to a privacy
                       notice (in the form, or "privacy" in the wording around it). not met: at least one has none (named). not assessed: no such form was recorded.
  C1  Consent request language.  Consent requests are forms with a consent control or consent wording. Two components: plain language (model reading of the wording) and an English or Eighth
                       Schedule language. met: both. partly met: one. not met: neither. not assessed: no consent request was found, or the model could not read the wording.
  C2  Contact in the consent request.  met: every consent request names a DPO, grievance or privacy officer or contact AND gives an email, phone or link. not met: otherwise (named).
                       not assessed: no consent request recorded. Confidence medium: only the DOM around a form is seen, not a banner's text.
  C3  Affirmative action, nothing pre-ticked, per purpose.  Uses the recorded `checked` state of form checkboxes and radios. not met: any consent-type control is pre-ticked. met: at least one
                       unticked consent control, none pre-ticked, no control bundling several purposes, and no data-collecting form that only says "by submitting you agree". partly met: unticked
                       controls exist but a form bundles purposes or relies on "by submitting". not assessed: no consent control found, or the evidence predates the `checked` field
                       (a contact form with no checkbox may rest on another lawful ground, so its absence is not a finding).
  C9  Only necessary data.  Model reading of each distinct form (what it is for, what it asks); excess fields must be real fields of that form. met: no form asks for excess data. partly met: fewer
                       than half of the forms assessed do. not met: half or more do. not assessed: no form could be read. Confidence never above medium.
  G1  Contact of a DPO or responsible person.  met: a verified quote gives a role (DPO, grievance or privacy officer, privacy contact) and an email, phone or web address, on the policy, a
                       grievance page, a contact page or an about page. not met: read in full and none.
  G2  Grievance and response period.  Periods are read as numbers of days (hours /24, weeks 7, months 30). met: a grievance means is published and the period is 90 days or fewer (90 passes).
                       partly met: a means but no stated period (or a vague one: "reasonable time"). not met: a stated period over 90 days, or read in full with no means. The longest stated period counts.
  G3  Means to access, correct, erase.  met: all three rights and a way to exercise them are stated. partly met: some. not met: none, read in full.
  G4  Right to nominate.  met: a verified quote about nominating another person. not met: read in full and none.
  G5  Retention/erasure described (statement only).  met: a retention or deletion statement exists. not met: read in full, none. Earns no points.
  G6  Third parties named.  met: at least one specific recipient or processor is named in the policy. not met: only generic recipients or none while the site contacted third parties. not applicable:
                       no third-party host was observed and none is named.
  G7  Security statement (statement only).  met: a verified statement of safeguards. not met: read in full, none.
  G8  Breach process (statement only).  met: a verified statement on what happens in a breach. not met: read in full, none.
  K1  Children.  Not applicable unless the evidence shows the site serves children: a statement that it does, a consent-by-guardian statement, a guardian field in a form, or a page path or
                       title with kids/children/child/junior/teen/preschool (blog articles do not count). Only weaker signs (students, schools, parents) go to a person (human review). If applicable:
                       met: guardian consent is described and no tracking or targeted advertising at children is stated. partly met: one of the two. not met: neither, read in full.
                       A site that says it is not for children, with no other sign, is not applicable. Confidence never above medium.
  M1  Consent Manager.  Not applicable unless a verified quote mentions a Consent Manager integration; then human review (registration cannot be seen from a website).
  S1, T1  Need records: a public website cannot show Significant Data Fiduciary status or a cross-border restriction. E1  Human review: an exemption is a decision, not an observation.
  X1  Cookies observed against declared.  Every distinct cookie seen on the crawl is looked for in the privacy and cookie policy text by name (or stable prefix) or, for a third-party cookie,
                       by the service its domain belongs to. met: all declared. partly met: half or more. not met: fewer than half. not applicable: no cookie observed. not assessed: no policy.
  X2  Third parties contacted against named.  The analytics and advertising services the browser contacted before any choice (vendors recorded in the network evidence) are looked for, by name
                       or alias, in the privacy and cookie policy text. met: all named, or none observed. partly met: undisclosed, but half or more are named. not met: fewer than half named
                       (one undisclosed tracker out of one is not met; the host is named). not assessed: no policy.
  X3  Form fields against itemised data.  The kinds of data the forms collect are looked for in the privacy policy text by their usual words. met: all itemised. partly met: half or more.
                       not met: fewer than half. not applicable: no form collects personal data. not assessed: no policy.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.enterprise.analysis import pages as pg
from app.enterprise.analysis import textrules as tr
from app.enterprise.analysis.constants import FORMS_PER_CALL, MAX_REFS, N4_SAMPLE_CHARS, N4_SAMPLES, OUTDATED_AFTER_DAYS
from app.enterprise.analysis.extract import TOPICS, normalise
from app.enterprise.analysis.llm import Answer, LLMCallable, LLMFailure, call_structured
from app.enterprise.analysis.sources import Loaded, Src
from app.enterprise.scoring.catalogue import BY_ID
from app.enterprise.scoring.engine import (
    HUMAN_REVIEW, MET, NEEDS_RECORDS, NOT_APPLICABLE, NOT_ASSESSED, NOT_MET, PARTLY, Assessment,
)

PRIVACY, COOKIE, GRIEVANCE, CONTACT, ABOUT, TERMS = pg.PRIVACY, pg.COOKIE, pg.GRIEVANCE, pg.CONTACT, pg.ABOUT, pg.TERMS
#: Parameters whose state rests on the model's extraction or interpretation (saved with assessor "llm"). The rest are decided by code from stored evidence ("rule:text").
LLM_PARAMS = frozenset({"N2", "N3", "N4", "N5", "N6", "C1", "C9", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "K1", "M1"})
RANK = {"low": 0, "medium": 1, "high": 2}
Runner = Callable[[str, int, int, Callable[[], Awaitable[tuple[dict, "Answer | None"]]]], Awaitable["dict | None"]]


# ---- the context every rule reads -------------------------------------------------------------------------------------------------------------------------------------

@dataclass
class Ctx:
    loaded: Loaded
    facts: list[dict]                         # kept facts, each with id, evidence_id, url, page_number, category
    unread: dict[str, int]                    # kind of page -> chunks not analysed (failed or pending); "forms" for form sources
    as_of: date
    judgments: dict[str, dict] = field(default_factory=dict)       # parameter id -> {"verdict", "confidence", "reason", "fact_ids"} or {"failed": reason}
    unresolved: list[str] = field(default_factory=list)

    def src(self, *cats: str) -> list[Src]:
        return self.loaded.of(*cats)

    def text(self, *cats: str) -> str:
        return "\n".join(s.text for s in self.src(*cats)).lower()

    def refs(self, *cats: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f"ev:{s.evidence_id}" for s in self.src(*cats)))[:MAX_REFS]

    def of(self, topic: str, *cats: str, pattern: str | None = None) -> list[dict]:
        rx = re.compile(pattern, re.I) if pattern else None
        return [f for f in self.facts if f["topic"] == topic and (not cats or f["category"] in cats) and (rx is None or rx.search(f["quote"] + " " + f["value"]))]

    def read_in_full(self, *cats: str) -> bool:
        """A privacy policy was found and every analysed chunk of the privacy policy and of the other kinds listed is done."""
        return bool(self.src(PRIVACY)) and all(self.unread.get(c, 0) == 0 for c in {PRIVACY, *cats})

    def why_not(self, *cats: str) -> str:
        if not self.src(PRIVACY):
            return "no privacy policy was found, so there was nothing to read"
        n = sum(self.unread.get(c, 0) for c in {PRIVACY, *cats})
        return f"{n} passage(s) of the {', '.join(sorted({PRIVACY, *cats}))} pages could not be analysed (model or API failure), so an absence cannot be concluded"


def _refs_of(facts: list[dict]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(f"ev:{f['evidence_id']}" for f in facts))[:MAX_REFS]


_refs = _refs_of


def _cite(f: dict) -> str:
    return f"{f['url']}{' p.' + str(f['page_number']) if f.get('page_number') else ''}: \"{f['quote'][:160]}\""


def _cap(conf: str | None, top: str) -> str:
    return conf if conf in RANK and RANK[conf] <= RANK[top] else top


def _settle(ctx: Ctx, pid: str, state: str, refs: tuple[str, ...], conf: str | None, note: str, *cats: str) -> Assessment:
    """Apply the rule for what was not read: only a finding of something survives a failed passage."""
    if state in (NOT_MET, PARTLY) and not ctx.read_in_full(*cats):
        return Assessment(pid, NOT_ASSESSED, (), None, f"{ctx.why_not(*cats)}. Reading so far: {note}")
    return Assessment(pid, state, refs, conf, note)


def _absent(ctx: Ctx, pid: str, what: str, *cats: str) -> Assessment:
    return _settle(ctx, pid, NOT_MET, ctx.refs(PRIVACY, *cats), "medium", f"the privacy notice was read in full and says nothing about {what}", *cats)


def _fail(pid: str, why: str) -> Assessment:
    return Assessment(pid, NOT_ASSESSED, (), None, why)


# ---- interpretation by the model --------------------------------------------------------------------------------------------------------------------------------------

class Judgment(BaseModel):
    verdict: Literal["yes", "partly", "no", "cannot_tell"]
    confidence: Literal["high", "medium", "low"]
    reason: str
    fact_ids: list[int]


class FormJudgment(BaseModel):
    index: int
    necessary: Literal["yes", "no", "cannot_tell"]
    excess_fields: list[str]
    reason: str
    confidence: Literal["high", "medium", "low"]


class FormBatch(BaseModel):
    items: list[FormJudgment]


class Conflict(BaseModel):
    fact_id_a: int
    fact_id_b: int
    explanation: str


class Conflicts(BaseModel):
    items: list[Conflict]


_JUDGE_SYSTEM = (
    "You judge one requirement about a company's privacy notice, for a readiness review under India's Digital Personal Data Protection Act, 2023. You are given numbered facts, each "
    "extracted from the company's own pages with the exact quote. Judge only from them. Do not use outside knowledge and do not cite any section number. "
    "verdict: yes = the facts fully satisfy the requirement; partly = they satisfy part of it; no = they do not satisfy it; cannot_tell = they are not enough to say. "
    "confidence is your own: low when you are unsure. fact_ids lists the numbers of the facts your verdict rests on."
)


def _fact_lines(facts: list[dict]) -> str:
    return "\n".join(f"[{f['id']}] value: {f['value']} | specific: {f['specific']} | quote: \"{f['quote'][:300]}\"" for f in facts)


async def _judge(llm: LLMCallable, pid: str, question: str, body: str) -> tuple[dict, Answer]:
    system = _JUDGE_SYSTEM + f"\n\nRequirement ({pid}): {BY_ID[pid].name}.\n{question}"
    ans = await call_structured(llm, system, body, Judgment)
    j: Judgment = ans.parsed          # type: ignore[assignment]
    return j.model_dump(), ans


def _sample_passages(text: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) >= 200]
    if not paras:
        return [text[:N4_SAMPLE_CHARS]] if text.strip() else []
    step = max(1, len(paras) // N4_SAMPLES)
    return [p[:N4_SAMPLE_CHARS] for p in paras[::step][:N4_SAMPLES]]


def consent_requests(ctx: Ctx) -> list[tuple[str, str, dict]]:
    """(evidence id, page url, form) for forms that ask for consent: a checkbox, a pre-ticked control, or consent wording."""
    out = []
    rx = re.compile(r"\b(consent|i agree|i accept|i have read|by (submitting|clicking|signing|continuing))\b", re.I)
    for ev_id, data in ctx.loaded.form_data:
        for f in data.get("forms", []):
            blob = " ".join([f.get("text") or "", f.get("label_text") or ""])
            if f.get("consent_controls") or f.get("preticked") or rx.search(blob):
                out.append((ev_id, data.get("page_url", ""), f))
    return out


def distinct_forms(ctx: Ctx) -> list[tuple[str, str, dict]]:
    """One form per distinct (fields, wording): (evidence id, page url, form)."""
    seen, out = set(), []
    for ev_id, data in ctx.loaded.form_data:
        for f in data.get("forms", []):
            sig = (tuple(sorted((x.get("type", ""), x.get("name", "")) for x in f.get("fields", []))), normalise(f.get("text") or "")[:200])
            if sig in seen or not f.get("fields"):
                continue
            seen.add(sig)
            out.append((ev_id, data.get("page_url", ""), f))
    return out


async def interpret(ctx: Ctx, llm: LLMCallable, run: Runner) -> None:
    """The few readings that are about meaning. Each is one persisted unit (level `mapping`); a failed one is recorded as failed and its parameter becomes not assessed."""
    pol_facts = lambda topic: ctx.of(topic, PRIVACY)                                  # noqa: E731

    async def unit(pid: str, idx: int, count: int, fn: Callable[[], Awaitable[tuple[dict, Answer]]]) -> dict:
        res = await run(f"param:{pid}", idx, count, fn)
        return res if res is not None else {"failed": "the model could not produce a valid reading"}

    jobs: dict[str, Awaitable[dict]] = {}
    for pid, topic, q in (
        ("N2", "data_category", "Does the notice give an itemised description of the personal data it processes: specific items (such as name, email address, IP address, payment details), not only general "
                                "categories like 'personal information' or 'data you provide'?"),
        ("N3", "purpose", "Does the notice state specific purposes, tied to the goods, services or uses concerned (for example 'to deliver your order'), not only generic ones like 'to improve our services'?"),
    ):
        facts = pol_facts(topic)
        if facts:
            body = f"Facts ({len(facts)}):\n{_fact_lines(facts)}"
            jobs[pid] = unit(pid, 0, 1, lambda pid=pid, q=q, body=body: _judge(llm, pid, q, body))
    policy = ctx.src(PRIVACY)
    full = "\n\n".join(s.text for s in policy)
    if policy and tr.detect_language(full)["language"] == "en":
        metrics = tr.readability(full)
        samples = _sample_passages(full)
        body = (f"Readability figures over the whole policy ({len(full)} characters): {metrics}.\n\nSampled passages (every {max(1, len(samples))}th long paragraph; the verdict rests on the figures "
                f"and these samples):\n" + "\n---\n".join(samples))
        q = "Is the notice written in clear and plain language, understandable on its own without other documents, by an ordinary reader (short sentences, no unexplained legal jargon)?"
        jobs["N4"] = unit("N4", 0, 1, lambda: _judge(llm, "N4", q, body))
    reqs = consent_requests(ctx)
    if reqs:
        wording = [f for f in ctx.of("consent_language") if f["category"] != "none"][:40]
        texts = [normalise(" ".join([f.get("text") or "", f.get("label_text") or ""]))[:500] for _, _, f in reqs[:15]]
        body = f"Facts from consent wording ({len(wording)}):\n{_fact_lines(wording)}\n\nForm wording near consent requests:\n" + "\n".join(f"- {t}" for t in dict.fromkeys(texts))
        q = "Is the wording that asks for consent clear and in plain language (the person can tell what they agree to)?"
        jobs["C1"] = unit("C1", 0, 1, lambda: _judge(llm, "C1", q, body))
    forms = distinct_forms(ctx)
    batches = [forms[i:i + FORMS_PER_CALL] for i in range(0, len(forms), FORMS_PER_CALL)]

    async def form_batch(idx: int, batch: list[tuple[str, str, dict]]) -> dict:
        async def fn() -> tuple[dict, Answer]:
            lines = []
            for n, (_, url, f) in enumerate(batch):
                flds = "; ".join(f"{x.get('label') or x.get('name') or x.get('type')} ({x.get('type')}{', required' if x.get('required') else ''})" for x in f.get("fields", []))
                lines.append(f"Form {n} on {url}: wording: {normalise(f.get('text') or '')[:300]} | near: {normalise(f.get('label_text') or '')[:200]} | fields: {flds}")
            system = ("You review web forms for data minimisation under India's DPDP Act, 2023: a form should ask only for personal data necessary for its stated purpose. For each form say whether its "
                      "fields are necessary for what the form evidently does (a contact form needs a way to reply and the message; a newsletter needs an email). List excess_fields exactly as they appear in the "
                      "fields list. cannot_tell when the purpose is unclear. Do not cite any section number.")
            ans = await call_structured(llm, system, "\n".join(lines), FormBatch)
            return {"items": [i.model_dump() for i in ans.parsed.items]}, ans          # type: ignore[attr-defined]
        res = await run("forms", idx, len(batches), fn)
        return res if res is not None else {"failed": "the model could not produce a valid reading"}

    results = {}
    if jobs:
        import asyncio
        done = await asyncio.gather(*jobs.values())
        results = dict(zip(jobs.keys(), done))
    ctx.judgments.update(results)
    if batches:
        import asyncio
        outs = await asyncio.gather(*(form_batch(i, b) for i, b in enumerate(batches)))
        ctx.judgments["C9"] = {"batches": [{"forms": [(e, u, f) for e, u, f in b], "result": o} for b, o in zip(batches, outs)]}


async def find_conflicts(ctx: Ctx, llm: LLMCallable, run: Runner) -> list[dict]:
    """Level 3, model part: contradictions between pages (retention, purposes, sharing). Informational: they are listed in the analysis record and never change a state. Only run when two or
    more pages carry facts on the same topic; every cited fact must exist and the two must come from different places."""
    groups: dict[str, list[dict]] = {}
    for f in ctx.facts:
        if f["topic"] in ("retention", "purpose", "sharing_recipient", "grievance_period", "children_directed", "children_not_directed") and f["category"] in (PRIVACY, COOKIE, TERMS, GRIEVANCE, CONTACT):
            groups.setdefault(f["topic"], []).append(f)
    groups = {t: fs for t, fs in groups.items() if len({f["url"] for f in fs}) >= 2}
    if not groups:
        return []

    async def fn() -> tuple[dict, Answer]:
        body = "\n\n".join(f"Topic {t}:\n{_fact_lines(fs)} (source urls: " + "; ".join(sorted({f['url'] for f in fs})) + ")" for t, fs in groups.items())
        system = ("You compare facts quoted from different pages of one company's website. List pairs of facts that CONTRADICT each other (for example two different retention periods for the same data, "
                  "or one page saying data is never shared and another listing recipients). Cite the two fact numbers. Do not list differences that are merely different details. Do not cite any section number.")
        ans = await call_structured(llm, system, body, Conflicts)
        return {"items": [i.model_dump() for i in ans.parsed.items]}, ans           # type: ignore[attr-defined]

    res = await run("conflicts", 0, 1, fn)
    out = []
    by_id = {f["id"]: f for f in ctx.facts}
    for c in (res or {}).get("items", []):
        a, b = by_id.get(c["fact_id_a"]), by_id.get(c["fact_id_b"])
        if a and b and a["topic"] == b["topic"] and (a["url"], a["quote"]) != (b["url"], b["quote"]):
            out.append({"topic": a["topic"], "a": _cite(a), "b": _cite(b), "explanation": c["explanation"][:300]})
    return out


# ---- level 3, deterministic cross-page observations -------------------------------------------------------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(s: str) -> date | None:
    s = s or ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return _d(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?[ /.-]+([A-Za-z]{3,9})\.?,?[ /.-]+(\d{4})", s)
    if m and m[2][:3].lower() in _MONTHS:
        return _d(int(m[3]), _MONTHS[m[2][:3].lower()], int(m[1]))
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", s)
    if m and m[1][:3].lower() in _MONTHS:
        return _d(int(m[3]), _MONTHS[m[1][:3].lower()], int(m[2]))
    m = re.search(r"([A-Za-z]{3,9})\.?,?\s+(\d{4})", s)
    if m and m[1][:3].lower() in _MONTHS:
        return _d(int(m[2]), _MONTHS[m[1][:3].lower()], 1)
    m = re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", s)
    if m:
        return _d(int(m[3]), int(m[2]), int(m[1]))
    return None


def _d(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def observed_trackers(ctx: Ctx) -> dict[str, dict]:
    """The analytics and advertising services the browser contacted before any choice: {key: {"vendor", "host", "category", "evidence": [ids]}}."""
    out: dict[str, dict] = {}
    for ev_id, n in ctx.loaded.networks:
        for t in n.get("non_essential", []) or []:
            key = (t.get("vendor") or t.get("host") or "").lower()
            if not key:
                continue
            e = out.setdefault(key, {"vendor": t.get("vendor") or t.get("host"), "host": t.get("host"), "category": t.get("category"), "evidence": []})
            e["evidence"].append(ev_id)
    return out


def third_party_hosts(ctx: Ctx) -> set[str]:
    return {h["host"] for _, n in ctx.loaded.networks for h in n.get("hosts", []) if h.get("third_party")}


def observed_cookies(ctx: Ctx) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ev_id, e in ctx.loaded.cookies:
        for c in e.get("cookies", []):
            d = out.setdefault(c.get("identity") or f"{c.get('name')}|{c.get('domain')}", {"name": c.get("name", ""), "domain": c.get("domain", ""), "first_party": bool(c.get("first_party")), "evidence": []})
            d["evidence"].append(ev_id)
    return out


def observations(ctx: Ctx) -> dict:
    """Level 3 findings that are plain comparisons. Returned for the analysis record; the parameters X1 to X3 and N8 use the same functions."""
    declared = ctx.text(PRIVACY, COOKIE)
    trackers = observed_trackers(ctx)
    undisclosed = sorted(k for k, t in trackers.items() if not tr.names_any(declared, tr.vendor_tokens(t["vendor"], t["host"]))) if ctx.src(PRIVACY) else []
    cookies = observed_cookies(ctx)
    undeclared = sorted(c["name"] for c in cookies.values() if not tr.cookie_declared(c["name"], "" if c["first_party"] else c["domain"], declared)) if ctx.src(PRIVACY) else []
    no_notice = sorted({u for _, u, f in distinct_forms(ctx) if _collects_personal(f) and not _has_notice_link(f)})
    pol_urls = sorted({s.url for s in ctx.src(PRIVACY)})
    outdated = []
    for f in ctx.of("policy_date", PRIVACY, COOKIE):
        d = parse_date(f["value"]) or parse_date(f["quote"])
        if d and (ctx.as_of - d).days > OUTDATED_AFTER_DAYS:
            outdated.append({"url": f["url"], "date": d.isoformat()})
    return {"undisclosed_trackers": undisclosed, "undeclared_cookies": undeclared, "forms_without_notice_link": no_notice, "duplicate_policies": pol_urls if len(pol_urls) > 1 else [],
            "possibly_outdated": outdated}


# ---- the parameters ---------------------------------------------------------------------------------------------------------------------------------------------------------

def _collects_personal(f: dict) -> bool:
    c = set(f.get("collects") or [])
    return bool(c) and not (c <= {"password", "email"} and "password" in c)           # a login form is not a collection of personal data for this purpose


def _has_notice_link(f: dict) -> bool:
    return bool(f.get("privacy_link")) or bool(re.search(r"privacy|data protection|notice", " ".join([f.get("text") or "", f.get("label_text") or ""]), re.I))


def _verdict_state(pid: str, j: dict) -> str | None:
    v = j.get("verdict")
    return {"yes": MET, "no": NOT_MET, "partly": PARTLY if BY_ID[pid].partial_allowed else HUMAN_REVIEW}.get(v)


def n1(ctx: Ctx) -> Assessment:
    pol = ctx.src(PRIVACY)
    if pol:
        urls = sorted({s.url for s in pol})
        return Assessment("N1", MET, ctx.refs(PRIVACY), "high", f"a privacy notice was found at {urls[0]}" + (f" (and {len(urls) - 1} more)" if len(urls) > 1 else ""))
    if not ctx.loaded.texts:
        return _fail("N1", "no page text was read, so the notice could not be looked for")
    if ctx.unresolved:
        return _fail("N1", f"{len(ctx.unresolved)} page(s) that may be the privacy notice could not be classified: {', '.join(ctx.unresolved[:3])}")
    searched = tuple(dict.fromkeys(f"ev:{s.evidence_id}" for s in ctx.loaded.texts))[:MAX_REFS]
    return Assessment("N1", NOT_MET, searched, "medium", f"{len({s.url for s in ctx.loaded.texts})} crawled pages were checked and none is a privacy notice")


def _data_or_purpose(ctx: Ctx, pid: str, topic: str, need: int, what: str) -> Assessment:
    facts = ctx.of(topic, PRIVACY)
    j = ctx.judgments.get(pid)
    if not facts:
        return _absent(ctx, pid, what)
    if j is None or "failed" in j:
        return _fail(pid, f"{len(facts)} {what} were extracted but the model's reading of whether they are specific failed" + (f": {j['failed']}" if j else ""))
    if j["verdict"] == "cannot_tell":
        return Assessment(pid, NOT_ASSESSED, (), None, f"the reading of whether the {what} are specific was inconclusive: {j['reason'][:200]}")
    spec = {normalise(f["value"]).lower() for f in facts if f["specific"]}
    ok_ids = [i for i in j.get("fact_ids", []) if any(f["id"] == i for f in facts)]
    support = [f for f in facts if f["id"] in ok_ids] or facts
    refs, conf = _refs_of(support), j["confidence"]
    note = f"{len(spec)} specific of {len({normalise(f['value']).lower() for f in facts})} distinct {what}; model: {j['reason'][:200]}; e.g. {_cite(support[0])}"
    if len(spec) >= need and j["verdict"] == "yes":
        return Assessment(pid, MET, refs, conf, note)
    if len(spec) >= need and j["verdict"] == "partly":
        return Assessment(pid, PARTLY, refs, conf, note)
    if len(spec) >= need and j["verdict"] == "no":
        return Assessment(pid, PARTLY, refs, "low", note + " (the model reads them as not specific, against the count)")
    if not spec and j["verdict"] in ("yes", "partly"):
        # The extraction marked none of the items specific, but the model reads some as concrete: the two disagree, so neither is allowed to decide a finding. A person looks at it.
        return Assessment(pid, HUMAN_REVIEW, refs, "low", note + " (no item was marked specific, yet the model reads some as concrete: a person should decide)")
    if spec:
        return _settle(ctx, pid, PARTLY, refs, conf, note)
    return _settle(ctx, pid, NOT_MET, refs, conf, note + " (only general categories)")


def n4(ctx: Ctx) -> Assessment:
    pol = ctx.src(PRIVACY)
    if not pol:
        return _fail("N4", "no privacy notice was found")
    if tr.detect_language("\n".join(s.text for s in pol))["language"] != "en":
        return _fail("N4", "the notice is not in English: the plain-language reading is made on English text only")
    j = ctx.judgments.get("N4")
    if j is None or "failed" in j:
        return _fail("N4", "the model's reading of the notice's language failed" + (f": {j['failed']}" if j else ""))
    st = _verdict_state("N4", j)
    note = f"model reading on readability figures over the whole notice and sampled passages: {j['reason'][:250]}. A person should confirm this reading (human-review parameter)."
    if st is None:
        return Assessment("N4", NOT_ASSESSED, (), None, "the reading was inconclusive: " + j["reason"][:200])
    if st == HUMAN_REVIEW:
        return Assessment("N4", HUMAN_REVIEW, ctx.refs(PRIVACY), None, note)
    if st == NOT_MET and not ctx.read_in_full():
        return _fail("N4", ctx.why_not())
    return Assessment("N4", st, ctx.refs(PRIVACY), _cap(j["confidence"], "medium"), note)


def n5(ctx: Ctx) -> Assessment:
    cats = (PRIVACY, GRIEVANCE, TERMS, CONTACT)
    w = ctx.of("withdraw_consent", *cats, pattern=r"withdraw|revoke|opt[- ]?out|unsubscribe|change your consent")
    e = ctx.of("exercise_channel", *cats)
    link = [f for f in e if tr.URL.search(f["quote"] + " " + f["value"]) or re.search(r"\b(link|portal|dashboard|settings|web ?form|online form|account)\b", f["quote"] + " " + f["value"], re.I)]
    got = [("withdrawal statement", w), ("means to exercise rights", e), ("link or portal", link)]
    n = sum(1 for _, x in got if x)
    support = w[:2] + e[:2] + link[:2]
    note = "; ".join(f"{label}: {'yes' if x else 'no'}" for label, x in got) + (f"; e.g. {_cite(support[0])}" if support else "")
    if n == 3:
        return Assessment("N5", MET, _refs(support), "high", note)
    if n:
        return _settle(ctx, "N5", PARTLY, _refs(support), "medium", note, *cats)
    return _absent(ctx, "N5", "withdrawing consent or exercising rights", *cats)


def n6(ctx: Ctx) -> Assessment:
    f = ctx.of("board_complaint", PRIVACY, GRIEVANCE, TERMS, CONTACT, pattern=r"\bboard\b")
    if f:
        return Assessment("N6", MET, _refs(f), "high", f"the notice names the Board: {_cite(f[0])}")
    return _absent(ctx, "N6", "complaining to the Data Protection Board", GRIEVANCE, TERMS, CONTACT)


def n7(ctx: Ctx) -> Assessment:
    pol = ctx.src(PRIVACY)
    if not pol:
        return _fail("N7", "no privacy notice was found")
    lang = tr.detect_language("\n".join(s.text for s in pol))
    refs = ctx.refs(PRIVACY)
    if lang["language"] == "en":
        return Assessment("N7", MET, refs, "high", f"the notice is written in English (English words make up {lang['english_ratio']:.0%} of its words)")
    if lang["language"] == "eighth_schedule":
        return Assessment("N7", MET, refs, "medium", f"the notice is written in the {tr.EIGHTH_SCHEDULE_SCRIPTS[lang['script']]} script, an Eighth Schedule language")
    urls = {s.url for s in pol}
    codes: set[str] = set()
    hdr = []
    for ev_id, h in ctx.loaded.headers:
        if h.get("page_url") in urls:
            c = tr.option_languages(h.get("alternates", []), None)
            if c:
                hdr.append(f"ev:{ev_id}")
            codes |= c
    opts = [f for f in ctx.of("language_option", PRIVACY) if tr.names_language_option(f["value"] + " " + f["quote"])]
    if "en" in codes or codes & tr.EIGHTH_SCHEDULE_CODES or opts:
        extra = _refs_of(opts) + tuple(hdr)
        return Assessment("N7", MET, tuple(dict.fromkeys(refs + extra))[:MAX_REFS], "medium", f"the notice is not in English, but a language option is declared ({', '.join(sorted(codes)) or opts[0]['value']})")
    return Assessment("N7", NOT_MET, refs, "medium", f"the notice is neither in English nor in an Eighth Schedule language's script, and no English or Eighth Schedule option is declared ({lang['script']} script)")


def n8(ctx: Ctx) -> Assessment:
    forms = [(e, u, f) for e, u, f in ((e, d.get("page_url", ""), f) for e, d in ctx.loaded.form_data for f in d.get("forms", [])) if _collects_personal(f)]
    if not forms:
        return _fail("N8", "no form that collects personal data was recorded")
    bad = [(e, u) for e, u, f in forms if not _has_notice_link(f)]
    refs = tuple(dict.fromkeys(f"ev:{e}" for e, _, _ in forms))[:MAX_REFS]
    if not bad:
        return Assessment("N8", MET, refs, "medium", f"all {len(forms)} data-collecting forms carry a link to a privacy notice (or name one beside the form)")
    urls = sorted({u for _, u in bad})
    return Assessment("N8", NOT_MET, tuple(dict.fromkeys(f"ev:{e}" for e, _ in bad))[:MAX_REFS], "medium",
                      f"{len(bad)} of {len(forms)} data-collecting forms carry no link to a privacy notice, e.g. {', '.join(urls[:3])}")


def c1(ctx: Ctx) -> Assessment:
    reqs = consent_requests(ctx)
    if not reqs:
        return _fail("C1", "no consent request was found in the forms (a consent banner's wording is not recorded)")
    j = ctx.judgments.get("C1")
    if j is None or "failed" in j:
        return _fail("C1", "the model's reading of the consent wording failed" + (f": {j['failed']}" if j else ""))
    if j["verdict"] == "cannot_tell":
        return _fail("C1", "the reading of the consent wording was inconclusive: " + j["reason"][:200])
    blob = " ".join(" ".join([f.get("text") or "", f.get("label_text") or ""]) for _, _, f in reqs)
    lang = tr.detect_language(blob)["language"] in ("en", "eighth_schedule")
    plain = j["verdict"] == "yes"
    refs = tuple(dict.fromkeys(f"ev:{e}" for e, _, _ in reqs))[:MAX_REFS]
    note = f"plain language: {'yes' if plain else 'no'} ({j['reason'][:160]}); English or Eighth Schedule language: {'yes' if lang else 'no'}; {len(reqs)} consent request(s)"
    state = MET if (plain and lang) else PARTLY if (plain or lang) else NOT_MET
    return Assessment("C1", state, refs, j["confidence"], note)


_OFFICER = re.compile(r"data protection officer|\bdpo\b|grievance officer|privacy officer|authori[sz]ed (person|representative)|responsible person|privacy (team|contact)|contact (us|our)", re.I)


def c2(ctx: Ctx) -> Assessment:
    reqs = consent_requests(ctx)
    if not reqs:
        return _fail("C2", "no consent request was found in the forms (a consent banner's wording is not recorded)")
    have, lack = [], []
    for e, u, f in reqs:
        blob = " ".join([f.get("text") or "", f.get("label_text") or ""])
        (have if _OFFICER.search(blob) and (tr.EMAIL.search(blob) or tr.PHONE.search(blob) or tr.URL.search(blob)) else lack).append((e, u))
    if not lack:
        return Assessment("C2", MET, tuple(dict.fromkeys(f"ev:{e}" for e, _ in have))[:MAX_REFS], "medium", f"all {len(have)} consent request(s) give a named contact with an email, phone or link")
    return Assessment("C2", NOT_MET, tuple(dict.fromkeys(f"ev:{e}" for e, _ in lack))[:MAX_REFS], "medium",
                      f"{len(lack)} of {len(reqs)} consent request(s) give no DPO or authorised-person contact, e.g. {sorted({u for _, u in lack})[0]}")


_CONSENT_LABEL = re.compile(r"consent|agree|accept|terms|privacy|newsletter|marketing|subscribe|updates|promotion|offers|opt|contact me|communicat|share", re.I)
_BUNDLE_TERMS = [re.compile(p, re.I) for p in (r"terms", r"privacy", r"marketing|promotion|offers|newsletter", r"third[- ]part|partners|share", r"analytics|research|profiling")]
_BY_SUBMIT = re.compile(r"by (submitting|clicking|signing up|registering|continuing)[^.]{0,80}\b(agree|consent|accept)", re.I)


def c3(ctx: Ctx) -> Assessment:
    forms = [(e, d.get("page_url", ""), f) for e, d in ctx.loaded.form_data for f in d.get("forms", [])]
    if not forms or not any("checked" in x for _, _, f in forms for x in f.get("fields", [])):
        return _fail("C3", "the form evidence does not record whether controls are pre-ticked (it predates that field), so this could not be established")
    pre, unticked, bundled, by_submit = [], [], [], []
    for e, u, f in forms:
        controls = [x for x in f.get("fields", []) if x.get("type") in ("checkbox", "radio") and _CONSENT_LABEL.search(" ".join(str(x.get(k) or "") for k in ("label", "name", "id")))]
        for x in controls:
            label = " ".join(str(x.get(k) or "") for k in ("label", "name"))
            if x.get("checked"):
                pre.append((e, u, label))
            else:
                unticked.append((e, u))
                if sum(1 for rx in _BUNDLE_TERMS if rx.search(label)) >= 3:
                    bundled.append((e, u))
        if not controls and _collects_personal(f) and _BY_SUBMIT.search(" ".join([f.get("text") or "", f.get("label_text") or ""])):
            by_submit.append((e, u))
    ref = lambda items: tuple(dict.fromkeys(f"ev:{i[0]}" for i in items))[:MAX_REFS]       # noqa: E731
    if pre:
        return Assessment("C3", NOT_MET, ref(pre), "high", f"{len(pre)} consent control(s) are pre-ticked when the page loads, e.g. \"{pre[0][2][:80]}\" on {pre[0][1]}")
    if not unticked:
        return _fail("C3", "no consent checkbox was found in any form, so whether consent is by a clear affirmative action could not be established (a form with no checkbox may rest on another ground)")
    if bundled or by_submit:
        why = f"{len(bundled)} control(s) bundle several purposes" if bundled else f"{len(by_submit)} data-collecting form(s) rely on 'by submitting you agree' without a control"
        return Assessment("C3", PARTLY, ref(unticked + bundled + by_submit), "medium", f"{len(unticked)} consent control(s) are unticked, but {why}")
    return Assessment("C3", MET, ref(unticked), "medium", f"{len(unticked)} consent control(s) found, none pre-ticked, each a separate choice (a banner's own controls are tested separately)")


def c9(ctx: Ctx) -> Assessment:
    j = ctx.judgments.get("C9")
    if not j:
        return _fail("C9", "no form was recorded to read")
    assessed, excess, conf, failed = [], [], "high", 0
    for b in j["batches"]:
        if "failed" in b["result"]:
            failed += len(b["forms"])
            continue
        for item in b["result"]["items"]:
            if not 0 <= item["index"] < len(b["forms"]) or item["necessary"] == "cannot_tell":
                continue
            ev_id, url, f = b["forms"][item["index"]]
            names = {str(x.get(k) or "").lower() for x in f.get("fields", []) for k in ("label", "name", "id")}
            real = [x for x in item["excess_fields"] if x.lower() in names]          # an excess field must be a field the form really has
            assessed.append((ev_id, url))
            conf = min(conf, item["confidence"], key=lambda c: RANK[c])
            if item["necessary"] == "no" and real:
                excess.append((ev_id, url, real))
    if not assessed:
        return _fail("C9", "no form could be read" + (f" ({failed} form(s) failed in the model)" if failed else ""))
    refs = tuple(dict.fromkeys(f"ev:{e}" for e, _ in assessed))[:MAX_REFS]
    if not excess:
        state = MET
    elif len(excess) * 2 < len(assessed):
        state = PARTLY
    else:
        state = NOT_MET
    if state in (PARTLY, NOT_MET) and failed:
        return _fail("C9", f"{failed} form(s) could not be read by the model, so the share of forms asking for excess data is unknown")
    note = f"{len(assessed)} distinct form(s) read; {len(excess)} ask for data beyond their purpose" + (f", e.g. {excess[0][1]}: {', '.join(excess[0][2])}" if excess else "")
    return Assessment("C9", state, tuple(dict.fromkeys(f"ev:{e}" for e, _, _ in excess))[:MAX_REFS] or refs, _cap(conf, "medium"), note)


def g1(ctx: Ctx) -> Assessment:
    cats = (PRIVACY, GRIEVANCE, CONTACT, ABOUT)
    f = [x for x in ctx.of("dpo_contact", *cats) if (tr.EMAIL.search(x["quote"]) or tr.PHONE.search(x["quote"]) or tr.URL.search(x["quote"]) or tr.EMAIL.search(x["value"]))
         and re.search(r"officer|dpo|privacy|data protection|grievance|contact|responsible", x["quote"] + " " + x["value"], re.I)]
    if f:
        return Assessment("G1", MET, _refs(f), "high", f"a contact for data protection is published: {_cite(f[0])}")
    return _absent(ctx, "G1", "a DPO or responsible person's contact details", *cats)


def g2(ctx: Ctx) -> Assessment:
    cats = (PRIVACY, GRIEVANCE, TERMS, CONTACT)
    mech = ctx.of("grievance_mechanism", *cats, pattern=r"grievance|complain|redress|officer|concern|query|queries|contact")
    per = ctx.of("grievance_period", *cats)
    days = [(d, f) for f in per for d in (tr.parse_days(f["value"]) or tr.parse_days(f["quote"]))]
    support = mech[:2] + per[:2]
    if days:
        worst, wf = max(days, key=lambda x: x[0])
        if worst > 90:
            return Assessment("G2", NOT_MET, _refs([wf]), "high", f"the stated response period is {worst:g} days, over the 90-day limit: {_cite(wf)}")
        return Assessment("G2", MET, _refs(support), "high" if mech else "medium", f"a grievance route is published with a response period of {worst:g} days (limit 90): {_cite(wf)}")
    if mech:
        return _settle(ctx, "G2", PARTLY, _refs(support), "medium", "a grievance route is published but no response period in days is stated" + (f" ({_cite(per[0])})" if per else ""), *cats)
    return _absent(ctx, "G2", "a grievance mechanism", *cats)


def g3(ctx: Ctx) -> Assessment:
    cats = (PRIVACY, GRIEVANCE, TERMS, CONTACT)
    a = ctx.of("right_access", *cats, pattern=r"access|copy|obtain|know|confirm")
    c = ctx.of("right_correction", *cats, pattern=r"correct|rectif|update|amend|accurate")
    e = ctx.of("right_erasure", *cats, pattern=r"erase|erasure|delet|remov|forgotten")
    m = ctx.of("exercise_channel", *cats)
    got = [("access", a), ("correction", c), ("erasure", e), ("means to exercise", m)]
    n = sum(1 for _, x in got if x)
    support = [x[0] for _, x in got if x]
    note = "; ".join(f"{k}: {'yes' if v else 'no'}" for k, v in got)
    if n == 4:
        return Assessment("G3", MET, _refs(support), "high", note)
    if n:
        return _settle(ctx, "G3", PARTLY, _refs(support), "medium", note, *cats)
    return _absent(ctx, "G3", "access, correction or erasure", *cats)


def _statement(ctx: Ctx, pid: str, topic: str, pattern: str, what: str, cats: tuple[str, ...] = (PRIVACY, GRIEVANCE, TERMS)) -> Assessment:
    f = ctx.of(topic, *cats, pattern=pattern)
    if f:
        return Assessment(pid, MET, _refs(f), "high", f"{what} is stated: {_cite(f[0])}")
    return _absent(ctx, pid, what, *cats)


def g4(ctx: Ctx) -> Assessment:
    return _statement(ctx, "G4", "right_nominate", r"nominat", "the right to nominate another person", (PRIVACY, GRIEVANCE, TERMS, CONTACT))


def g5(ctx: Ctx) -> Assessment:
    return _statement(ctx, "G5", "retention", r"retain|retention|stored|kept|keep|delet|erase|period|as long as|no longer", "retention or erasure")


def g7(ctx: Ctx) -> Assessment:
    return _statement(ctx, "G7", "security", r"secur|encrypt|safeguard|protect|firewall|access control", "a security safeguards statement")


def g8(ctx: Ctx) -> Assessment:
    return _statement(ctx, "G8", "breach", r"breach|incident|notify|intimat|compromis", "a breach process")


def g6(ctx: Ctx) -> Assessment:
    f = [x for x in ctx.of("sharing_recipient", PRIVACY, COOKIE) if x["specific"] and normalise(x["value"]).lower()[:40] and any(w in x["quote"].lower() for w in re.split(r"\W+", x["value"].lower()) if len(w) >= 4)]
    if f:
        names = sorted({x["value"] for x in f})
        return Assessment("G6", MET, _refs(f), "high", f"the policy names {len(names)} recipient(s), e.g. {', '.join(names[:4])}: {_cite(f[0])}")
    hosts = third_party_hosts(ctx)
    if ctx.loaded.networks and not hosts and not ctx.of("sharing_recipient", PRIVACY):
        return Assessment("G6", NOT_APPLICABLE, (), None, "the browser contacted no third-party host and the policy names no recipient")
    return _absent(ctx, "G6", "which third parties or processors receive data" + (f" ({len(hosts)} third-party host(s) were contacted)" if hosts else ""), COOKIE)


_CHILD_STRONG_PATH = re.compile(r"\b(kids?|child|children|toddlers?|preschool|kindergarten|juniors?|teens?|minors?)\b", re.I)
_CHILD_WEAK = re.compile(r"\b(students?|schools?|k-?12|parents?|learners?|classroom|pupils?)\b", re.I)
_GUARDIAN_FIELD = re.compile(r"guardian|parent|child'?s? name|date of birth of (the )?child", re.I)


def k1(ctx: Ctx) -> Assessment:
    cats = (PRIVACY, TERMS)
    strong, weak = [], []
    for p in ctx.loaded.pages.values():
        if p.category in (pg.ARTICLE,):
            continue
        hay = " ".join([p.url.split("?")[0].replace("/", " ").replace("-", " ").replace("_", " "), p.title, " ".join(p.headings[:6])])
        if _CHILD_STRONG_PATH.search(hay):
            strong.append(p)
        elif _CHILD_WEAK.search(hay):
            weak.append(p)
    form_hits = [(e, d.get("page_url", "")) for e, d in ctx.loaded.form_data for f in d.get("forms", []) for x in f.get("fields", []) if _GUARDIAN_FIELD.search(" ".join(str(x.get(k) or "") for k in ("label", "name", "id", "placeholder")))]
    directed = ctx.of("children_directed", *cats)
    consent = ctx.of("children_consent", *cats, pattern=r"parent|guardian|verifiable|consent")
    notrack = ctx.of("children_no_tracking", *cats, pattern=r"track|profil|advertis|target|behaviou?r")
    not_directed = ctx.of("children_not_directed", *cats)
    def page_refs(ps: list) -> tuple[str, ...]:
        urls = {p.url for p in ps}
        return tuple(dict.fromkeys(f"ev:{s.evidence_id}" for s in ctx.loaded.texts if s.url in urls))[:MAX_REFS]
    if not (directed or consent or strong or form_hits):
        if weak:
            return Assessment("K1", HUMAN_REVIEW, page_refs(weak), None, f"{len(weak)} page(s) mention students, schools or parents (e.g. {weak[0].url}); whether the site serves children is for a person to decide")
        if not ctx.loaded.texts:
            return _fail("K1", "no page text was read")
        note = "the site states it is not directed at children" if not_directed else "no page, form or statement shows the site serves children"
        return Assessment("K1", NOT_APPLICABLE, _refs(not_directed), None, note)
    refs = tuple(dict.fromkeys(_refs(directed + consent + notrack) + page_refs(strong) + tuple(f"ev:{e}" for e, _ in form_hits)))[:MAX_REFS]
    why = "the site appears to serve children: " + "; ".join(x for x in (f"statement: {_cite(directed[0])}" if directed else "", f"page {strong[0].url}" if strong else "", f"guardian field on {form_hits[0][1]}" if form_hits else "",
                                                                          f"guardian consent: {_cite(consent[0])}" if consent else "") if x)
    n = bool(consent) + bool(notrack)
    note = f"{why}; guardian consent described: {'yes' if consent else 'no'}; no tracking or targeted advertising at children stated: {'yes' if notrack else 'no'}"
    if n == 2:
        return Assessment("K1", MET, refs, "medium", note)
    if n == 1:
        return _settle(ctx, "K1", PARTLY, refs, "medium", note, *cats)
    return _settle(ctx, "K1", NOT_MET, refs, "medium", note, *cats)


def m1(ctx: Ctx) -> Assessment:
    f = ctx.of("consent_manager", pattern=r"consent manager")
    if f:
        return Assessment("M1", HUMAN_REVIEW, _refs(f), None, f"a Consent Manager is mentioned ({_cite(f[0])}); whether it is registered with the Board cannot be seen from the website")
    if not ctx.loaded.texts:
        return _fail("M1", "no page text was read")
    return Assessment("M1", NOT_APPLICABLE, (), None, "no Consent Manager integration appears in the pages read")


def x1(ctx: Ctx) -> Assessment:
    if not ctx.src(PRIVACY, COOKIE):
        return _fail("X1", "no privacy or cookie policy was found to compare with")
    cookies = observed_cookies(ctx)
    if not cookies:
        return Assessment("X1", NOT_APPLICABLE, (), None, "no cookie was observed on any crawled page")
    text = ctx.text(PRIVACY, COOKIE)
    miss = sorted((c["name"], c["domain"]) for c in cookies.values() if not tr.cookie_declared(c["name"], "" if c["first_party"] else c["domain"], text))
    n, d = len(cookies), len(cookies) - len(miss)
    refs = tuple(dict.fromkeys([f"ev:{i}" for c in cookies.values() for i in c["evidence"]][:MAX_REFS // 2] + list(ctx.refs(PRIVACY, COOKIE))[:MAX_REFS // 2]))
    note = f"{d} of {n} distinct cookies are named in the policy" + (f"; not named: {', '.join(m[0] for m in miss[:8])}" if miss else "")
    state = MET if not miss else PARTLY if d * 2 >= n else NOT_MET
    return Assessment("X1", state, refs, "high" if state == MET else "medium", note)


def x2(ctx: Ctx) -> Assessment:
    if not ctx.src(PRIVACY):
        return _fail("X2", "no privacy policy was found to compare with")
    trackers = observed_trackers(ctx)
    if not trackers:
        return Assessment("X2", MET, tuple(dict.fromkeys(f"ev:{i}" for i, _ in ctx.loaded.networks))[:MAX_REFS] or ctx.refs(PRIVACY), "medium",
                          "the browser contacted no analytics or advertising service before a choice, so none is missing from the policy")
    text = ctx.text(PRIVACY, COOKIE)
    missing = {k: t for k, t in trackers.items() if not tr.names_any(text, tr.vendor_tokens(t["vendor"], t["host"]))}
    named = len(trackers) - len(missing)
    refs = tuple(dict.fromkeys([f"ev:{i}" for t in missing.values() for i in t["evidence"]][:MAX_REFS - 5] + list(ctx.refs(PRIVACY, COOKIE))[:5]))
    if not missing:
        return Assessment("X2", MET, tuple(dict.fromkeys([f"ev:{i}" for t in trackers.values() for i in t["evidence"]][:MAX_REFS - 5] + list(ctx.refs(PRIVACY, COOKIE))[:5])), "medium",
                          f"all {len(trackers)} analytics or advertising service(s) contacted are named in the policy")
    listing = ", ".join(f"{t['vendor']} ({t['host']})" for t in list(missing.values())[:6])
    state = PARTLY if named * 2 >= len(trackers) else NOT_MET
    return Assessment("X2", state, refs, "medium", f"{len(missing)} of {len(trackers)} service(s) contacted before any choice are not named in the policy: {listing}")


def x3(ctx: Ctx) -> Assessment:
    if not ctx.src(PRIVACY):
        return _fail("X3", "no privacy policy was found to compare with")
    forms = [(e, f) for e, d in ctx.loaded.form_data for f in d.get("forms", [])]
    kinds: dict[str, list[str]] = {}
    for e, f in forms:
        if _collects_personal(f) or (f.get("collects") and "password" not in f["collects"]):
            for k in f.get("collects", []):
                kinds.setdefault(k, []).append(e)
    if not kinds:
        return Assessment("X3", NOT_APPLICABLE, (), None, "no form collecting personal data was recorded")
    text = ctx.text(PRIVACY)
    miss = sorted(k for k in kinds if not tr.policy_mentions(k, text))
    n, d = len(kinds), len(kinds) - len(miss)
    refs = tuple(dict.fromkeys([f"ev:{e}" for k in (miss or list(kinds)) for e in kinds[k]][:MAX_REFS - 5] + list(ctx.refs(PRIVACY))[:5]))
    state = MET if not miss else PARTLY if d * 2 >= n else NOT_MET
    return Assessment("X3", state, refs, "high" if state == MET else "medium", f"forms collect {n} kind(s) of data ({', '.join(sorted(kinds))}); the policy itemises {d}" + (f"; not itemised: {', '.join(miss)}" if miss else ""))


def s1(ctx: Ctx) -> Assessment:
    return Assessment("S1", NEEDS_RECORDS, (), None, "whether the organisation is a Significant Data Fiduciary, and its DPO, audit and impact assessment, are not visible on a public website; they need the organisation's records")


def t1(ctx: Ctx) -> Assessment:
    f = ctx.of("cross_border")
    return Assessment("T1", NEEDS_RECORDS, _refs(f), None, "whether a transfer outside India meets a restriction the Central Government has notified needs the organisation's records" + (f"; the notice says: {_cite(f[0])}" if f else ""))


def e1(ctx: Ctx) -> Assessment:
    return Assessment("E1", HUMAN_REVIEW, (), None, "whether an exemption (research, State, a notified class) applies is a decision for a person, not an observation of the website")


def assess_all(ctx: Ctx) -> list[Assessment]:
    """Every parameter this package owns, in catalogue order. Never raises for one parameter: a rule that fails is reported as that parameter being not assessed."""
    rules: list[tuple[str, Callable[[Ctx], Assessment]]] = [
        ("N1", n1), ("N2", lambda c: _data_or_purpose(c, "N2", "data_category", 3, "kinds of personal data")), ("N3", lambda c: _data_or_purpose(c, "N3", "purpose", 2, "purposes")), ("N4", n4),
        ("N5", n5), ("N6", n6), ("N7", n7), ("N8", n8), ("C1", c1), ("C2", c2), ("C3", c3), ("C9", c9), ("G1", g1), ("G2", g2), ("G3", g3), ("G4", g4), ("G5", g5), ("G6", g6), ("G7", g7),
        ("G8", g8), ("K1", k1), ("S1", s1), ("M1", m1), ("T1", t1), ("E1", e1), ("X1", x1), ("X2", x2), ("X3", x3)]
    out = []
    for pid, fn in rules:
        try:
            out.append(fn(ctx))
        except Exception as exc:                               # a bug in one rule must not lose the others, and must never read as a failure of the site
            out.append(_fail(pid, f"the rule could not be applied ({type(exc).__name__}: {str(exc)[:120]})"))
    return out


__all__ = ["Ctx", "assess_all", "interpret", "find_conflicts", "observations", "LLM_PARAMS", "TOPICS", "LLMFailure"]
