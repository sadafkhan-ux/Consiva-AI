# Scanner Score v2.1.4 (Scoring Mechanism v1.0, Annexure A)

Code: `backend/app/scoring/domain_score.py` (`assess_scanner`). Tests: `backend/tests/test_domain_score.py`.

**Status (5 Nov 2026): wired in, unit-tested, not yet verified on live scans.** `GET /api/v1/consent/scans/{id}/assessment` (same path, same response model) now serves this engine. The dashboard,
the web report and every PDF read that one response. Backend suites and the frontend suite (53 files) pass; a docker rebuild and a check against stored real scans are still to do. Plan and gaps:
`docs/SCANNER_FUNCTIONAL_SCOPE_GAP_ANALYSIS.md`.

## How it is served, and rollback
- Same response fields as before (`checks`, `counts`, `score`, `score_text`, `total_text`, `summary`, `recommendations`, `pages`, `limitations`), now filled from the parameters, plus `engine` and `scanner` (the whole result: band, ceiling, coverage, evidence confidence, parameters, findings, removed, what a Quick Scan cannot reach). `methodology_version` is `scanner-2.1.4`.
- A scan on the authorised application profile is a Deep Scan; everything else is a Quick Scan (`assessment_service.scan_mode`).
- **Rollback:** set `SCORING_ENGINE=ten_check` and restart the backend. The ten-check engine (`ten_checks.py`, its golden tests and its fixtures) is untouched. The setting also keys the report/PDF cache (`assessment_service.active_version`), so a switch never serves a PDF written by the other engine.
- If the validated-result gate refuses a result, the endpoint returns a plain error quoting the scan reference and logs the refusal; it never serves a weaker result.

## What it does
- 24 parameters in 7 domains, 100 points (A 26, B 28, C 20, D 6, E 10, F 6, G 4). A Quick Scan can assess 70 of them; the other 30 are listed in the result (`beyond_quick_scan`) as not assessed in a Quick Scan.
- Each parameter is in one state: earned, partial, not met, prerequisite missing (costs its points, linked to the blocking parameter), not assessable (removed: our limit) or not applicable (removed: objective test).
- **Every score is out of 100, for Quick and Deep alike (client decision, 5 Nov 2026):** score = points awarded / points in play x 100, a whole number. No ceiling and no "x / 70" is shown anywhere; the points a Quick Scan cannot assess are left out and redistributed like any other unassessable parameter. A Quick Scan is held at 99 (100 needs a Deep Scan at full coverage and high confidence). This departs from SC-11, SC-12 and SC-21 of the Scoring Mechanism (Quick ceiling of 70); `SCALE` and `QUICK_MAX` in `domain_score.py` are the only two constants to change to revert. Partial credit is only what `PARTIAL` defines, in whole numbers. No decimal appears in any result.
- Band from the score: Evidence-led 85+, Substantially addressed 70+, Developing 50+, Needs attention 25+, Early stage. Under 60% of reachable points assessed gives Insufficient evidence; low evidence confidence lowers the band one level. 100 only from a Deep Scan at full coverage and high confidence, else 99.
- Every parameter carries an outcome from the Scope 6.2 list. Findings carry severity and confidence separately; a High-severity finding at Low confidence is worded "Worth investigating".
- Fixed phrase library (`PHRASES`); no free-text generation and no language model.
- Validated-result gate (`validate_result`): refuses a result with an asserting finding that has no evidence, a score on a non-asserting outcome, a prerequisite block with no named blocker, an outcome outside the list, prohibited wording, or a Quick score above 70. The API must treat `ResultRefused` as a loud internal error, never serve a weaker result.

## Reading of the worked example (section 12)
No banner, no tracking, no notice, no forms, HTTPS: 16 points earned of 57 in play, score 28/100, band Needs attention (the document's illustration of 20 used the 70-point ceiling). Asserted by `test_worked_example_...`.

## The Deep Scan parameters and where their evidence comes from (5 Nov 2026)
All 24 parameters can now be assessed. The crawler records the evidence; a scan made before it existed has none, and the parameter is then "not assessable", never failed.

| Parameter | Evidence | Recorded by |
|---|---|---|
| A5 notice linked from every page, offered in an Indian language | Per page: does it link to a privacy notice. Language: scripts, switcher and hreflang the pages show, and the notice's own statement | `scanner/page_signals.py` (`notice_link_signals`, `language_signals`) |
| B5 consent can be withdrawn, B6 withdrawal persists | A fresh session: Accept, reopen the choice (a consent tool's reopen control or a "cookie settings" control), withdraw, watch requests; then up to two further pages in the same session | `scanner/withdrawal.py` (Deep Scan only), stored in the consent signal's evidence under `withdrawal` |
| D1, D2 children | A page that declares itself to be for children (address, title or text; policy and legal pages excluded). With none, the domain is not applicable and its points are redistributed | `page_signals.child_directed_signal` (`child_surfaces`) |
| E2 cross-border | The organisation behind each recognised service and where it is established, then the notice's `cross_border` statement | `rules/vendor_orgs.py` (own research, provenance stated, nothing marked verified) |
| G1 notice link beside a form | Whether the form or the element holding it links to a notice | `page_parser.form_has_notice_link` (`form_signals`) |
| G2 pre-selected consent | The live `checked` state of each checkbox and radio button as the page loads (a script-built form has no `checked` attribute) | `crawler._FORM_CHECKED_JS`, `FormField.checked` |

Honest limits of these tests
- Withdrawal is judged from requests only. Cookies are not read, because their classification needs the lookup table the scanner does not hold.
- A reopen control the scan cannot recognise (an unusual icon with no words, or one reached only by scrolling) is reported as "no option to reopen found" for B5 and "not tested" for B6, with that reason in the site owner's words.
- Where an organisation is established is its registered country from public details, not where data is stored. A vendor not in the table is "not identified", never assumed to be in India.
- The child-page test is narrow on purpose and looks only at what a page says about itself.
- No migration was needed: everything is stored in existing JSON columns and the website-scan stage record.

## Where the two documents disagree: decisions taken (5 Nov 2026, to be signed off)
1. Prerequisite-blocked parameters: the Scope (L-05) says they do not count; the Scoring Mechanism (SC-05, SC-16) says they cost their points. **Decision: the score follows the Scoring Mechanism; the wording follows the Scope** (outcome "Blocked by a prerequisite", linked to the blocking finding, one finding with its consequences).
2. Bands: the Scope lists four; the Scoring Mechanism lists five plus insufficient evidence. **Decision: the five bands of the Scoring Mechanism plus the insufficient-evidence override.** The band always comes from the server.
3. Quick Scan result: the Scope says a band, not a number; the Scoring Mechanism expresses every report out of 100. **Decision: the result carries both; the band, ceiling, coverage and evidence confidence are always printed with the score** (SC-25), and the calculation is on the page behind "How the score was reached".
4. Domain D's objective test is now implemented as above (a page that declares itself to be for children); Domain G's is "no form that collects personal details". Both still need sign-off (Scoring section 14).

## Legal check and readiness indicators (5 Nov 2026)
Every parameter was checked against the text of the Act and the Rules: `docs/DPDP_PARAMETER_LEGAL_VERIFICATION.md` (generated from `scoring/legal_map.py`, with a test that finds a phrase from each provision in the official PDFs). That check corrected A4 (the Act and Rules ask for a complaint route **to the Board**, not only the fiduciary's grievance contact) and the basis of A5 (s.5(3)).
Eight readiness indicators (retention, security, breach handling, cookies, grounds for processing, 90-day grievance period, complaint to the Board, home-page security headers) are shown with evidence and the provisions they relate to. They carry no marks and never change the score, because what is scored is fixed by the Scoring Mechanism and a change needs legal sign-off (GR-24).

## Choices by purpose (Functional Scope DS-12; readiness indicator I9), 5 Nov 2026
Deep Scan only (`scanner/purposes.py`). Where the preferences dialog offers choices by purpose, each optional purpose is switched on alone in its own fresh browser session (every other optional purpose off), saved, and the analytics or advertising services then contacted are recorded. Switching on analytics should contact analytics and no advertising; marketing should contact advertising and no analytics; a functional purpose should contact neither. The verdict follows the evidence only: "as expected" or "unexpected".
It is an indicator, not a scored parameter, because the Scoring Mechanism has no parameter for it and adding one needs legal sign-off (GR-24). It is anchored on Act s.6(1) ("free, specific, informed..."), checked against the PDF by `tests/test_legal_map.py`.
Limits: requests only (cookies are not read); a preferences control the scan cannot recognise is "not tested" with the reason in plain words (no preferences control found, no choice by purpose, no save control); up to four purposes are tested; the switch labels decide each purpose's kind and an unrecognised label is "other" (expected to contact neither).

## Rules version history (a change that can move a result raises the version, which also keys the report and PDF cache)
- 2.0.0 (5 Nov 2026): the seven-domain model, bands, the validated-result gate.
- 2.1.0 (5 Nov 2026): the Deep Scan parameters A5, B5, B6, D1, D2, E2, G1 (notice link) and G2 are assessed; A4 asks for the complaint route to the Board (Act s.5(1)(iii), Rules r.3(c)(iii)); C1 follows s.8(9) and r.9 (business contact information, not a name or a title); eight readiness indicators and per-provision references added; a Quick Scan lists every parameter, marking the 11 it cannot reach "Deep Scan only". Scans scored under 2.0.0 read differently under 2.1.0 for those parameters, and the comparison must say so (SC-30).
- 2.1.1 (5 Nov 2026): wording only (the Deep Scan only rows are said once in the table row and the band note); no result changes.
- 2.1.2 (5 Nov 2026): wording only; no result changes.
- 2.1.3 (5 Nov 2026): wording only; no result changes.
- 2.1.4 (5 Nov 2026): check numbers run without gaps (reachable parameters first); parameters beyond a Quick Scan are counted separately from "could not be assessed"; no score changes.

## The scorecard is now the default engine (5 Nov 2026)
`SCORING_ENGINE` now defaults to `scorecard`: the ten-row DPDP Website Readiness Scorecard (`docs/SCORECARD.md`) over the 24 checks described above, which stay as its detail. `scanner` (this document's engine alone) and `ten_check` remain available for rollback. The report cache is keyed by `scorecard-3.0.0`.
