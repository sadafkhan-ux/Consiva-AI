"""The Enterprise DPDP parameter catalogue: the 39 parameters, what each is, the law it rests on, and its points.

Independent of the 5-page agent: nothing here imports from app.scoring, app.rules, app.policy or app.llm. The points total exactly 100 across the 33 scored parameters; the other six are
shown in a report and earn or lose nothing, because a public website cannot show them.

Points and the method were approved by the owner on 13 November 2026 (see docs/ENTERPRISE_SCORING_METHOD.md). The legal readings (which sentence of the Act a parameter rests on) are
a DRAFT until a named person signs the registry off: `LEGAL_REVIEW_STATUS`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

CATALOGUE_VERSION = "1.0.0"
LEGAL_REVIEW_STATUS = "draft: awaiting a named legal sign-off"

#: Act sections 3 to 17 (except 6(9)) and Rules 3 and 5 to 16 come into force eighteen months after 13 Nov 2025 (G.S.R. 843(E); Rule 1(4)).
IN_FORCE_MAIN = date(2027, 5, 13)
#: Section 6(9) and Rule 4 (Consent Managers) come into force one year after 13 Nov 2025.
IN_FORCE_CONSENT_MANAGER = date(2026, 11, 13)

# kinds
STATUTORY, SUPPORTING, STATEMENT_ONLY, NEEDS_RECORDS, HUMAN_DECISION = "statutory", "supporting", "statement_only", "needs_records", "human_decision"
# groups
NOTICE, CONSENT, CONTACT_RIGHTS, CONDITIONAL, CROSS_CHECK, TECHNICAL = "notice", "consent", "contact_rights", "conditional", "cross_check", "technical"
GROUP_LABELS = {
    NOTICE: "Notice", CONSENT: "Consent", CONTACT_RIGHTS: "Contact, grievance and rights", CONDITIONAL: "Conditional duties",
    CROSS_CHECK: "What the site says against what it does", TECHNICAL: "Technical signals (supporting)",
}


@dataclass(frozen=True)
class Parameter:
    id: str
    name: str
    group: str
    kind: str
    points: int
    legal_refs: tuple[str, ...]
    in_force: date | None
    #: May the result be "partly met" (half credit)? Only where the parameter has more than one component and a partial rule has been written for it.
    partial_allowed: bool = False
    #: A reading a person should confirm before it is relied on.
    human_review: bool = False
    #: Applies only to some organisations: removed from the score, not failed, when it does not apply.
    conditional: bool = False
    #: The most the Act's Schedule lets the Board impose for a breach of this duty (rupees crore), and the Schedule item. None for what is not a statutory duty.
    penalty_ceiling_cr: int | None = None
    schedule_item: int | None = None

    @property
    def scored(self) -> bool:
        return self.points > 0


def _p(id, name, group, kind, points, refs, *, force=IN_FORCE_MAIN, partial=False, review=False, conditional=False, ceiling=50, item=7) -> Parameter:
    statutory = kind in (STATUTORY, STATEMENT_ONLY, NEEDS_RECORDS, HUMAN_DECISION)
    return Parameter(id, name, group, kind, points, tuple(refs), force if kind != SUPPORTING or refs else None, partial, review, conditional,
                     ceiling if statutory else None, item if statutory else None)


PARAMETERS: tuple[Parameter, ...] = (
    # Notice: 30
    _p("N1", "A privacy notice exists and is reachable from the site", NOTICE, STATUTORY, 3, ("s.5(1)", "R3(c)")),
    _p("N2", "The notice gives an itemised description of the personal data", NOTICE, STATUTORY, 5, ("s.5(1)(i)", "R3(b)(i)"), partial=True),
    _p("N3", "The notice gives the specified purposes and the specific goods, services or uses", NOTICE, STATUTORY, 5, ("s.5(1)(i)", "R3(b)(ii)"), partial=True),
    _p("N4", "The notice is understandable independently, in clear and plain language", NOTICE, STATUTORY, 3, ("R3(a)", "R3(b)"), review=True),
    _p("N5", "The notice says how to withdraw consent and exercise rights, with a link to the site or app", NOTICE, STATUTORY, 5, ("s.5(1)(ii)", "R3(c)(i)", "R3(c)(ii)"), partial=True),
    _p("N6", "The notice says how to complain to the Board", NOTICE, STATUTORY, 3, ("s.5(1)(iii)", "R3(c)(iii)")),
    _p("N7", "The notice can be read in English or an Eighth Schedule language", NOTICE, STATUTORY, 3, ("s.5(3)",)),
    _p("N8", "The notice accompanies or precedes each request for consent", NOTICE, STATUTORY, 3, ("s.5(1)",)),
    # Consent: 33
    _p("C1", "The consent request is in clear plain language, with an English or Eighth Schedule option", CONSENT, STATUTORY, 3, ("s.6(3)",), partial=True),
    _p("C2", "The consent request gives contact details of a DPO or authorised person", CONSENT, STATUTORY, 3, ("s.6(3)",)),
    _p("C3", "Consent is by clear affirmative action, nothing pre-ticked, per specified purpose", CONSENT, STATUTORY, 5, ("s.6(1)",), partial=True),
    _p("C4", "No analytics or advertising cookie or request before a choice", CONSENT, STATUTORY, 5, ("s.6(1)",), partial=True, review=True),
    _p("C5", "Reject blocks those cookies and requests", CONSENT, STATUTORY, 5, ("s.6(1)",), partial=True),
    _p("C6", "Reject is offered with the same ease as Accept", CONSENT, SUPPORTING, 1, ()),
    _p("C7", "Consent can be withdrawn as easily as it was given", CONSENT, STATUTORY, 5, ("s.6(4)", "R3(c)(i)"), partial=True),
    _p("C8", "After withdrawal, the processing stops", CONSENT, STATUTORY, 3, ("s.6(6)",)),
    _p("C9", "Forms ask only for data necessary for the purpose", CONSENT, STATUTORY, 3, ("s.6(1)",), partial=True, review=True),
    # Contact, grievance and rights: 15
    _p("G1", "Business contact of the DPO or a responsible person is published prominently", CONTACT_RIGHTS, STATUTORY, 3, ("s.8(9)", "R9")),
    _p("G2", "A grievance mechanism is published, with a response period of not more than 90 days", CONTACT_RIGHTS, STATUTORY, 3, ("s.8(10)", "s.13(1)", "R14(3)"), partial=True),
    _p("G3", "The means and particulars to request access, correction and erasure are published", CONTACT_RIGHTS, STATUTORY, 3, ("s.11", "s.12", "R14(1)"), partial=True),
    _p("G4", "The right to nominate another person is stated", CONTACT_RIGHTS, STATUTORY, 3, ("s.14", "R14(4)")),
    _p("G5", "Retention and erasure are described", CONTACT_RIGHTS, STATEMENT_ONLY, 0, ("s.8(7)", "R8")),
    _p("G6", "Third parties and processors the site contacts are disclosed in the policy", CONTACT_RIGHTS, STATUTORY, 3, ("s.11(1)(b)", "s.8(2)")),
    _p("G7", "A security safeguards statement is made", CONTACT_RIGHTS, STATEMENT_ONLY, 0, ("s.8(5)", "R6(1)"), ceiling=250, item=1),
    _p("G8", "A breach intimation process is described", CONTACT_RIGHTS, STATEMENT_ONLY, 0, ("s.8(6)", "R7"), ceiling=200, item=2),
    # Conditional: 6 scored
    _p("K1", "Where the site serves children: verifiable parental consent, no tracking or targeted advertising directed at children", CONDITIONAL, STATUTORY, 3,
       ("s.9(1)", "s.9(3)", "R10", "R11", "R12"), partial=True, review=True, conditional=True, ceiling=200, item=3),
    _p("S1", "Significant Data Fiduciary duties: DPO in India, audit, impact assessment", CONDITIONAL, NEEDS_RECORDS, 0, ("s.10(2)", "R13"), conditional=True, ceiling=150, item=4),
    _p("M1", "Consent Manager use is consistent with the Act where one is integrated", CONDITIONAL, STATUTORY, 3, ("s.6(7)", "s.6(8)", "s.6(9)", "R4"),
       force=IN_FORCE_CONSENT_MANAGER, conditional=True),
    _p("T1", "Transfer outside India meets any restriction the Central Government has notified", CONDITIONAL, NEEDS_RECORDS, 0, ("s.16", "R15"), conditional=True),
    _p("E1", "An exemption from the Act applies (research, State, class of fiduciary)", CONDITIONAL, HUMAN_DECISION, 0, ("s.17", "R12", "R16"), conditional=True),
    # Cross-checks: 9
    _p("X1", "Cookies observed match the cookies the cookie policy declares", CROSS_CHECK, STATUTORY, 3, ("s.5(1)", "s.6(1)"), partial=True),
    _p("X2", "Third parties contacted match those the privacy policy names", CROSS_CHECK, STATUTORY, 3, ("s.5(1)", "s.11(1)(b)"), partial=True),
    _p("X3", "Form fields collected match the data the notice itemises", CROSS_CHECK, STATUTORY, 3, ("s.5(1)(i)", "R3(b)(i)"), partial=True),
    # Technical, supporting only: 7. Rule 6(1) lists no website control by name, so these are signals, never described as a legal duty.
    _p("TS1", "Pages are served over HTTPS, with no mixed content", TECHNICAL, SUPPORTING, 1, ()),
    _p("TS2", "A valid certificate with more than 30 days left", TECHNICAL, SUPPORTING, 1, ()),
    _p("TS3", "TLS 1.2 or 1.3", TECHNICAL, SUPPORTING, 1, ()),
    _p("TS4", "HSTS, Content-Security-Policy, X-Content-Type-Options, Referrer-Policy and Permissions-Policy", TECHNICAL, SUPPORTING, 2, (), partial=True),
    _p("TS5", "Cookies carry Secure, HttpOnly and SameSite where appropriate", TECHNICAL, SUPPORTING, 1, (), partial=True),
    _p("TS6", "Redirects stay on HTTPS", TECHNICAL, SUPPORTING, 1, ()),
)

BY_ID: dict[str, Parameter] = {p.id: p for p in PARAMETERS}
TOTAL_POINTS = sum(p.points for p in PARAMETERS)
SUPPORTING_POINTS = sum(p.points for p in PARAMETERS if p.kind == SUPPORTING)

# The invariants the method rests on, checked when the module is loaded so an edit that breaks them cannot be imported.
assert len(PARAMETERS) == 39 and len(BY_ID) == 39, "39 distinct parameters"
assert TOTAL_POINTS == 100, f"the points must total exactly 100, not {TOTAL_POINTS}"
assert SUPPORTING_POINTS * 10 <= TOTAL_POINTS, "supporting parameters carry at most 10% of the points"
assert all(p.points > 0 for p in PARAMETERS if p.kind in (STATUTORY, SUPPORTING)), "every statutory or supporting parameter is scored"
assert all(p.points == 0 for p in PARAMETERS if p.kind in (STATEMENT_ONLY, NEEDS_RECORDS, HUMAN_DECISION)), "what a website cannot show earns or loses nothing"
