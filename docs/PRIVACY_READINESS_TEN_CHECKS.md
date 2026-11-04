# Privacy Readiness Score and the ten checks

## One score, one engine (2026-11-04)
The **Privacy Readiness Score is the ten-check score, worked out once, on the server**: `GET /api/v1/consent/scans/{id}/assessment`, field `score` (a number, one decimal) and `score_text` (the same, written `N/100`, for example `94.4/100`). Passed = 1, Partially Passed = 0.5, Failed = 0; Not Applicable and Not Assessed checks are left out of the denominator; score = marks awarded / marks available x 100, rounded half up. The web page, the email, the dashboard, the Summary PDF, the Free PDF, the Full PDF and the long report all print that one number. There is no scoring code in the browser: the front end only draws what `/assessment` returns, and when it cannot load it shows "No score", never a number it worked out.

The engine is `backend/app/scoring/ten_checks.py` (a pure function of the stored evidence and the policy analysis), its inputs are assembled by `backend/app/scoring/ten_inputs.py` and `backend/app/services/assessment_service.py`. It was ported from the browser engine it replaces and is held to it by recorded results: `tests/test_ten_checks_parity.py` (160 cases) and `tests/test_ten_inputs_parity.py` (102) compare the Python output with the browser engine's recorded output exactly, marks, texts and all.

The response also carries: `final` (true only when the scan is complete and the policy reading has finished or failed; a score read before that can still change), `policy_analysis` (done / running / failed / not_run), `counts`, `incomplete`, `limited_basis` (fewer than half the checks could be assessed), the ten checks with evidence and source, `summary`, `recommendations` and `methodology_version` (`TEN_CHECK_VERSION`, now 1.1.1; raise it whenever a rule that can change a result changes).

The weighted score is **removed**: `GET .../dpdp-score` and `.../dpdp-score/compare` no longer exist (404), and no weight, point total, weighted percentage or coverage percentage is computed or shown anywhere. The 16 DPDP requirement checks remain as a statuses-and-evidence reading with no numbers, at `GET .../requirements` (see `DPDP_REQUIREMENTS_READING.md`). Any other system (the UAT web page, the email) that shows a Privacy Readiness Score must read `/assessment`.

Before this change three scores existed for one scan (for example 80.8 on the web page, 95 in the PDF, 75 on the dashboard): two engines (a server weighted one, a browser ten-check one) and a timing difference (policy reading not finished when the browser scored). One engine on the server removes the first; `final` and the dashboard's "Assessing" state handle the second. The score is not stored: like the weighted score it is worked out on request from the evidence and the policy analysis as they are now, so it always matches them.

Verified: for five stored scans (swaransoft 95, cookieyes 90, wipro 95, bbc 85, icicibank 62.5) the Summary, Free and Full PDFs printed through the real server pipeline each print exactly the `/assessment` score (15 of 15). Not verified: the live UAT site and the email (not ours to run).

## Observed collection against the policy (engine 1.1.0)
Checks 2 to 5 also compare what the scan saw the site collecting (forms, analytics and advertising trackers and cookies, persistent cookie lifetimes) with the verified policy passages (`backend/app/scoring/disclosure.py`). A policy that states a purpose only for form submissions on a site running Google Analytics or an advertising pixel no longer passes check 3 in full: the result is Partially Passed and names the observed item. The same comparison is made for the data described (2), the recipients (4) and the retention of persistent cookies (5). Check 7 lists which parts of a DPO or grievance contact were found and which were not detected; no mark depends on that list. Full criteria and legal basis: `docs/PRIVACY_CHECKPOINT_LEGAL_REVIEW.md`.

## How the ten checks are marked

### Marks

Passed = 1, Partially Passed = 0.5, Failed = 0. Not Applicable and Not Assessed carry no marks and are left out of the maximum.
The engine also works out marks awarded ÷ applicable marks × 100 (rounded half up, one decimal; "No meaningful score" when nothing applies). That figure is internal: it is not printed on any surface and it is not the Privacy Readiness Score.

## The five results

| Result | Meaning | Marks |
|---|---|---|
| Passed | The check ran and the evidence meets the pass condition. | 1 / 1 |
| Partially Passed | Evidence exists but covers only part of the requirement, or its quality is limited. | 0.5 / 1 |
| Failed | The check ran to the end and the disclosure or behaviour was not found or was seen to fail. A limit of the scan, not a finding that the law was broken. | 0 / 1 |
| Not Assessed | The check could not be run: no page loaded, the policy page was blocked or did not load, its text could not be read, the analysis did not run, or the requirement was not found in a privacy or cookie policy page whose text was cut (what came after the cut was not read). | none, left out of the maximum |
| Not Applicable | The check does not apply (only check 9, with no banner and nothing that tracks). | none, left out of the maximum |

Change recorded 2026-11-03: before this, a check that could not be run scored 0 and stayed in the maximum. A page that could not be reached is not a failed check, so it is now Not Assessed. If fewer than five checks can be assessed the score is marked provisional (`limitedBasis`).

## Per check

| # | Check | Evidence and where it comes from | Passed | Partially Passed | Failed | Not Assessed |
|---|---|---|---|---|---|---|
| 1 | Privacy Policy Availability | Pages the crawler read; policy detector result | Policy page found and its text read | Found, text unreadable or analysis not run | No policy page after searching the scanned pages | Site not reachable, or the policy page blocked or did not load |
| 2 | Personal Data Collection | Policy analysis, quote verified word for word against the policy text | Verified quote naming what is collected | Analysis says partial | Not found in the text that was read | Policy text unavailable |
| 3 | Purpose of Processing | as 2 | as 2 | as 2 | as 2 | as 2 |
| 4 | Data Sharing and Third Parties | as 2 | Verified quote that names at least one kind of recipient | Verified, but names only "service providers" or "third parties"; or analysis says partial | as 2 | as 2 |
| 5 | Data Retention | as 2 | Verified quote about personal data in general | Verified, but a period for one kind of data only (for example form submissions); or analysis says partial | as 2 | as 2 |
| 6 | User Rights | as 2, three rights and the means | The means of making a request and at least two of three rights named | One right, or only the means | None found | as 2 |
| 7 | Contact and Grievance | as 2 | Grievance contact plus a response period or a business or DPO contact | Only part | None found | as 2 |
| 8 | Cookie and Tracker Detection | The scan's own cookies and requests | Detection ran on every scanned page | A scanned page did not load | n/a | No page loaded, or evidence not loaded |
| 9 | Consent Behaviour | Before, after Accept and after Reject, observed on the homepage | All three states tested, nothing analytics or marketing before consent or after Reject | Only one of Accept and Reject tested, or requests sent in consent-denied mode | Tracking before consent and after Reject, or tracking with no banner | Neither Accept nor Reject could be tested |
| 10 | Evidence and Transparency | Checks 1 to 9 | 80% or more of the positive findings carry an excerpt and a source page the scan read | 50% or more | Fewer | None of the others assessed |

Evidence quality is stated beside every result (Specific, General, Limited, None) together with what it covers ("One kind of data only", "Recipients not named", "Homepage only", "2 of 3 rights named"). The quality rules for checks 4, 5 and 6 read only the verified passage and apply to any website; none is specific to a site.

## Decisions to know about

- **Coverage is stated, not scored.** A Snapshot reads at most five pages by design. The score card, the "how to read this score" band and the limitations say "5 of 112 pages", and check 8 carries "Detection covers 5 of 112 pages". Coverage alone does not lower a mark. If a stricter rule is wanted, it is one line in `buildTenChecks`.
- **Consent behaviour is homepage only.** Check 9 can be Passed on one page of evidence. Its row says "Homepage only" and that it does not show every consent requirement is met across the site.
- **Check 10 is not circular.** It judges only checks 1 to 9, says so in its evidence, and counts a finding as traceable only when it has an excerpt and a source address that is one of the pages the scan read. A "not found" result records what was searched and is not counted as evidence.
- **Technical observations are not legal findings.** Checks 8 and 9 are tagged "Technical check", checks 4 and 5 "Best practice", and the report states that detection records what a site does and does not assess whether the policy discloses it.
- **Recommendations** come from the checks that did not pass; one line per check, never worded as a violation.

## Not done here

- The ten-check score is worked out on request and not stored; there is no database copy of it.
- Evidence per page now stores the final URL after redirects and the content type (migration 0046). Page title and HTTP status were already stored.
- The requirements section on the dashboard and the long Evidence report list requirement statuses and evidence with no numbers; the only score is the ten-check score.

## Same page, different score: what was fixed (2026-11-04)
Two runs of one site (digitalanumati.com) gave 95 and 70 from the same five pages. Checks 4 (sharing) and 5 (retention) were found in one run and "not found" in the other, and the second run said "the text was cut at the storage limit" about a policy page of 7,808 characters. Three causes, three fixes:
1. **Wrong page blamed.** The engine treated any cut document as the reason a requirement was missing, including a long course page the scan had filed as a policy. Only a cut privacy or cookie policy page (by its address) counts now.
2. **A cut policy scored 0.** A requirement not found in a policy page that really was cut is now Not Assessed (no marks, left out of the maximum), with the page and how much of it was read. It is never Failed. This applies to checks 2 to 7.
3. **No safety net for two statements.** The model sometimes misses the sharing or retention sentence and finds it on the next run of the same page. The fixed-pattern second look that already covered contact, rights and grievance now covers sharing and retention. It runs only when the model reported "not found", accepts only an exact sentence of the page that names a recipient (sharing) or a period or criterion (retention), searches the privacy and cookie policy pages first, and never replaces a passage the model found. The engine's quality rules (check 4 needs a named kind of recipient, check 5 must not be one kind of data only) still apply to what it finds.
Not fixed: the model's own run-to-run variation on other requirements. The pattern second look covers sharing, retention, contact, rights and grievance; the other requirements can still differ between two runs of the same page.

