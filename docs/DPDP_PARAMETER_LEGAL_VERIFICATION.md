# Every parameter checked against the DPDP Act, 2023 and the DPDP Rules, 2025

Generated from `backend/app/scoring/legal_map.py` and checked by `backend/tests/test_legal_map.py`, which finds a phrase copied from each provision in the official PDFs in `knowledge_sources/` (`DPDP Act 2023.pdf`, `DPDP Rules.pdf`). If a phrase is not in the PDF the test fails, so a mapping cannot drift from the text. A mapping is never a determination of compliance. Any change needs the legal sign-off of Functional Scope GR-24.

Commencement: Rules 1, 2 and 17 to 21 on 13 Nov 2025; Rule 4 on 13 Nov 2026; Rules 3, 5 to 16, 22 and 23 on 13 May 2027 (Rule 1 of the Rules). Act ss.3 to 17 on 13 May 2027 (G.S.R. 843(E)). Every provision below starts on **13 May 2027**, so a scan reports readiness, never a current breach.

## Scored parameters (24, 100 points)

| Ref | Parameter | Points | Provision | What it says | Relation | Words checked in the PDF |
|---|---|---|---|---|---|---|
| A1 | Privacy notice published and reachable | 8 | DPDP Act s.5(1) | a notice accompanies or precedes every request for consent | source of the obligation | “accompanied or preceded by a notice” |
|  |  |  | DPDP Rules r.3(a) | the notice is presented and understandable independently of other information | source of the obligation | “understandable independently of any other information” |
| A2 | Notice itemises the personal data collected | 5 | DPDP Act s.5(1) | a notice accompanies or precedes every request for consent | source of the obligation | “accompanied or preceded by a notice” |
|  |  |  | DPDP Rules r.3(b)(i) | an itemised description of the personal data | source of the obligation | “an itemised description of such personal data” |
| A3 | Notice states the specified purposes | 5 | DPDP Act s.5(1) | a notice accompanies or precedes every request for consent | source of the obligation | “accompanied or preceded by a notice” |
|  |  |  | DPDP Rules r.3(b)(ii) | the specified purposes and the goods, services or uses they enable | source of the obligation | “the specified purpose or purposes of” |
| A4 | Notice explains withdrawal, rights and complaints | 5 | DPDP Act s.5(1)(iii) | the notice says how to complain to the Board | source of the obligation | “the manner in which the Data Principal may make a complaint to the Board” |
|  |  |  | DPDP Rules r.3(c)(i) | how to withdraw consent, as easily as it was given | source of the obligation | “withdraw her consent, with the ease of doing so being comparable” |
|  |  |  | DPDP Rules r.3(c)(ii) | how to exercise rights under the Act | source of the obligation | “exercise her rights under the Act” |
|  |  |  | DPDP Rules r.3(c)(iii) | how to make a complaint to the Board | source of the obligation | “make a complaint to the Board” |
|  |  |  | DPDP Act s.6(4) | consent can be withdrawn at any time, as easily as it was given | source of the obligation | “with the ease of doing so being comparable” |
| A5 | Notice reachable site-wide and offered beyond English | 3 | DPDP Act s.5(3) | the notice can be read in English or an Eighth Schedule language | source of the obligation | “in English or any language specified in” |
| B1 | A consent mechanism exists where one is needed | 6 | DPDP Act s.4(1) | personal data is processed only on consent or for a legitimate use | related context | “only in accordance with the provisions of this Act” |
|  |  |  | DPDP Act s.6(1) | consent is free, specific, informed, unconditional and unambiguous, by a clear affirmative action | source of the obligation | “free, specific, informed, unconditional and unambiguous with a clear affirmative action” |
| B2 | Nothing non-essential runs before a choice is made | 7 | DPDP Act s.6(1) | consent is free, specific, informed, unconditional and unambiguous, by a clear affirmative action | source of the obligation | “free, specific, informed, unconditional and unambiguous with a clear affirmative action” |
| B3 | Reject is offered as easily as Accept | 5 | DPDP Act s.6(1) | consent is free, specific, informed, unconditional and unambiguous, by a clear affirmative action | source of the obligation | “free, specific, informed, unconditional and unambiguous with a clear affirmative action” |
|  |  |  | DPDP Act s.6(4) | consent can be withdrawn at any time, as easily as it was given | source of the obligation | “with the ease of doing so being comparable” |
|  |  |  | DPDP Rules r.3(c)(i) | how to withdraw consent, as easily as it was given | source of the obligation | “withdraw her consent, with the ease of doing so being comparable” |
| B4 | Reject is honoured | 5 | DPDP Act s.6(1) | consent is free, specific, informed, unconditional and unambiguous, by a clear affirmative action | source of the obligation | “free, specific, informed, unconditional and unambiguous with a clear affirmative action” |
| B5 | Consent can be withdrawn, and withdrawal is honoured | 3 | DPDP Act s.6(4) | consent can be withdrawn at any time, as easily as it was given | source of the obligation | “with the ease of doing so being comparable” |
|  |  |  | DPDP Act s.6(6) | after withdrawal the Data Fiduciary and its processors cease processing | source of the obligation | “cease and cause its Data Processors to cease” |
| B6 | Withdrawal persists across pages | 2 | DPDP Act s.6(6) | after withdrawal the Data Fiduciary and its processors cease processing | source of the obligation | “cease and cause its Data Processors to cease” |
| C1 | Contact for privacy questions published | 6 | DPDP Act s.8(9) | publish business contact information of a Data Protection Officer or a person who can answer | source of the obligation | “business contact information of a Data Protection Officer” |
|  |  |  | DPDP Rules r.9 | prominently publish business contact information | source of the obligation | “business contact information of the Data Protection Officer” |
| C2 | Grievance mechanism published | 6 | DPDP Act s.8(10) | an effective grievance redressal mechanism | source of the obligation | “effective mechanism to redress the grievances” |
|  |  |  | DPDP Act s.13 | right to readily available means of grievance redressal | source of the obligation | “readily available means of grievance redressal” |
|  |  |  | DPDP Rules r.14(3) | publish the grievance response period, not exceeding ninety days | source of the obligation | “not exceeding ninety days” |
| C3 | Rights and how to exercise them published | 6 | DPDP Act s.11 | right to information about personal data and its processing | source of the obligation | “right to obtain from the Data Fiduciary” |
|  |  |  | DPDP Act s.12 | right to correction, completion, updating and erasure | source of the obligation | “right to correction, completion, updating and erasure” |
|  |  |  | DPDP Rules r.14(1) | publish the means to request the exercise of rights and the identifier needed | source of the obligation | “the details of the means using which a Data Principal may make a request” |
| C4 | Right of nomination disclosed | 2 | DPDP Act s.14 | right to nominate another individual | source of the obligation | “right to nominate” |
|  |  |  | DPDP Rules r.14(4) | how to nominate | source of the obligation | “nominate one or more individuals” |
| D1 | No behavioural tracking on child-directed surfaces | 3 | DPDP Act s.9(3) | no tracking, behavioural monitoring or targeted advertising directed at children | source of the obligation | “tracking or behavioural monitoring of children” |
|  |  |  | DPDP Act s.9(4) | prescribed classes and purposes are exempt from s.9(1) and (3) | related context | “shall not be applicable to processing of personal data of a child” |
|  |  |  | DPDP Rules r.12 | exemptions for classes and purposes in the Fourth Schedule | related context | “Exemptions from certain obligations applicable to processing of personal data of child” |
| D2 | Age assurance or parental consent route present | 3 | DPDP Act s.9(1) | verifiable consent of the parent before processing a child's data | source of the obligation | “verifiable consent of the parent” |
|  |  |  | DPDP Rules r.10(1) | verifiable consent of the parent, with due diligence that the parent is an adult | source of the obligation | “verifiable consent of the parent is obtained” |
|  |  |  | DPDP Act s.9(4) | prescribed classes and purposes are exempt from s.9(1) and (3) | related context | “shall not be applicable to processing of personal data of a child” |
|  |  |  | DPDP Rules r.12 | exemptions for classes and purposes in the Fourth Schedule | related context | “Exemptions from certain obligations applicable to processing of personal data of child” |
| E1 | Third-party services identified and disclosed | 4 | DPDP Act s.8(1) | the Data Fiduciary is responsible for its processors | related context | “on its behalf by a Data Processor” |
|  |  |  | DPDP Act s.8(2) | a processor is engaged only under a valid contract | related context | “only under a valid contract” |
| E2 | Cross-border destinations identifiable | 3 | DPDP Act s.16 | the Central Government may restrict transfers outside India | related context | “restrict the transfer of personal” |
|  |  |  | DPDP Rules r.15 | transfers outside India are subject to requirements the Central Government specifies | related context | “Transfer of personal data outside the territory of India” |
| E3 | No undisclosed data-collecting third party | 3 | DPDP Act s.8(1) | the Data Fiduciary is responsible for its processors | related context | “on its behalf by a Data Processor” |
|  |  |  | DPDP Act s.5(1) | a notice accompanies or precedes every request for consent | source of the obligation | “accompanied or preceded by a notice” |
| F1 | Served securely, with no insecure content on pages that collect data | 3 | DPDP Act s.8(5) | reasonable security safeguards | source of the obligation | “reasonable security safeguards to prevent personal data breach” |
|  |  |  | DPDP Rules r.6(1)(a) | security measures such as encryption | related context | “securing of personal data through encryption” |
| F2 | Form submissions go to a secure, accounted-for destination | 3 | DPDP Act s.8(5) | reasonable security safeguards | source of the obligation | “reasonable security safeguards to prevent personal data breach” |
|  |  |  | DPDP Rules r.6(1)(a) | security measures such as encryption | related context | “securing of personal data through encryption” |
| G1 | Forms that collect personal details present consent or a notice link | 3 | DPDP Act s.5(1) | a notice accompanies or precedes every request for consent | source of the obligation | “accompanied or preceded by a notice” |
|  |  |  | DPDP Act s.6(1) | consent is free, specific, informed, unconditional and unambiguous, by a clear affirmative action | source of the obligation | “free, specific, informed, unconditional and unambiguous with a clear affirmative action” |
| G2 | No pre-selected consent controls | 1 | DPDP Act s.6(1) | consent is free, specific, informed, unconditional and unambiguous, by a clear affirmative action | source of the obligation | “free, specific, informed, unconditional and unambiguous with a clear affirmative action” |

## Readiness indicators (not scored)

They carry evidence and an outcome but no marks. They exist because the Act and Rules ask for things that the Scoring Mechanism does not score, and a change to what is scored needs legal sign-off.

| Id | Indicator | Provision | What it says | Relation | Words checked in the PDF |
|---|---|---|---|---|---|
| I1 | Retention and erasure stated | DPDP Act s.8(7) | erase personal data when the purpose is no longer served | related context | “unless retention is necessary for compliance with any law” |
|  |  | DPDP Rules r.8 | time for erasure and retention of logs | related context | “Time period for specified purpose to be deemed as no longer being served” |
| I2 | Security safeguards described | DPDP Act s.8(5) | reasonable security safeguards | source of the obligation | “reasonable security safeguards to prevent personal data breach” |
|  |  | DPDP Rules r.6(1)(a) | security measures such as encryption | related context | “securing of personal data through encryption” |
| I3 | Personal data breach handling described | DPDP Act s.8(6) | intimation of a personal data breach | related context | “intimation of such breach” |
|  |  | DPDP Rules r.7 | intimation of a personal data breach | related context | “Intimation of personal data breach” |
| I4 | Cookies and tracking technologies described | DPDP Act s.5(1) | a notice accompanies or precedes every request for consent | source of the obligation | “accompanied or preceded by a notice” |
|  |  | DPDP Rules r.3(b)(i) | an itemised description of the personal data | source of the obligation | “an itemised description of such personal data” |
| I5 | Grounds for processing, including legitimate uses, stated | DPDP Act s.4(1) | personal data is processed only on consent or for a legitimate use | related context | “only in accordance with the provisions of this Act” |
| I6 | Grievance response period of at most 90 days stated | DPDP Act s.8(10) | an effective grievance redressal mechanism | source of the obligation | “effective mechanism to redress the grievances” |
|  |  | DPDP Rules r.14(3) | publish the grievance response period, not exceeding ninety days | source of the obligation | “not exceeding ninety days” |
| I7 | How to complain to the Data Protection Board stated | DPDP Act s.5(1)(iii) | the notice says how to complain to the Board | source of the obligation | “the manner in which the Data Principal may make a complaint to the Board” |
|  |  | DPDP Rules r.3(c)(iii) | how to make a complaint to the Board | source of the obligation | “make a complaint to the Board” |
| I8 | Security headers sent by the home page | DPDP Act s.8(5) | reasonable security safeguards | source of the obligation | “reasonable security safeguards to prevent personal data breach” |
|  |  | DPDP Rules r.6(1)(a) | security measures such as encryption | related context | “securing of personal data through encryption” |
| I9 | Each choice by purpose does only what it says | DPDP Act s.6(1) | consent is free, specific, informed, unconditional and unambiguous, by a clear affirmative action | source of the obligation | “free, specific, informed, unconditional and unambiguous with a clear affirmative action” |

## Corrections this review made
- **A4 (notice explains withdrawal, rights and complaints).** The Act (s.5(1)(iii)) and the Rules (r.3(c)(iii)) ask for how to make a complaint **to the Data Protection Board**. The parameter used the Data Fiduciary's own grievance contact. It now requires the Board route; a grievance contact alone earns half of that element and is scored under C2 (s.8(10), s.13).
- **A5 language.** The basis is s.5(3) (the notice in English or an Eighth Schedule language) and s.6(3); it is now cited as such.
- **Anchors.** Domain E now cites s.8(1) as well as s.8(2); r.15 is shown as related context, because it does not itself ask for a notice statement about transfers.
- **Commencement per provision.** Findings now list each provision with its own commencement date and whether it is in force on the scan date, instead of one flag for the domain.

## Provisions that a website scan cannot or should not judge
| Provision | Why it is not scored |
|---|---|
| Rules r.3(a), r.3(b) plain language, independence from other documents | A judgement of wording; Functional Scope DS-19 says a person judges it. |
| Act s.7 legitimate uses; s.8(3), (4), (6), (8); Rules r.5, r.6(1)(b) to (g), r.7 | Organisational practice, records and security that are not visible from outside. Only the notice's statements are shown, as indicators. |
| Act s.9(4), Rules r.12 and the Fourth Schedule (exempt classes such as clinical establishments and educational institutions) | Whether a business is an exempt class is not decided by a scan. D1 and D2 say so and a person decides. |
| Rules r.8 and the Third Schedule (erasure periods for large e-commerce, gaming and social media businesses; 48-hour prior notice; one-year log retention) | Class membership and internal practice. The notice's retention statement is indicator I1. |
| Act s.10, Rules r.13 (Significant Data Fiduciary duties) | Designation is by notification, not visible. |
| Rules r.4 (Consent Managers, from 13 Nov 2026) | Consiva is not a registered Consent Manager and a report must never imply it (GR-06). |
| Rules r.11 (guardians of persons with disability) | Applies only where such data is processed; not detectable. |
| Act s.15 duties of the Data Principal; ss.18 to 44 Board, appeals, penalties, rules | Not obligations of a website. A scan never states that a penalty applies. |
