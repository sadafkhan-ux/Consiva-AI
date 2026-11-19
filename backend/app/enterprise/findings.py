"""Findings: what a person should act on or look at, built from the scored result (Pass, Partially Pass or Fail).

One finding per parameter that did not Pass. It carries the parameter's legal references, the pages it was raised on (the true count, and up to 50 addresses), the evidence it rests
on, what was expected, what was observed, why, how confident the reading is, what to do about it, and whether a person must look at it. Repeated observations of one parameter are
grouped into the one finding with every evidence reference kept, so nothing unique is dropped and nothing is repeated 120 times.

Which parameters raise a finding (method version 2, owner's decision of 19 Nov 2026):
  Fail, Partially Pass     a finding, with a severity (below). A Fail that rests on "could not be verified" or "needs records" (not on evidence) says so, and is marked for a person to check.
  Pass                     no finding (a Pass because the duty does not apply carries its reason in the parameter, not as a finding)

Severity follows the parameter, not a guess: a statutory parameter worth 3 or more points that Fails is `high`, worth 2 `medium`, worth 1 `low`; Partially Pass is one step lower; a
supporting technical signal is `low`. The Act's Schedule ceiling is attached as a ceiling set by law, never as a prediction.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.scoring.catalogue import BY_ID, SUPPORTING, Parameter
from app.enterprise.scoring.engine import NOT_MET, PARTLY, ScoreResult, status_label
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
    "C10": "Show a consent banner or consent tool on every page where analytics, advertising or other consent-dependent services run, before they run.",
    "C11": "Let visitors give or refuse consent for each purpose separately (for example analytics, advertising, personalisation), not only Accept all or Reject all.",
    "C12": "Remove any term that asks a person to waive a right under the Act, such as the right to complain to the Data Protection Board; such a term is invalid.",
    "C13": "Keep a record of each consent (what was agreed, when, the notice shown) so you can prove it, and say in the policy that you keep it.",
    "G1": "Publish, prominently on the site, the business contact information of the Data Protection Officer or of a person who can answer questions about the processing of personal data.",
    "G2": "Publish your grievance redressal mechanism and the period within which you respond, which must not be more than 90 days.",
    "G3": "Publish how a person requests access, correction and erasure, and which identifying particulars you need from them.",
    "G4": "State that a person may nominate another individual to exercise their rights in case of death or incapacity, and how to do it.",
    "G5": "State how long each kind of personal data is kept, and that it is erased when consent is withdrawn or the purpose is served, unless a law requires it to be kept.",
    "G6": "Name, in the privacy policy, the third parties and processors that personal data is shared with or that the site contacts.",
    "G7": "Describe the safeguards you apply: encryption or masking, access control, logging and monitoring of access, and backups for continued processing.",
    "G8": "Describe how you tell affected people and the Data Protection Board of a personal data breach, without delay and with a detailed report to the Board within 72 hours.",
    "G9": "Keep logs of access and processing, with the personal data concerned, for at least one year for detecting and investigating misuse, and say so in the policy.",
    "G10": "Publish which identifier a person must give to make a rights request (for example registered email, mobile number or customer ID) and how you verify the requester.",
    "L1": "State, for each purpose, whether you process personal data on consent or on a legitimate use under the Act (for example data the person provided voluntarily).",
    "P1": "State that processors act only under a contract with you that requires reasonable security safeguards and erasure of the data when the work ends.",
    "P2": "State how you keep personal data complete, accurate and consistent, especially where it is used for decisions about a person or shared with another fiduciary.",
    "K1": "If the site serves children, obtain verifiable parental consent before processing, and do not track, profile or target advertising at them.",
    "K2": "If you serve persons with disability who have a lawful guardian, obtain the guardian's verifiable consent and check the guardianship order, and describe this in the policy.",
    "S1": "If you are notified as a Significant Data Fiduciary, appoint a DPO based in India and an independent data auditor, run an impact assessment and audit every year, and say so.",
    "M1": "If a Consent Manager is integrated, confirm it is registered with the Board and that your handling of its consent records follows the Act.",
    "M2": "As a Consent Manager, publish your promoters, directors, key managerial personnel and shareholders above 2%, and how Data Principals can access their consent records.",
    "R1": "If you are an e-commerce, online gaming or social media platform in the Third Schedule, erase data after three years' inactivity and tell the person 48 hours before.",
    "T1": "State whether personal data is transferred outside India, to where and on what terms, and confirm transfers meet any restriction the Central Government has notified.",
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
    if p.kind == SUPPORTING or p.points == 0:
        return "low"
    if state == NOT_MET:
        return "high" if p.points >= 3 else "medium" if p.points == 2 else "low"
    return "medium" if p.points >= 3 else "low"            # partially pass


def _expected(p: Parameter) -> str:
    if p.kind == SUPPORTING:
        return f"Good practice (a technical signal, not a duty named in the Act or Rules): {p.name}."
    refs = ", ".join(p.legal_refs)
    return f"{p.name}. Basis in the Act and Rules: {refs}." if refs else f"{p.name}."


def _title(p: Parameter, state: str) -> str:
    return f"{status_label(state)}: {p.name}"


def draft_for(p: Parameter, state: str, note: str, confidence: str | None, evidence_refs: list[str], urls: list[str], *, on_evidence: bool = True) -> FindingDraft | None:
    """A finding for a Fail or a Partially Pass; None for a Pass. `on_evidence` is False when the Fail rests on "could not be verified" or "needs records", not on what was observed."""
    if state not in (NOT_MET, PARTLY) or p.points == 0:          # E1 is shown for information only: it raises no finding
        return None
    if not on_evidence:
        reason = "This could not be verified on the evidence collected, so it is reported as Fail; a person should check it on the site."
    else:
        reason = {NOT_MET: "The evidence collected shows this was not met.", PARTLY: "The evidence collected shows this was met in part."}[state]
    return FindingDraft(
        finding_key=p.id, parameter_id=p.id, title=_title(p, state), description=note or _title(p, state), severity=severity_for(p, state), state=state,
        legal_refs=list(p.legal_refs), affected_urls=urls[:MAX_LISTED_URLS], affected_count=len(urls), evidence_refs=evidence_refs, expected=_expected(p), observed=note or "", reason=reason,
        confidence=confidence, recommendation=RECOMMENDATIONS[p.id], human_review=(not on_evidence) or p.human_review or confidence == "low",
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


async def build_findings(db: AsyncSession, assessments, result: ScoreResult | None = None) -> list[FindingDraft]:
    """`assessments` are the current rows (anything with parameter_id, state, note, confidence, evidence_refs); `result` the score worked out from them, whose Pass / Partially Pass / Fail
    decides the findings (without it, the recorded states are used as they are). Returns the findings, most serious first."""
    lines = {x.parameter_id: x for x in result.lines} if result is not None else {}
    drafts: list[FindingDraft] = []
    for a in assessments:
        p = BY_ID.get(a.parameter_id)
        if p is None:
            continue
        line = lines.get(a.parameter_id)
        state = line.state if line else a.state
        on_evidence = line.on_evidence if line else True
        note = a.note or ""
        if line is not None and line.adjustment and not on_evidence:
            note = line.adjustment
        refs = list(a.evidence_refs or [])
        urls = await _urls_for(db, refs) if state in (NOT_MET, PARTLY) else []
        d = draft_for(p, state, note, a.confidence, refs, urls, on_evidence=on_evidence)
        if d is not None:
            drafts.append(d)
    if result is not None:                                  # a parameter with no recorded assessment at all still Fails, and still raises its finding
        recorded = {a.parameter_id for a in assessments}
        for x in result.lines:
            if x.parameter_id not in recorded:
                d = draft_for(BY_ID[x.parameter_id], x.state, x.adjustment or "", None, [], [], on_evidence=x.on_evidence)
                if d is not None:
                    drafts.append(d)
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    return sorted(drafts, key=lambda d: (order[d.severity], d.parameter_id))

