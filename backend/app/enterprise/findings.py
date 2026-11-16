"""Findings: what a person should act on or look at, built from the current parameter assessments.

One finding per parameter that is not fully met. It carries the parameter's legal references, the pages it was raised on (the true count, and up to 50 addresses), the evidence it rests
on, what was expected, what was observed, why, how confident the reading is, what to do about it, and whether a person must look at it. Repeated observations of one parameter are
grouped into the one finding with every evidence reference kept, so nothing unique is dropped and nothing is repeated 120 times.

Which parameters raise a finding:
  not met, partly met      a finding, with a severity (below)
  human review             an `info` finding that says a person must decide, never a verdict
  needs records            an `info` finding that says the organisation's own records are needed
  not assessed             no finding: it is listed under limitations, because "could not be checked" is not a defect of the site
  met, not applicable      no finding

Severity follows the parameter, not a guess: a statutory parameter worth 5 points that is not met is `high`, worth 3 `medium`; partly met is one step lower; a supporting technical signal is
`low`; what a person must decide or the organisation must evidence is `info`. The Act's Schedule ceiling is attached as a ceiling set by law, never as a prediction.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.scoring.catalogue import BY_ID, SUPPORTING, Parameter
from app.enterprise.scoring.engine import HUMAN_REVIEW, NEEDS_RECORDS, NOT_MET, PARTLY
from app.enterprise.store import FindingDraft

MAX_LISTED_URLS = 50

RECOMMENDATIONS: dict[str, str] = {
    "N1": "Publish a privacy notice and link to it from every page footer and from every place consent or personal data is requested.",
    "N2": "List, item by item, each kind of personal data you collect (for example name, email, phone, device identifiers), not only a general statement.",
    "N3": "State the specific purpose of each use of personal data and the specific goods, services or uses it enables, in plain language.",
    "N4": "Rewrite the notice so it can be understood on its own, in clear and plain language, without needing other documents to follow it.",
    "N5": "Explain in the notice how a person can withdraw consent as easily as they gave it, how to exercise their rights, and give the link to the site or app where they can do so.",
    "N6": "Add how a person can make a complaint to the Data Protection Board of India to the notice.",
    "N7": "Offer the notice in English and in any language of the Eighth Schedule you serve, and make the choice visible from the notice.",
    "N8": "Show the notice, or a direct link to it, with every request for consent: on the consent banner and beside every form that collects personal data.",
    "C1": "Write the consent request in clear plain language and offer it in English or an Eighth Schedule language.",
    "C2": "Give the contact details of the Data Protection Officer, or another person authorised to answer, in the consent request.",
    "C3": "Make consent a clear affirmative action: nothing pre-ticked, separate choices for separate purposes.",
    "C4": "Do not load analytics, advertising or marketing tags or set their cookies until the visitor has chosen. Hold tag-manager tags until Accept, and set Google consent mode to denied by default.",
    "C5": "Make Reject stop analytics and advertising for the rest of the visit and on later pages, and check it on every template, not only the home page.",
    "C6": "Give Reject the same prominence as Accept on the first layer of the banner (a button of equal size and weight, not a text link).",
    "C7": "Make the control that reopens the cookie choice easy to find on every page (for example a persistent 'Cookie settings' link) and let it withdraw consent in one step.",
    "C8": "When consent is withdrawn, stop analytics and advertising tags at once, on the current page and on later pages, including tags a tag manager releases after a delay.",
    "C9": "Remove form fields that are not necessary for the stated purpose, or mark them optional and explain why they are asked.",
    "G1": "Publish, prominently on the site, the business contact information of the Data Protection Officer or of a person who can answer questions about the processing of personal data.",
    "G2": "Publish your grievance redressal mechanism and the period within which you respond, which must not be more than 90 days.",
    "G3": "Publish how a person requests access, correction and erasure, and which identifying particulars you need from them.",
    "G4": "State that a person may nominate another individual to exercise their rights in case of death or incapacity, and how to do it.",
    "G5": "Describe how long personal data is kept and when it is erased. The retention itself needs your internal records to verify.",
    "G6": "Name, in the privacy policy, the third parties and processors that personal data is shared with or that the site contacts.",
    "G7": "State the security safeguards you apply. Whether they are in place needs your internal records and a security review to verify.",
    "G8": "Describe how you will tell the Board and affected people of a personal data breach. The process itself needs your internal records to verify.",
    "K1": "If the site serves children, obtain verifiable parental consent before processing, and do not track, profile or target advertising at them.",
    "S1": "If you are notified as a Significant Data Fiduciary, appoint a DPO based in India and an independent data auditor, and run periodic impact assessments. These need your internal records.",
    "M1": "If a Consent Manager is integrated, confirm it is registered with the Board and that your handling of its consent records follows the Act.",
    "T1": "Confirm that any transfer of personal data outside India meets the restrictions the Central Government has notified. This needs your internal records.",
    "E1": "Decide with legal advice whether an exemption in the Act applies to this processing.",
    "X1": "Make the cookie policy list every cookie the site sets, with its purpose and duration, and keep it in step with what the site actually sets.",
    "X2": "Name every third party the site contacts in the privacy policy, and remove any you no longer use.",
    "X3": "Make the notice itemise every kind of data your forms ask for, and remove form fields the notice does not explain.",
    "TS1": "Serve every page and every resource over HTTPS and fix mixed content.",
    "TS2": "Renew the certificate before it expires, and monitor its expiry.",
    "TS3": "Disable TLS versions older than 1.2 and offer TLS 1.3 where your platform supports it.",
    "TS4": "Add the missing protective headers: Strict-Transport-Security, Content-Security-Policy, X-Content-Type-Options, Referrer-Policy and Permissions-Policy.",
    "TS5": "Set the Secure attribute and a SameSite value on every cookie.",
    "TS6": "Make sure no redirect moves a visitor from https to http.",
}
assert set(RECOMMENDATIONS) == set(BY_ID), "every parameter has a recommendation"


def severity_for(p: Parameter, state: str) -> str:
    if state in (HUMAN_REVIEW, NEEDS_RECORDS):
        return "info"
    if p.kind == SUPPORTING:
        return "low"
    if state == NOT_MET:
        return "high" if p.points >= 5 else "medium"
    return "medium" if p.points >= 5 else "low"            # partly met


def _expected(p: Parameter) -> str:
    if p.kind == SUPPORTING:
        return f"Good practice (a technical signal, not a duty named in the Act or Rules): {p.name}."
    refs = ", ".join(p.legal_refs)
    return f"{p.name}. Basis in the Act and Rules: {refs}." if refs else f"{p.name}."


def _title(p: Parameter, state: str) -> str:
    label = {NOT_MET: "Not met", PARTLY: "Partly met", HUMAN_REVIEW: "Needs a person's decision", NEEDS_RECORDS: "Needs the organisation's records"}[state]
    return f"{label}: {p.name}"


def draft_for(p: Parameter, state: str, note: str, confidence: str | None, evidence_refs: list[str], urls: list[str]) -> FindingDraft | None:
    if state not in (NOT_MET, PARTLY, HUMAN_REVIEW, NEEDS_RECORDS):
        return None
    reason = {
        NOT_MET: "The evidence collected shows this was not met.",
        PARTLY: "The evidence collected shows this was met in part.",
        HUMAN_REVIEW: "The reading was not certain enough to score, so a person must decide.",
        NEEDS_RECORDS: "A public website cannot show this; the organisation's own records are needed.",
    }[state]
    return FindingDraft(
        finding_key=p.id, parameter_id=p.id, title=_title(p, state), description=note or _title(p, state), severity=severity_for(p, state), state=state,
        legal_refs=list(p.legal_refs), affected_urls=urls[:MAX_LISTED_URLS], affected_count=len(urls), evidence_refs=evidence_refs, expected=_expected(p), observed=note or "", reason=reason,
        confidence=confidence, recommendation=RECOMMENDATIONS[p.id], human_review=state in (HUMAN_REVIEW, NEEDS_RECORDS) or p.human_review,
        schedule_item=p.schedule_item if p.kind != SUPPORTING else None, penalty_ceiling_cr=p.penalty_ceiling_cr if p.kind != SUPPORTING else None)


async def _urls_for(db: AsyncSession, refs: list[str]) -> list[str]:
    ids = []
    for r in refs:
        try:
            ids.append(uuid.UUID(r.removeprefix("ev:")))
        except ValueError:
            continue
    if not ids:
        return []
    rows = (await db.execute(text("select distinct source_url from ent_evidence where id = any(:ids) order by source_url"), {"ids": ids})).all()
    return [r.source_url for r in rows]


async def build_findings(db: AsyncSession, assessments) -> list[FindingDraft]:
    """`assessments` are the current rows (anything with parameter_id, state, note, confidence, evidence_refs). Returns the findings, most serious first."""
    drafts: list[FindingDraft] = []
    for a in assessments:
        p = BY_ID.get(a.parameter_id)
        if p is None:
            continue
        refs = list(a.evidence_refs or [])
        urls = await _urls_for(db, refs) if a.state in (NOT_MET, PARTLY, HUMAN_REVIEW) else []
        d = draft_for(p, a.state, a.note or "", a.confidence, refs, urls)
        if d is not None:
            drafts.append(d)
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    return sorted(drafts, key=lambda d: (order[d.severity], d.parameter_id))

