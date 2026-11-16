# Enterprise DPDP scoring method

Version `ent-dpdp-1.0.0`. Code: `backend/app/enterprise/scoring/` (`catalogue.py`, `engine.py`). Tests: `backend/tests/enterprise/test_enterprise_scoring.py`.
This is independent of the 5-page agent's P1 to P12 scoring, which it does not import.

## Approval record

| Item | Status |
|---|---|
| 100 points over 33 scored parameters, as in the table below | Approved by the owner, 13 November 2026 |
| Result credit: Met 1, Partly met 0.5, Not met 0 | Approved |
| Before 13 May 2027 the score counts parameters not yet in force, labelled "readiness" | Approved (the owner's recommendation was accepted) |
| Minimum coverage of 60 points in play before a score is shown | Approved 13 Nov 2026, **withdrawn by the owner on 16 Nov 2026**: coverage is still worked out and reported beside the score, but it never withholds the score |
| Supporting parameters count, capped at 10% of the points in play | Approved 13 Nov 2026, **withdrawn by the owner on 16 Nov 2026** (no cap; they earn their own points) |
| Parameters not assessed or not applicable are left out of the calculation | Approved 13 Nov 2026, **reversed by the owner on 16 Nov 2026**: the score is the marks earned out of 100, and such a parameter earns 0 |
| Penalty ceilings from the Act's Schedule are a separate risk label, not part of the score | Approved |
| Named person to sign off the legal registry | **Not given.** The registry stays a draft: `LEGAL_REVIEW_STATUS` |
| Pass, partial and fail rule for each parameter | **Not yet written.** `partial_allowed` in the catalogue is a first reading, to be confirmed parameter by parameter. Parameters marked `human_review` need a person to confirm each result |

The approval was taken from the owner's reply "kro suru" to the proposal that read: "points theek hain, baaki meri salah, sign-off [naam]". No name was given.

## The method

1. Each of 33 parameters carries fixed points; together they are exactly 100. Six more (G5, G7, G8, S1, T1, E1) are shown and earn or lose nothing, because a public website cannot show them.
2. A parameter's result is one of: met, partly met, not met, not assessed, not applicable, needs records, human review.
3. Met earns all the parameter's points, partly met half (only where the parameter allows a partial), not met none.
4. Not assessed, not applicable, needs records and human review earn nothing. They stay in the 100, so their points are lost; no points are given without evidence.
5. `score = round half up of the points earned`, out of 100 (`points_available`). `coverage = points in play` (out of 100), reported beside the score but never changing it. There is no score only when nothing could be assessed, and the reason is stated.
6. Supporting parameters (C6 and TS1 to TS6, 8 points) are scored like the others, with no cap.
7. A result that earns or loses points needs at least one evidence reference, or a reviewer's decision. The same parameter cannot be assessed twice.
8. A model's low-confidence reading becomes "human review" and moves no points. A reviewer's decision replaces it, with the reviewer's name and reason.
9. Before 13 May 2027 most duties are not in force; the result is labelled `readiness` and lists the parameters not yet in force. Section 6(9) and Rule 4 (Consent Managers) start on 13 November 2026.
10. Legal exposure is separate: for each statutory parameter not fully met, the most the Schedule to the Act lets the Board impose (item 1: 250 crore, 2 and 3: 200 crore, 4: 150 crore, 7: 50 crore). It is a ceiling set by law, not a prediction. Supporting signals have none.

## Points

| Group | Parameters | Points |
|---|---|---|
| Notice | N2, N3, N5 = 5 each; N1, N4, N6, N7, N8 = 3 each | 30 |
| Consent | C3, C4, C5, C7 = 5 each; C1, C2, C8, C9 = 3 each; C6 = 1 (supporting) | 33 |
| Contact, grievance and rights | G1, G2, G3, G4, G6 = 3 each | 15 |
| Conditional | K1 (children), M1 (Consent Manager) = 3 each; removed when not applicable | 6 |
| Cross-checks | X1, X2, X3 = 3 each | 9 |
| Technical (supporting) | TS1, TS2, TS3, TS5, TS6 = 1 each; TS4 = 2 | 7 |
| **Total** | | **100** |

## A correction to the proposal

The proposal's worked example (six parameters, 16.5 earned of 24 in play) is scored 17 (16.5 earned of 100): since 16 Nov 2026 the parameters not assessed earn nothing and coverage never changes the score. It is tested in `test_the_owners_example_of_six_parameters_is_scored_whatever_the_coverage`.

**Consequence of removing the minimum.** With the marks-earned rule a scan that assessed little scores low, because what was not assessed earns nothing. Coverage is printed beside the score so a reader can see why.

## What this does not do

It does not decide what each parameter's state is. That needs the page, cookie, consent, form and document evidence, the rules and the model reading, which are built separately and feed this engine. It does not certify an organisation as DPDP compliant.
