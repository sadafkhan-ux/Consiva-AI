"""The provisions of the DPDP Act, 2023 and the DPDP Rules, 2025 that each Scanner Score parameter and readiness indicator relates to, and when each comes into force.

Every anchor carries a short phrase copied from the provision itself (`phrase`). tests/test_legal_map.py checks each phrase against the Act and Rules PDFs in knowledge_sources/, so an
anchor cannot drift from the text. A mapping is never a determination (Functional Scope 6.1): `relation` says whether the provision is the source of the obligation the parameter looks at,
related context, or only good practice, and `commences` says when it takes effect (RP-11, RP-12, GR-05).

Commencement (G.S.R. 843(E), 13 Nov 2025 for the Act; Rule 1 of the Rules for the Rules):
  Act   ss.1(2), 2, 18 to 26, 35, 38 to 43, 44(1) and (3): 13 Nov 2025.   s.6(9) and s.27(1)(d): 13 Nov 2026.   ss.3 to 5, 6 (the rest), 7 to 17, 27 (the rest), 28 to 34, 36, 37, 44(2): 13 May 2027.
  Rules rules 1, 2 and 17 to 21: 13 Nov 2025.   rule 4: 13 Nov 2026.   rules 3, 5 to 16, 22 and 23: 13 May 2027.
Any change here is a change to the statutory mapping and needs the named legal sign-off of Functional Scope GR-24.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

NOV_2025, NOV_2026, MAY_2027 = date(2025, 11, 13), date(2026, 11, 13), date(2027, 5, 13)
SOURCE, CONTEXT, PRACTICE = "source of the obligation", "related context", "good practice"


@dataclass(frozen=True)
class Anchor:
    instrument: str          # "Act" or "Rules"
    ref: str                 # "s.5(1)" or "r.3(c)"
    summary: str             # what the provision says, in a few words
    relation: str            # SOURCE, CONTEXT or PRACTICE
    phrase: str              # words copied from the provision, checked against the PDF
    commences: date = MAY_2027

    @property
    def label(self) -> str:
        return f"DPDP {self.instrument} {self.ref}"

    def in_force_on(self, on: date | None) -> bool:
        return on is not None and on >= self.commences


def act(ref: str, summary: str, relation: str, phrase: str, commences: date = MAY_2027) -> Anchor:
    return Anchor("Act", ref, summary, relation, phrase, commences)


def rule(ref: str, summary: str, relation: str, phrase: str, commences: date = MAY_2027) -> Anchor:
    return Anchor("Rules", ref, summary, relation, phrase, commences)


S5_1 = act("s.5(1)", "a notice accompanies or precedes every request for consent", SOURCE, "accompanied or preceded by a notice")
S5_1_iii = act("s.5(1)(iii)", "the notice says how to complain to the Board", SOURCE, "the manner in which the Data Principal may make a complaint to the Board")
S5_3 = act("s.5(3)", "the notice can be read in English or an Eighth Schedule language", SOURCE, "in English or any language specified in")
S6_1 = act("s.6(1)", "consent is free, specific, informed, unconditional and unambiguous, by a clear affirmative action", SOURCE, "free, specific, informed, unconditional and unambiguous with a clear affirmative action")
S6_4 = act("s.6(4)", "consent can be withdrawn at any time, as easily as it was given", SOURCE, "with the ease of doing so being comparable")
S6_6 = act("s.6(6)", "after withdrawal the Data Fiduciary and its processors cease processing", SOURCE, "cease and cause its Data Processors to cease")
S4_1 = act("s.4(1)", "personal data is processed only on consent or for a legitimate use", CONTEXT, "only in accordance with the provisions of this Act")
S8_1 = act("s.8(1)", "the Data Fiduciary is responsible for its processors", CONTEXT, "on its behalf by a Data Processor")
S8_2 = act("s.8(2)", "a processor is engaged only under a valid contract", CONTEXT, "only under a valid contract")
S8_5 = act("s.8(5)", "reasonable security safeguards", SOURCE, "reasonable security safeguards to prevent personal data breach")
S8_6 = act("s.8(6)", "intimation of a personal data breach", CONTEXT, "intimation of such breach")
S8_7 = act("s.8(7)", "erase personal data when the purpose is no longer served", CONTEXT, "unless retention is necessary for compliance with any law")
S8_9 = act("s.8(9)", "publish business contact information of a Data Protection Officer or a person who can answer", SOURCE, "business contact information of a Data Protection Officer")
S8_10 = act("s.8(10)", "an effective grievance redressal mechanism", SOURCE, "effective mechanism to redress the grievances")
S9_1 = act("s.9(1)", "verifiable consent of the parent before processing a child's data", SOURCE, "verifiable consent of the parent")
S9_3 = act("s.9(3)", "no tracking, behavioural monitoring or targeted advertising directed at children", SOURCE, "tracking or behavioural monitoring of children")
S9_4 = act("s.9(4)", "prescribed classes and purposes are exempt from s.9(1) and (3)", CONTEXT, "shall not be applicable to processing of personal data of a child")
S11 = act("s.11", "right to information about personal data and its processing", SOURCE, "right to obtain from the Data Fiduciary")
S12 = act("s.12", "right to correction, completion, updating and erasure", SOURCE, "right to correction, completion, updating and erasure")
S13 = act("s.13", "right to readily available means of grievance redressal", SOURCE, "readily available means of grievance redressal")
S14 = act("s.14", "right to nominate another individual", SOURCE, "right to nominate")
S16 = act("s.16", "the Central Government may restrict transfers outside India", CONTEXT, "restrict the transfer of personal")
R3_a = rule("r.3(a)", "the notice is presented and understandable independently of other information", SOURCE, "understandable independently of any other information")
R3_b_i = rule("r.3(b)(i)", "an itemised description of the personal data", SOURCE, "an itemised description of such personal data")
R3_b_ii = rule("r.3(b)(ii)", "the specified purposes and the goods, services or uses they enable", SOURCE, "the specified purpose or purposes of")
R3_c_i = rule("r.3(c)(i)", "how to withdraw consent, as easily as it was given", SOURCE, "withdraw her consent, with the ease of doing so being comparable")
R3_c_ii = rule("r.3(c)(ii)", "how to exercise rights under the Act", SOURCE, "exercise her rights under the Act")
R3_c_iii = rule("r.3(c)(iii)", "how to make a complaint to the Board", SOURCE, "make a complaint to the Board")
R6_a = rule("r.6(1)(a)", "security measures such as encryption", CONTEXT, "securing of personal data through encryption")
R7 = rule("r.7", "intimation of a personal data breach", CONTEXT, "Intimation of personal data breach")
R8 = rule("r.8", "time for erasure and retention of logs", CONTEXT, "Time period for specified purpose to be deemed as no longer being served")
R9 = rule("r.9", "prominently publish business contact information", SOURCE, "business contact information of the Data Protection Officer")
R10_1 = rule("r.10(1)", "verifiable consent of the parent, with due diligence that the parent is an adult", SOURCE, "verifiable consent of the parent is obtained")
R12 = rule("r.12", "exemptions for classes and purposes in the Fourth Schedule", CONTEXT, "Exemptions from certain obligations applicable to processing of personal data of child")
R14_1 = rule("r.14(1)", "publish the means to request the exercise of rights and the identifier needed", SOURCE, "the details of the means using which a Data Principal may make a request")
R14_3 = rule("r.14(3)", "publish the grievance response period, not exceeding ninety days", SOURCE, "not exceeding ninety days")
R14_4 = rule("r.14(4)", "how to nominate", SOURCE, "nominate one or more individuals")
R15 = rule("r.15", "transfers outside India are subject to requirements the Central Government specifies", CONTEXT, "Transfer of personal data outside the territory of India")

#: parameter -> the provisions it relates to
ANCHORS: dict[str, tuple[Anchor, ...]] = {
    "A1": (S5_1, R3_a),
    "A2": (S5_1, R3_b_i),
    "A3": (S5_1, R3_b_ii),
    "A4": (S5_1_iii, R3_c_i, R3_c_ii, R3_c_iii, S6_4),
    "A5": (S5_3,),
    "B1": (S4_1, S6_1),
    "B2": (S6_1,),
    "B3": (S6_1, S6_4, R3_c_i),
    "B4": (S6_1,),
    "B5": (S6_4, S6_6),
    "B6": (S6_6,),
    "C1": (S8_9, R9),
    "C2": (S8_10, S13, R14_3),
    "C3": (S11, S12, R14_1),
    "C4": (S14, R14_4),
    "D1": (S9_3, S9_4, R12),
    "D2": (S9_1, R10_1, S9_4, R12),
    "E1": (S8_1, S8_2),
    "E2": (S16, R15),
    "E3": (S8_1, S5_1),
    "F1": (S8_5, R6_a),
    "F2": (S8_5, R6_a),
    "G1": (S5_1, S6_1),
    "G2": (S6_1,),
}

#: readiness indicator -> the provisions it relates to (these are not scored)
INDICATOR_ANCHORS: dict[str, tuple[Anchor, ...]] = {
    "I1": (S8_7, R8),
    "I2": (S8_5, R6_a),
    "I3": (S8_6, R7),
    "I4": (S5_1, R3_b_i),
    "I5": (S4_1,),
    "I6": (S8_10, R14_3),
    "I7": (S5_1_iii, R3_c_iii),
    "I8": (S8_5, R6_a),
    "I9": (S6_1,),
}


def statutory_of(ref: str, on: date | None) -> list[dict]:
    """The provisions behind a parameter or indicator, each with its relation and whether it is in force on `on` (the scan date)."""
    anchors = ANCHORS.get(ref) or INDICATOR_ANCHORS.get(ref) or ()
    return [{"provision": a.label, "summary": a.summary, "relation": a.relation, "commences": a.commences.isoformat(), "in_force": a.in_force_on(on)} for a in anchors]


def all_anchors() -> list[Anchor]:
    seen, out = set(), []
    for group in (*ANCHORS.values(), *INDICATOR_ANCHORS.values()):
        for a in group:
            if a.label not in seen:
                seen.add(a.label)
                out.append(a)
    return out
