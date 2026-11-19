# Enterprise DPDP scoring method

Version `ent-dpdp-2.0.0` (catalogue `2.0.0`). Code: `backend/app/enterprise/scoring/` (`catalogue.py`, `engine.py`). Tests: `backend/tests/enterprise/test_enterprise_scoring.py`.
This is independent of the 5-page agent's P1 to P12 scoring, which it does not import and which is unchanged.

## Approval record

| Item | Status |
|---|---|
| **Version 2: 100 points over 50 scored parameters (51 in all), as in the table below** | **Approved by the owner, 19 November 2026** ("approved", after the proposal table was shown). Replaces the 33-parameter catalogue of 13 Nov 2026 |
| **Only three statuses: Pass, Partially Pass, Fail** | **Owner's instruction, 19 Nov 2026** |
| **A duty that does not apply to the site is Pass, with the reason** | **Owner's decision, 19 Nov 2026** |
| **A duty a website cannot show is judged from the published policy** (kind `policy`) | **Owner's decision, 19 Nov 2026** |
| **What could not be verified is Fail, with what and why** (never skipped) | **Owner's instruction, 19 Nov 2026** |
| Result credit: Pass 1, Partially Pass 0.5, Fail 0 | Approved 13 Nov 2026, kept |
| Before 13 May 2027 the score counts parameters not yet in force, labelled "readiness" | Approved 13 Nov 2026, kept |
| Score = marks earned out of 100; coverage reported beside it, never withholding it | Owner's decision of 16 Nov 2026, kept (coverage now means the marks decided on collected evidence) |
| Penalty ceilings from the Act's Schedule are a separate risk label, not part of the score | Approved, kept |
| Named person to sign off the legal registry | **Not given.** The registry stays a draft: `LEGAL_REVIEW_STATUS` |

Superseded: the 13 Nov rule that six parameters (G5, G7, G8, S1, T1, E1) earn or lose nothing (now only E1), and the 16 Nov rule that not applicable earns nothing (now a Pass with the reason).

## The method

1. Each of 50 parameters carries fixed points; together they are exactly 100. E1 (an exemption claimed) is shown for information and earns or loses nothing.
2. Every parameter ends as **Pass** (full points), **Partially Pass** (half, only where the parameter allows a partial) or **Fail** (none). No other status is reported.
3. The assessors may record what they found more precisely, and that is kept for the audit trail (`assessed_state`); the engine resolves it:
   - does not apply to the site → **Pass**, with the reason;
   - could not be verified (for example the entry page did not load) → **Fail**, saying what could not be verified and why;
   - needs the organisation's records → **Fail**, "not shown in the published policy";
   - awaits a person's decision → **Fail** until a reviewer decides.
4. A model's low-confidence reading keeps its result and is marked "a reviewer should confirm" (`needs_attention.review_recommended`). A reviewer's decision replaces any result, with the reviewer's name and reason.
5. `score = round half up of the points earned`, out of 100. `coverage` = the marks decided on evidence actually collected (not on a "could not be verified" or "needs records" fallback); it is reported beside the score and never changes it. There is no score only when nothing at all could be verified.
6. A Pass, Partially Pass or Fail found by an assessor needs at least one evidence reference. The same parameter cannot be assessed twice.
7. Supporting parameters (C6 and TS1 to TS6, 7 points) are scored like the others.
8. Before 13 May 2027 most duties are not in force; the result is labelled `readiness` and lists the parameters not yet in force. Section 6(9) and Rule 4 (Consent Managers: M1, M2) start on 13 November 2026.
9. Legal exposure is separate: for each duty (kind `statutory` or `policy`) not fully met, the most the Schedule lets the Board impose (item 1: 250 crore, 2 and 3: 200 crore, 4: 150 crore, 7: 50 crore). It is a ceiling set by law, not a prediction.
10. Every Fail and Partially Pass raises one finding; a Fail that rests on something not verifiable is marked for a person to check. E1 raises none.

## Points

| Group | Parameters (points) | Points |
|---|---|---|
| Notice | N1 3, N2 4, N3 4, N4 2, N5 4, N6 2, N7 2, N8 2 | 23 |
| Consent | C1 2, C2 2, C3 2, C4 4, C5 4, C6 1 (supporting), C7 3, C8 2, C9 2, **C10 2** (consent mechanism where tracking runs), **C11 2** (choice per purpose), **C12 1** (no term waiving a right, s.6(2)), **C13 1** (consent records, s.6(10)) | 28 |
| Contact, grievance and rights | G1 3, G2 3, G3 3, G4 2, G6 2, **G10 1** (identifiers and verification for requests, R14(1)(b), R14(2)) | 14 |
| Duties judged from the published policy | G5 2 (retention and erasure), G7 3 (security safeguards R6(1)), G8 3 (breach intimation R7), **G9 1** (one-year log retention R6(1)(e), R8(3)), **L1 2** (ground for processing s.4, s.7), **P1 2** (processors under contract s.8(2), R6(1)(f)), **P2 1** (accuracy s.8(3)), T1 2 (transfer outside India s.16, R15) | 16 |
| Conditional (Pass, with the reason, where the duty does not apply) | K1 2 (children), **K2 1** (guardian of a person with disability, R11), S1 1 (Significant Data Fiduciary), M1 1 (Consent Manager used), **M2 1** (the organisation is a Consent Manager, Sch1 Part B), **R1 1** (Third Schedule erasure with 48 hours' notice) | 7 |
| Cross-checks | X1 2, X2 2, X3 2 | 6 |
| Technical (supporting) | TS1 to TS6, 1 each | 6 |
| Information only | E1 (exemption claimed) | 0 |
| **Total** | **51 parameters** | **100** |

Left out on purpose (not duties a website owner can show): the Act's scope (s.3), the State's legitimate uses and their standards (s.7(b) to (i), Rule 5, Second Schedule), the Data Principal's duties (s.15), and the Board, appeals and Government procedure (ss.18 to 37, Rules 17 to 23, Seventh Schedule). The report lists them under "Parts of the Act and Rules outside a website assessment".

## Where each parameter is decided

- Consent behaviour (C4 to C8, C10, C11): `app/enterprise/assess/consent.py`, from the browser's consent sessions. Where no analytics or advertising runs and no banner exists, C5 to C8, C10 and C11 do not apply (Pass); where it runs with no banner, they Fail on the evidence of what ran.
- Technical signals (TS1 to TS6): `app/enterprise/assess/technical.py`.
- Text, policy and cross-check parameters: `app/enterprise/analysis/consolidate.py`, from facts the model extracts with a verbatim quote (topics in `analysis/extract.py`; the twelve topics added for version 2 change the extraction prompt, so each passage is read once more on the next scan).

## What this does not do

It does not certify an organisation as DPDP compliant. A duty judged from the published policy says what the policy states, not that the organisation does it.
