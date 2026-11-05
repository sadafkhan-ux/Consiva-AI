# The DPDP Website Readiness Scorecard (engine `scorecard-3.0.0`)

Source: `Task01/DPDP_Website_Readiness_Scorecard.xlsx`. Code: `backend/app/scoring/scorecard.py`. Tests: `backend/tests/test_scorecard.py`, `test_scorecard_evidence.py`. It is served by `GET /api/v1/consent/scans/{id}/assessment` (same path, same response fields) and is the default engine (`SCORING_ENGINE=scorecard`). Roll back with `SCORING_ENGINE=scanner` (the 24-check Scanner Score alone) or `ten_check`.

## What it is
Ten rows, 100 marks, three categories. The 24 detailed checks of the Scanner Score stay inside as the detail behind each row ("Level 2"); nothing is detected twice. Every row has a written decision table below.

| Category | Rows | Marks |
|---|---|---|
| Cookie Consent | P1 banner 8, P2 Accept and Reject 8, P3 nothing before a choice 12, P4 Reject blocks 12, P5 withdrawal 8 | 48 |
| Privacy Notice | P6 notice reachable 6, P7 data and purposes 10, P8 contact and grievance 10, P9 rights and routes 10 | 36 |
| Forms & Security | P10 forms, consent, HTTPS 16 | 16 |

Outcomes per row: **pass** (full marks), **partial** (half; every weight is even), **fail** (0), **not assessed** (left out of the denominator: it applies but could not be run), **not applicable** (left out: it does not apply). The workbook has four outcomes; the fifth keeps a check we could not run from being scored as a failure (Scope L-03, GR-18).
Score = marks awarded / marks in play x 100, a whole number. Bands: 90 and over **Substantially addressed**, 75 to 89 **Largely addressed**, 50 to 74 **Partially addressed**, under 50 **Needs attention** (the workbook's thresholds, with neutral names). Fewer than 60% of the marks assessable: **Insufficient evidence** and no score. Low evidence confidence lowers the band one level. A Quick Scan is held at 99; 100 needs a Deep Scan at full coverage and high confidence. **There is no hard-fail cap.**

## Decision tables
| Row | Pass | Partial | Fail | Not assessed / not applicable |
|---|---|---|---|---|
| P1 banner shown | Banner on the home page and on every inner page read, purposes described | Banner missing on some inner pages (P1.2), or no purposes or categories described (P1.3) | Tracking observed and no banner | **N/A** when no banner is needed (nothing analytics or advertising observed). Not assessed if it could not be established |
| P2 Accept and Reject | Reject on the first layer, comparable to Accept | Reject exists but is less prominent (rules below) | No reject control | N/A with P1. Blocked rows below. Not assessed if undetermined |
| P3 nothing before a choice | 0 analytics or advertising cookies or requests, and no tracking identifier in browser storage | Only consent-denied pings, or a tracking identifier in storage | Analytics or advertising before any choice | Not assessed if the pre-consent state was not observed |
| P4 Reject blocks | 0 after Reject, held on the further pages tested | Consent-denied pings only, or activity on a further page | Activity still observed after Reject | Not assessed if Reject could not be clicked and confirmed |
| P5 withdrawal | Reopened, withdrawn, nothing followed (and held on further pages) | Reopened, but activity continued, here or on a further page | No way to reopen the choice | **Not assessed in a Quick Scan** (Deep only), and when the test stopped short. N/A with P1 |
| P6 notice reachable | Found, loads, linked from every page read, enough text | Not linked from every page read, or under 1,500 characters | No notice found | Not assessed if the notice page could not be read |
| P7 data and purposes | Data itemised and purposes stated | One of the two (partly) | Neither | Not assessed if the notice could not be read. Fail, blocked by P6, if there is no notice |
| P8 contact and grievance | A business contact for privacy questions, and a grievance route with a response period | Only part (named in the finding) | Neither | As P7 |
| P9 rights and routes | Rights listed, with routes to exercise them, to withdraw consent, to complain to the Board, and nomination | Rights listed, a route or the nomination right missing | No rights, no way to exercise them | As P7 |
| P10 forms and security | Every personal-data form has a consent control or notice link, none pre-ticked, pages and forms served over HTTPS, no insecure content on form pages | Some forms lack consent or a link, a pre-ticked box, an http page, a cross-organisation destination, or insecure content on a form page | A form submits over http, or no form has consent or a notice link | **N/A when there is no form that collects personal details** |

Where a row depends on an artefact the site lacks (no banner but tracking observed; no notice), the dependent rows are **fail, blocked by P1 or P6**, and the finding for P1 or P6 names them as its consequences (Scoring Mechanism SC-05, SC-16; Scope L-05). One missing banner therefore costs P1 to P5, which is the workbook's own rubric read literally. Say if you want a different treatment.

## Decisions taken (go-ahead of 5 Nov 2026) that need your sign-off
1. **Weights** are the workbook's, which the workbook calls "a proposed starting point (not set by law)". Cookie Consent carries 48 marks although the Act does not name cookies; it applies through consent (s.6). Changing weights is a change to the scoring rules (GR-24: named legal sign-off) and starts a new rules version.
2. **Band names** are neutral ("Substantially addressed" and so on) instead of "Ready" and "At risk", which read as a verdict or a certificate (GR-03). Thresholds are the workbook's 90 / 75 / 50.
3. **No hard-fail cap.** The workbook's optional rule (cap at "At risk" if P3 or P4 fails) is not applied: one observation, possibly from a single page load at low confidence, would override the score, and the note calls it "the most visible violation" (banned wording, GR-02). A failed P3 or P4 is the first finding, worded as a finding, "worth investigating" if confidence is low (L-07).
4. **P8 follows the law, not a name.** Act s.8(9) and Rules r.9 ask for business contact information of a Data Protection Officer or a person who can answer; r.14(3) asks for a published grievance response period of at most 90 days. A generic business email is therefore a pass for the contact. The workbook's "named contact" and "generic info@ is partial" are not applied.
5. **P5 judges behaviour only.** What the notice says about withdrawing consent is P9. The workbook's sample counted it in both.
6. **Not applicable for a site that tracks nothing** (P1, P2, P4, P5): it needs no banner, so it is not failed for lacking one (SC-46 / the Scanner Score's B1 rule).

## What the scorecard asked for that the scanner now records
Per-page banner presence (`banner_by_page`, P1.2); browser storage key **names** only (`storage_signals`, P3.3; QS-18, GR-16); insecure content on pages that collect details (`mixed_content`, P10.4); Accept and Reject measured side by side (`button_comparison`, P2.2: size, text size, filled or bare link; rules in `reject_and_compare.judge`); Reject on two further pages in the same session (`reject_persistence`, P4.3, Deep Scan only).
Not built: **P4.4** (pre-existing trackers cleared after Reject). It needs Reject in the same session after trackers fired, which the Scope forbids presenting as the Reject test (QS-20); it is shown as "not recorded". **P2.3** (toggles default OFF) is recorded only as "category choices offered".

## Honest limits
- Partial rules that need judgement ("vague purposes", "hard to find") are not scored: partial credit follows only rules code can test.
- A scan made before the new evidence existed shows those sub-checks as "not recorded", and they never lower a row.
- Requests and storage key names are observed; cookies after withdrawal are not classified (the scanner does not hold the lookup table).
- The legal references of every row come from `scoring/legal_map.py` (each checked against the PDF). They have not had the named legal review GR-24 requires.
