# Requirement traceability: Functional Scope v1.0 and Scoring Mechanism v1.0 against the code (5 Nov 2026)

Honest status after the Scanner Score integration. **Done** = in code and tested. **Partial** = some of it, or only in the engine and not yet in the screens/PDFs. **Not done** = absent. **Existing** = already in the product before this work; not re-verified against the wording here.
The Benchmark Field Report was read for headings only and used for nothing in the build (it is market research, per the instruction).

## Scoring Mechanism (SC)
| IDs | Status | Note |
|---|---|---|
| SC-01..06 | Done | Fixed 24 parameters, out of 100, positive evidence only, our limits removed, the website's gaps cost points, pure function with no model. |
| SC-07 | Partial | The rules version is in every result, but a result is recomputed on read. An issued report is not frozen, so reopening it later after a rules change would differ. |
| SC-08 | Done | Whole numbers in the new engine. The ten-check rollback still prints halves and one decimal. |
| SC-09 | Partial | The prohibited-wording gate blocks "certified" and trust-mark wording. Nothing yet stops a screenshot of the ring being used as a badge. |
| SC-10 | Done in code | Weights are fixed in `PARAMETERS`; the release note and reference-set check are a process, not built. |
| SC-11..13 | Changed by client decision | Scores are out of 100 for Quick and Deep, with no ceiling shown (SC-11 and the 70 ceiling of SC-21 are not followed). Quick is held at 99 (SC-12). The 30 unreachable parameters are still listed. |
| SC-14..16 | Done | Redistribution, disclosed removals, "prerequisite missing" kept apart from "not assessable". |
| SC-17, 18 | Done | Partial points defined in `PARTIAL`; each result says what was found. |
| SC-19 | Partial | Per-parameter low-confidence flag is not set; confidence is held at report level only. |
| SC-21..24 | Done | Band against the ceiling, Deep against 100, under-60% override, low confidence lowers the band. |
| SC-25 | Partial | Score, ceiling, band and confidence print together in the hero, panel and PDF text. The ring graphic alone, and any shared image of it, does not carry them. |
| SC-26, 27, 31 | Done | Same parameters and weights; the same inputs give the same score (tested). |
| SC-28..30 | Not done | No Quick-to-Deep comparison, no parameter-by-parameter movement, no "rules changed" comparison. |
| SC-32..36 | Partial | Engine caps Quick remedies at 3, offers them only for evidenced gaps, never names a capability until it is confirmed live. They are not yet shown in the dashboard or PDFs. |
| SC-37 | Done | 100 only from a Deep Scan at full coverage and high confidence. |
| SC-38..41 | Partial | The perfect-score notice and cadence are generated; they are not yet rendered. Deep cannot reach 100 anyway until the missing capabilities exist. |
| SC-42, 43 | Not done | Needs scan history. |
| SC-44..46, 48 | Done | Behaviour-based consent points, content-based notice points, no exemption for an absence that is the gap, no manual override. |
| SC-47 | Not done | Detecting a site that treats our scanner differently. |
| SC-49..52 | Partial | Disclaimer and "not assessed" lines are in the panel; not on every score surface. |

## Functional Scope, assessment logic and evidence
| IDs | Status | Note |
|---|---|---|
| L-01 | Partial | Wording is from a fixed library; checks do not yet declare a deepest permitted level. |
| L-02..07 | Done | Only asserting outcomes count, not tested is never a failure, not applicable differs from not tested, blocked items are linked to the blocker, severity and confidence separate, high severity at low confidence reads as "worth investigating". |
| L-08..14 | Done | Bands, insufficient-evidence rule, band shown with coverage and confidence, explanation before the result, whole numbers, exclusions disclosed. |
| L-15 | Partial | Versions are recorded for scoring rules and mapping; check list and classification versions are placeholders. |
| L-16..18 | Not done | Immutable issued reports, re-scan as a new report, change notes and reference-site check. |
| EV-01, 02 | Done in engine | The gate refuses an asserting finding without evidence. |
| EV-03 | Partial | "Not found" carries what and where was searched; patterns and candidate counts are not itemised. |
| EV-04 | Not done | Evidence retention and expiry. |
| EV-05 | Done | Evidence traceability is no longer a scored check. It is not yet shown as a separate assurance statement. |
| EV-06, 07 | Partial | Findings use fixed phrases; the contradiction check covers the consent wording only. |

## Functional Scope, Quick Scan, Deep Scan, report and operations
| Area | Status | Note |
|---|---|---|
| QS-01..05 (anonymous start, validation, notice, reuse) | Not done | Scans still require login. Input validation and the private-address block are **Existing**. |
| QS-06..12 (discovery, page choice, reasons) | Partial, Existing | Robots, sitemaps, link discovery and a stored selection reason exist. One page per category is not enforced. |
| QS-13..18 (detection, forms, storage names) | Partial, Existing | Browser storage names and request-parameter scrubbing are not built. |
| QS-19..23 (three consent states) | Partial, Existing | Fresh session per state exists. N/A versus not tested is now correct in the score. |
| QS-24..30 (honest progress, first line in 15 s, cancel) | Not done | |
| DS-01..09 (ownership, scope, rendering) | Partial | DNS ownership exists, optional. No file or tag method, no entitlements. |
| DS-10..15 (four consent states, withdrawal, per-purpose) | Not done | Scanner capability missing. Those parameters are marked not assessable. |
| DS-16..21 | Partial | Inventory and form inspection exist; vendor organisation and non-India flag do not. |
| DS-22..26 (compare, schedule, alerts, status workflow, exports) | Partial, Existing | Compare, schedule, alerts and actions exist. Regressions-first, the rules-version note and CSV export are not built. |
| RP-01..22 (the two-page report, colours, share link) | Not done | I changed score and band wording and headings only. |
| CI-01..10 (cookie intelligence) | Not done | |
| CV-01..10 (conversion and entitlement) | Not done | |
| GR-01..08 | Partial | Prohibited wording is enforced inside the new engine and its tests. It is not a release gate over the LLM-written findings elsewhere. |
| GR-09..17 | Partial, Existing | No form submission, private-address block, honest agent name. The user-agent has no information-page link. |
| GR-18..22 | Done in engine | |
| GR-23, 24, 25 | Partial | No model in the score. No legal sign-off workflow and no per-source rate flag. |
| EX-01..16 (timeouts, restricted, error reference) | Partial | Restricted marking and a refusal with scan reference exist; the timeout, partial and 60-second rules do not. |
| IN-01..15, PF-01..30 | Not done | Capacity split, 60 s budget, cost view, metrics, API and webhook polish, plugins. |
| Section 15 quality gates | Not done | No reference-site set, no form-submission canary, no tenant-isolation test written for this work. |

## Summary
Scoring Mechanism: core engine and display done; comparisons, perfect-score rendering, remedies display and frozen reports not done. Functional Scope: assessment logic mostly done; Quick/Deep behaviour, the report redesign, cookie intelligence, conversion, integrations and performance work are not done.

## Deep Scan and edge-case audit (5 Nov 2026, run on the engine and on the five stored scans)
Method: seven Deep and Quick scenarios (clean site, no banner with analytics before consent, banner with Reject untested, no notice, notice blocked by robots, nothing scanned, banner never shown), read parameter by parameter, then the five stored scans through the rebuilt stack and the three PDFs.

Defects found and fixed in this pass
1. A restricted Deep scan with only 16 assessable points showed 99/100 beside "Insufficient evidence". The score is now withheld whenever fewer than 60% of the reachable points could be assessed (Scope L-09, L-12: a number is justified by high coverage). The figure is kept as `indicative_score` for internal use only.
2. A consent tool whose banner was never shown to the scanner was scored as "no consent mechanism found" and cost points. It is now Inconclusive and removed from the score (SC-04). The bbc scan moved from 64 to 83 for this reason.
3. The explanation did not say how much of the assessment applied (L-12). It now says "N of M parameters this scan type assesses applied".
4. Reject honoured (B4) did not state that Reject was tested once, on the home page.

Deep Scan behaviour confirmed by test: same formula and scale as Quick; reachable points 100; the parameters the scanner cannot yet test (A5, B5, B6, D1, D2, E2, G2) are removed with a stated reason and keep the score at or below 99; a score of 100 is impossible until they are built.

Still not implemented for Deep (unchanged): fourth consent state and two further pages (DS-10, 11), per-purpose testing (DS-12), organisations and non-India flag (DS-17), verified ownership by file or tag (DS-02), entitlements (DS-01), regressions-first comparison and rules-changed note (DS-22, 23), finding status verified by the next scan (DS-25).

Benchmark Field Report (read in full this time, used as market context only): its "refuse" list (legal conclusions, badges, fear hooks, single-page confident scores, internal failures on customer surfaces, database-size claims, Consent Manager implication) is covered by the wording gate, the disclaimers and the restricted/insufficient-evidence rules. Its "adopt" list is mostly not built: live progress feed, ungated PDF and share, effort estimates (the findings carry an effort field but it is not shown), glossed citations, domain-personalised upgrade headline. It also recommends keeping the number for Deep Scan only; the client decision of 5 Nov (every score out of 100) overrides that for Quick.

## Update: the seven parameters built (5 Nov 2026)
A5, B5, B6, D1, D2, E2 and G2 are now assessed from recorded evidence, and G1 now counts a link to the notice beside the form (SC rows for those parameters; Scope DS-10 to DS-12 and DS-17 partly). Withdrawal (DS-10, DS-11, DS-15) is tested on Deep Scans only. Still not built: per-purpose consent testing (DS-12), the identity of the organisation behind every unlisted service, verified ownership by file or tag, entitlements, frozen issued reports, and the Quick Scan flow items. Tests: backend 4,752 passed, 48 skipped (the two live-LLM files excluded); the withdrawal test also runs against a real headless browser on an offline site.

## Update: per-purpose consent testing built (DS-12)
Choices by purpose are tested on Deep Scans (`scanner/purposes.py`, indicator I9, shown in the panel under "Choices by purpose"). Each purpose is switched on alone in a fresh session and what it contacts is judged against its name. Tested in a real headless browser on an offline fake consent tool: purposes independent, a leaky site detected, no preferences control reported with its reason.

## Update: one page per kind, true page reasons (QS-08 to QS-11)
Built: `category_of`, one page of each of the five named kinds on a public scan, a freed kind when its page fails, `category_already_covered` skips, and reasons that say a page collects visitor details only when a form was found. "Remaining" pages fill leftover budget (a departure from one remaining page, stated in docs/PAGE_SELECTION.md). Tested on a fake site (tests/test_page_categories.py).

## Update: the DPDP Website Readiness Scorecard (workbook of 5 Nov 2026)
Built as the default engine (`docs/SCORECARD.md`): ten rows, the workbook's weights, a five-outcome rule (not assessed is never failed), neutral band names, no hard-fail cap, P8 and P5 corrected as the workbook's concerns list says. New evidence recorded: banner per page, storage key names, insecure content on form pages, Accept and Reject measured, Reject on further pages. Not built: P4.4. Needs legal sign-off on the weights and the legal references (GR-24). Verified on all 32 completed stored scans (no error) and on the PDFs of five.
