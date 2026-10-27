# DPDP Compliance Score: methodology v1.5.0

**Status: a proposed methodology. It is not an officially prescribed legal formula, not a certification and not legal advice.**
It measures what a public website scan can observe, against the DPDP Act, 2023 and the DPDP Rules, 2025.

Code: `backend/app/scoring/dpdp_score.py` (the only place a score is calculated). Endpoint:
`GET /api/v1/consent/scans/{scan_id}/dpdp-score`. Dashboard: `DpdpScoreSection` (everything below: legal references, weights,
methodology, evidence records). PDF: a fixed **two-page** report with no legal wording (see "The PDF report"). Both render one view,
`frontend/src/report/dpdpScore.ts`, so the numbers cannot differ.
Tests: `backend/tests/test_dpdp_score.py`, `frontend/src/components/dpdpScore.test.tsx`, `frontend/src/report/dpdpScorePdf.test.ts`.

## Formula

```
Score (%)    = (total weighted points earned ÷ total weighted points assessed) × 100
Coverage (%) = (weight of assessed requirements ÷ weight of applicable requirements) × 100
```

Both are rounded half-up to one decimal place, after exact fraction arithmetic.

| Status | Points earned | In the score denominator? | In coverage numerator? |
|---|---|---|---|
| Met | 100% of the weight | yes | yes |
| Partially Met | 50% of the weight | yes | yes |
| Not Met | 0% | yes | yes |
| Unable to Verify | none | **no** (reported separately) | no, but it stays in the applicable weight |
| Not Applicable | none | no | no, and it is removed from the applicable weight |
| Not Applicable because another check carries the same gap (duplicate) | none | no | no, and it is removed from the applicable weight |

Missing or untested evidence is never Met. A requirement that was not observed is Unable to Verify, which cannot raise the score
and lowers coverage.

### When the score is withheld ("Insufficient Evidence")

A score is shown only when **at least 2 requirements** were assessed **and coverage is at least 30%**. Otherwise the page and the PDF
say "Insufficient Evidence", give the reason, and still show coverage. These thresholds belong to the methodology version.
Because five statutory requirements cannot be seen from a website, the highest coverage a website scan can reach is about 58% to 65%.
That is expected and is shown, not hidden.

## Two bases: readiness and in force

The Act's core duties (ss.3 to 17, and Rules 3 and 5 to 16) start **18 months** after the 13 Nov 2025 publication, i.e. **13 May 2027**.
The Consent Manager provisions (Rule 4) start after **one year**, on 13 Nov 2026 (G.S.R. 843(E) and Rule 1). The assessment date is `as_of`
(default today, UTC).

* **Readiness score (the headline).** Every applicable requirement, whether or not it is in force yet, labelled as readiness.
* **In force on the assessment date.** The same calculation over only the requirements already in force. Until 13 May 2027 this has
  nothing to assess and says so.

A finding is therefore never described as a present violation of a duty that has not started.

## Requirements, provisions and weights

Weight scale: **5** core consent duty a scan observes directly; **4** duty a scan observes directly that supports it; **3** statutory duty
a scan cannot observe; **2** supporting or entity-specific duty.

| ID | Category | Requirement | Provisions | Weight | Observed by |
|---|---|---|---|---|---|
| DPDP-C1 | Consent | No analytics or marketing processing before consent | Act s.6(1); Rules r.3 | 5 | pre-consent scan pass (R-001, R-008) |
| DPDP-C2 | Consent | A consent mechanism is provided where tracking is used | Act s.5(1), s.6(1) | 4 | banner or CMP detection (R-002, R-009) |
| DPDP-C3 | Consent | Refusing consent is honoured | Act s.6(1), s.6(4), s.6(6); Rules r.3(c)(i) | 4 | post-Reject pass, only if Reject was clicked and confirmed (R-003, R-011) |
| DPDP-C4 | Consent | Refusing is as easy as accepting | Act s.6(4); Rules r.3(c)(i) | 4 | reject-all option present (R-005) |
| DPDP-C5 | Consent | Choices are specific to each purpose | Act s.6(1); Rules r.3(b)(ii) | 2 | per-purpose choices present |
| DPDP-N1 | Notice | A privacy notice is published | Act s.5(1); Rules r.3(a), r.3(b) | 4 | privacy policy page found (R-006) |
| DPDP-N2 | Notice | Cookies and trackers are disclosed | Act s.5(1); Rules r.3(b)(i) | 2 | cookie policy page found (R-007) |
| DPDP-R9 | Data principal rights | DPO or responsible person contact published | Act s.8(9); Rules r.9 | 2 | not observable |
| DPDP-R14A | Data principal rights | Means to exercise rights published | Act s.11 to s.13; Rules r.14(1) | 3 | not observable |
| DPDP-R14B | Data principal rights | Grievance system, response within 90 days | Act s.8(10), s.13; Rules r.14(3) | 3 | not observable |
| DPDP-S6 | Security and breach | Reasonable security safeguards | Act s.8(5); Rules r.6 | 3 | not observable |
| DPDP-S7 | Security and breach | Breach intimation to the Board and individuals | Act s.8(6); Rules r.7 | 3 | not observable |
| DPDP-X4 | Entity-specific | Consent Manager registration and obligations | Act s.6(9); Rules r.4 | 2 | declared only |
| DPDP-X8 | Entity-specific | Erasure after the Third Schedule period | Act s.8(7); Rules r.8 | 2 | declared only |
| DPDP-X10 | Entity-specific | Verifiable parental or guardian consent | Act s.9; Rules r.10, r.11 | 3 | declared only |
| DPDP-X13 | Entity-specific | Significant Data Fiduciary duties | Act s.10; Rules r.13 | 3 | declared only |

Verify the Act's sub-section numbers against the statute before relying on them in a client document: the Rules' numbers were checked
against the gazette text, the Act's against the Act as cited elsewhere in this codebase.

Each result carries its provisions, weight, effective date, observations, the scan evidence ids and the related consent rule ids
(R-001 to R-012). The score and the findings read one shared context (`build_consent_context` in `consent_rules.py`), so they
cannot disagree about whether, for example, the banner was shown.

### Status rules (summary)

* **No pages scanned, or the scanned address only frames another site (R-012):** everything is Unable to Verify.
* **C1:** Not Met if a classified analytics or marketing item fired before consent; Partially Met if only unclassified items did
  (purpose unknown, a reviewer decides); Met if none; Unable to Verify if the scan predates three-state testing.
* **C2:** Not Applicable if no tracking was observed; Not Met with tracking and no banner; Unable to Verify if a CMP is loaded but its
  banner was not displayed to the scanner.
* **C3:** Met or Not Met only when Reject was clicked and the banner recorded it; otherwise Unable to Verify.
* **C4:** by the detected reject-all option. **C5:** Met with per-purpose choices; Partially Met with only an accept/reject choice;
  Not Met with neither.
* **N1:** by the privacy policy page. **N2:** Not Applicable without tracking; Met with a cookie policy; Partially Met with only a
  general privacy policy; Not Met with neither.

### Applicability and exclusions

Entity-specific requirements (X4, X8, X10, X13) are excluded **unless declared applicable** through the endpoint's
`consent_manager`, `erasure_class`, `processes_children_data` and `significant_data_fiduciary` parameters; a website cannot show
them. Declaring one adds it as Unable to Verify, so it lowers coverage but never moves the score. Requirements that depend on observed
tracking (C2, N2) or on a banner (C3 to C5) are Not Applicable when there is nothing to apply them to. Not assessed by this score at
all: Rule 5 and the Second Schedule (State processing), Rule 15 (transfers, only by order), Rule 16 (research exemption), Rules 17 to 23
(the Board and the Tribunal).

### Duplicate penalties

If two requirements in a group are both Not Met, only the heavier one keeps the penalty; the other is removed from numerator and
denominator and shown as "Counted under another requirement":

* `consent-before-tracking`: C1 and C2 (tracking with no banner is one gap);
* `choice-quality`: C4 and C5 (no reject option and no per-purpose choice is one gap);
* `notice`: N1 and N2 (no notice at all is one gap).

## Worked examples (each is a unit test)

**1. Clean site, 21 Oct 2026.** Banner with reject and per-purpose choices, privacy policy, no tracking.
C1 5/5, C3 4/4, C4 4/4, C5 2/2, N1 4/4 (C2 and N2 not applicable). Earned 19, assessed 19: **100.0%**.
Applicable = 19 + 14 (R9, R14A, R14B, S6, S7 unverifiable) = 33, so coverage = 19 ÷ 33 = **57.6%**. In force today: no score.

**2. Mixed site.** A tracker fires before consent; no reject-all and no per-purpose choice; privacy policy but no cookie policy.
C1 Not Met 0/5, C2 Met 4/4, C3 Met 4/4, C4 Not Met 0/4, C5 counted under C4, N1 Met 4/4, N2 Partially Met 1/2.
Earned 13, assessed 23: 13 ÷ 23 = **56.5%**. Applicable 37 (C1 to C4, N1, N2 = 23, plus 14), coverage 23 ÷ 37 = **62.2%**.
By category: Consent 8/17 = 47.1%; Notice 5/6 = 83.3%; Data principal rights and Security and breach: not assessed.

**3. Banner never shown to the scanner.** C2 to C5 are Unable to Verify; only C1 (5) and N1 (4) are assessed: 9 of 37 weighted points = 24.3%
coverage, below 30%, so the page says **Insufficient Evidence**.

**4. No pages scanned.** Nothing is assessed, coverage 0%, Insufficient Evidence. Nothing is Met.

**5. Same site as example 1 on 13 May 2027.** All requirements are in force: the in-force score equals the readiness score, 100.0%.

## The PDF report

The downloaded PDF is exactly **two A4 pages** and carries no legal references, effective dates, weights tables or methodology pages:

* **Page 1:** the Compliance Score and the coverage in a banner, a one-line brief, the formula in plain words with this scan's own
  numbers (`score = points earned ÷ points possible × 100`, `coverage = weight of the checks we could test ÷ weight of all checks that apply × 100`, met = full
  points, partly met = half, not met = none, untested checks earn nothing and lower the coverage), the scores by category, the status and
  group counts, and the scope (main domain, pages, consent states tested).
* **Page 2:** the fixes to do first, then the four finding groups, a few entries each (what was observed, the page, the consent state).
  What does not fit is left to the dashboard and the page says so; the fixes come first so they can never be pushed off the page.

The complete cookie inventory, the weights and legal-mapping tables and the per-check records are in the dashboard, not the PDF. To
return to the previous multi-page report, check out the git tag `before-two-page-report`.

## Scope, versions, remediation order and the caution (v1.5.0)

Every result states **what was assessed and when**: the entered main domain, the pages scanned against the limit, the scan time (UTC),
the date effective dates were judged against, and which consent states were actually tested (After Reject counts only when the banner
confirmed the click). It shows two versions: the **scoring methodology version** (formula, weights, status rules) and the **legal mapping
version** (`LEGAL_MAPPING_VERSION`: which provisions each check cites, their effective dates and applicability; pinned by a reviewed
table in `test_dpdp_assessment.py`, so changing a citation without bumping the version fails the tests). The legal sources are the
gazette notifications G.S.R. 846(E) (as corrected by 892(E)) and 843(E).

**Prioritised remediation.** The remediation of every failed check, in a fixed order that can be read off the result: duties in force
first ("Fix now"), then future duties ("Prepare before <date>"); within those, Not Met before Partially Met, then the heavier documented
weight, then the id. Each step gives the action, how to verify it, its legal references and why it has that priority.

**Caution.** A score that is shown but rests on fewer than 4 assessed requirements, or on coverage under 50%, carries "Treat this score
with caution". This was added after manual review found a 100% on two checks. It never changes the score.

Validation against the recorded scans and the manually reviewed examples are in `DPDP_ASSESSMENT_AUDIT.md`.

## Configurable weights, category weights and the methodology shown with every result (v1.4.0)

**Weights are configurable.** Every requirement has a documented default weight and rationale (table below). To change one, set
`DPDP_SCORE_WEIGHTS` to JSON in the environment, for example `DPDP_SCORE_WEIGHTS={"DPDP-C1": 6}`. Each weight must be a whole number
from 1 to 10 for a known requirement id; anything else (a typo, an unknown id, 0, 11, a decimal) stops the app from starting rather
than silently changing a score. A custom set is declared in every result: the methodology line reads "CUSTOM weights", the changed
weights are listed beside their defaults, and the dashboard and PDF warn that scores made with different weights are not comparable.
A configured weight never makes an unverifiable check count, and it does not change a finding's severity, which follows the
documented weight.

**Category weights.** A category has no weight of its own to set: its weight is the sum of the weights of its applicable
requirements, shown with its share of the total and a rationale (`CATEGORY_INFO`). The overall score is exactly the weight-averaged
score of the categories, so the two can never disagree.

**Coverage is its own figure.** The dashboard and the PDF banner show the DPDP Compliance Score and the assessment coverage side
by side, each labelled, and coverage is still shown when the score is withheld ("Insufficient Evidence", with the reason).

**Duplicates.** A requirement is scored once however many requests, cookies or pages relate to it. Where two requirements describe
the same underlying gap (`consent-before-tracking`, `choice-quality`, `notice`), only the heavier one is penalised; the other is Not
Applicable with the reason.

**Explained in both places.** Each result carries a "Methodology, weights and calculation" section (version, weights source, formulas,
points rules, reliability rule, duplicate rule, timing rule, scope, category weights, every requirement's weight and why, the duplicate
groups) and a "Why this result" explanation in numbers. The dashboard and the PDF render the same data.

## The four finding groups (v1.3.0)

Every result is classified from actual scan evidence, legal applicability and effective dates (`backend/app/scoring/findings.py`),
and the dashboard and the PDF render that one structure:

| Group | What lands here |
|---|---|
| **Confirmed Findings** | A check in a duty that is **in force** failed (Not Met or Partially Met) with sufficient evidence. |
| **Needs Human Review** | The evidence is ambiguous, or business context or legal interpretation is needed. Shown in the "Logic Required / Human Review" side box. |
| **Unable to Verify** | The scan lacks the evidence. Neither a pass nor a failure; says what is missing and how to verify it. Lowers coverage. |
| **Future Readiness** | The same failures, in duties that are **not yet in force**, with the date they start. Never a current violation. |

Today nothing is in force, so Confirmed Findings is empty until the duties apply (13 May 2027); the gaps appear under Future Readiness,
and the page lists each upcoming start date. Each finding shows: severity where justified, affected page(s), consent state,
what was observed and its evidence ids, the legal reference, business impact (operational, never a penalty), remediation, how to
verify, and why it is in its group. A network request or cookie is evidence that something was observed; it is never by itself
proof of a legal violation, and findings based on them carry that caveat.

**Severity** is shown only for a Confirmed Finding on a duty in force that rests on a direct observation (high confidence). It
follows the check's documented weight (5 High, 4 Medium, otherwise Low). Partly met, absence-based and single-page results, Future
Readiness, Needs Human Review and Unable to Verify are "not rated", and say why. It is not derived from the old risk levels.

**Limitations.** (1) The groups are derived from the requirement checks; the older per-finding rule and LLM findings (R-001 to R-012)
and their review workflow remain separate and keep their own severity labels. (2) A scan sees at most 5 pages of the main domain.
(3) "Business impact" and "remediation" are generic texts per check, not advice for a specific organisation. (4) Whether a gap is a
violation, and whether another ground than consent applies, are for a person to decide: they sit in Needs Human Review.
(5) The Act's sub-section numbers should be checked against the statute before client use.

## Replaces the overall risk level (v1.2.0)

The score is **not** derived from the old risk levels and does not convert them. The PDF's first-page banner that used to read
"Overall risk: HIGH" (the highest finding severity) now shows the DPDP Compliance Score, or "Insufficient Evidence", with its
coverage, in a neutral colour. The headline no longer states a "highest severity". Individual findings keep their own severity
for the review workflow; that is a per-finding label, not an overall score.

## Website checks and business checks

Every check is labelled **website** (a public scan can observe it: C1 to C5, N1, N2) or **business** (it needs internal documents,
business information, system integrations or legal review: R9, R14A, R14B, S6, S7 and the declared entity-specific X4, X8, X10, X13).
A business check is always Unable to Verify from a scan, is never counted as met, and lowers coverage. This scan makes no statement
about whether retention rules, a DPIA, grievance handling, breach response times or a record of processing activities (ROPA) exist
or are complete. A ROPA is not required by name under the DPDP Act or Rules, so it is not a scored check; where an organisation keeps
one it is supporting evidence for the checks above.

Each result also states **when it applies** (`APPLICABILITY_TEXT`), its **confidence** (high for a direct observation across several
pages; medium for a partial result, a conclusion that rests on something not being found, or a single page; none when nothing was
assessed) and its **scoring rationale** (the points earned out of the weight, and why the weight is what it is).

## Each check as a record (v1.1.0)

There are exactly five statuses: **Met, Partially Met, Not Met, Unable to Verify, Not Applicable.** A duplicate is shown as Not
Applicable with the reason "Counted under DPDP-C1". The score shows the count of each status.

Every check (`DETAILS` in `dpdp_score.py`) has: a unique ID, a description, its legal references, written assessment criteria
(what leads to each status), and the evidence expected. Every result also carries an **evidence record**: what was expected,
what the scan observed, the page URLs it came from (trackers by their page, cookies by their recorded sightings, banner checks by
the page the banner was tested on, absence checks by the pages checked), whether the evidence is sufficient and why, and the
limitation (it covers only the pages scanned; a website cannot observe organisational duties).

**Timing.** A check in a duty that is not yet in force is a **readiness gap**, not a current violation: it is listed under
"Readiness gaps: the duty is not yet in force" with "applies from 13 May 2027", and only moves to "Current gaps" once the duty applies.
The score formula is the same either way.

**Remediation.** Each Not Met or Partially Met check gives: what was observed, the requirement it relates to, why it matters (a
restatement of the provision, never an invented penalty), the recommended action, and how to verify the fix (normally a re-scan
that should show the specific change).

## Verified gaps, missing evidence and the "Logic Required / Human Review" box

After every scan the same result also says, in one place (`backend/app/scoring/review.py`, shown in the dashboard side box and the PDF):

* **Current gaps and readiness gaps:** requirements that are Not Met, split by whether the duty is in force, with what the scan observed, its pages and evidence ids, and the remediation.
* **Partially satisfied:** Partially Met requirements.
* **Missing evidence:** Unable to Verify requirements, split into "the scan could not observe it" and "needs organisational evidence".
* **Uncertain findings:** observations that depend on business context or legal interpretation. Each item says what the scan
  confirms, what is missing, what decision a person must make, what it needs (business context, legal interpretation, more evidence),
  and cites its provisions and evidence ids.

Technical signals are counted apart: **network requests**, **requests that match a known analytics or advertising service**
(execution not confirmed) and **cookies**. A request is not proof that a tracker ran, and neither a request nor a cookie is a
violation on its own. The scan is of the entered main domain only, up to 5 pages; subdomains and other websites are not crawled,
listed or reported. The box always states that a website scan cannot establish complete DPDP compliance.

## Changing the methodology

Any change to a weight, status rule, threshold or exclusion changes `METHODOLOGY_VERSION` (semantic versioning: weights or status rules
change the major or minor number) and this document, and the worked-example tests must be updated to the new numbers. Scores
from different versions must not be compared.

## Score ledger, check table and assessment appendix (added 2026-10-22)

Every number on the score card can now be traced to the individual checks, their weights, their evidence and their legal basis.
There is **one calculation**: the server (`backend/app/scoring/dpdp_score.py`). The dashboard and the PDF only format what it returns.

### Where each part lives

| Question | Code |
|---|---|
| Where the checks are defined | `REQUIREMENTS` (16 checks), `DETAILS` (definition, criteria, expected evidence), `LEGAL_BASIS_NOTES` (legal basis and how far a scan can speak to it) |
| How checks are selected and applicability decided | `Requirement.applies_if` with `Applicability` (entity-specific checks X4, X8, X10, X13 are excluded unless declared), and each check's own Not Applicable rule (`APPLICABILITY_TEXT`) |
| Where weights are set | `Requirement.weight` (documented defaults), `resolve_weights` (validated overrides from `DPDP_SCORE_WEIGHTS`) |
| How evidence is collected | the three-pass crawl (`scanner/crawler.py`), stored, then read by `scan_repository.get_scan_evidence_summary_concurrent` into `build_consent_context` |
| How a status is decided | the check functions (`_pre_consent_processing` and the others), which never return Met on missing evidence |
| How penalties are decided | `SCORED_FACTOR`: Met 1, Partially Met 0.5, Not Met 0. Penalty = weight − points earned (`_attach_contributions`) |
| How the aggregates are calculated | `_block` (score and coverage), `build_ledger` (labels and tallies, reading `_block`'s figures) |
| API | `GET /api/v1/consent/scans/{id}/dpdp-score`, field `ledger` and the per-check fields |
| Scorecard UI | `DpdpScoreSection.tsx`: `CheckTable`, `ScoreLedger`, `PenaltyRules`, directly under the score |
| PDF | `downloadReport.ts`: pages 1–2 summary, then the assessment appendix (page 3 onwards) |
| One view for both | `buildScoreView` in `report/dpdpScore.ts` |

### The ten concepts, and the ledger field for each

| Concept | Ledger field | What it counts |
|---|---|---|
| Total checks | `total_checks` | every check defined (16) |
| Applicable checks | `applicable_checks` | total minus Not Applicable |
| Total applicable weight | `applicable_weight` | sum of the weights of the applicable checks. The coverage denominator |
| Earned weight | `earned_weight` | Met full weight, Partially Met half, Not Met nothing. The score numerator |
| Failed checks | `failed_checks` | Not Met (partly met checks are `partial_checks`) |
| Cannot Verify checks | `unable_checks`, `unable_weight` | applicable, but a scan could not obtain the evidence. Not scored, never a violation |
| Not Applicable checks | `not_applicable_checks`, `not_applicable` (with reasons) | excluded under the applicability rule, never a pass |
| Coverage | metric `coverage` | assessed weight ÷ applicable weight × 100. **By weight, not by number of checks** |
| Compliance (readiness) score | metric `score` | earned weight ÷ assessed weight × 100 |
| Security score | none | there is no separate security score; the "Security and breach" category is Not assessed because a scan cannot observe it (`security_score_note`) |

Also in the ledger: `assessed_weight` (the score denominator and coverage numerator), `penalty_weight` (assessed minus earned),
`total_weight` (all checks), and a status table (`status_rules`) saying for each status whether it is in the numerator, the
denominator and the coverage, with the checks that carry it.

### Penalties, in the code's own terms

There is no separate penalty list. A check's penalty is the weight it did not earn. The multipliers 1, 0.5 and 0 are the share of
the weight a check **earns**, so 1 means met and no penalty, and 0 means not met and the full penalty. Unable to Verify and Not
Applicable carry no penalty: they are not scored. Unable to Verify lowers coverage (its weight stays in the applicable total) and
never changes the score. Not Applicable is removed from both the score and the coverage denominators and is never counted as a pass.

### Real data: scan 277f3f37 (gignaati.com, 21 Oct 2026)

| Figure | Value |
|---|---|
| Checks defined / applicable / assessed / cannot verify / not applicable | 16 / 12 / 5 / 7 / 4 |
| Total weight / applicable / assessed / earned / penalty / not verified | 49 / 39 / 19 / 18 / 1 / 20 |
| Readiness score | 18 ÷ 19 × 100 = 94.7% |
| Coverage | 19 ÷ 39 × 100 = 48.7% |
| Consent score, coverage | 13 ÷ 13 = 100%, 13 ÷ 19 = 68.4% |
| Notice score, coverage | 5 ÷ 6 = 83.3%, 6 ÷ 6 = 100% |

The assessed checks are C1, C2, C3, N1 (Met, 17 weight) and N2 (Partially Met, 1 of 2). No figure of 29 exists for this scan.
Validated on all 27 completed stored scans: counts and weights add up, the score and coverage metrics equal the score block, and
the per-check contributions add up to the score within rounding. Contributions are rounded to one decimal, so their sum can differ
from the score by a few tenths.

### Legal basis, and what is not claimed

Each check lists its provisions, whether the duty is in force on the assessment date (the core duties start 13 May 2027), why it
applies, the evidence expected and the limits of a scan. `LEGAL_BASIS_NOTES` separates a statutory duty tested by a scan signal
from Consiva's own reading of a duty: the cookie-policy check (DPDP-N2) is labelled as a reading, because the Act and Rules do not
name a cookie policy. Provision numbers were mapped from the Act and Rules and should be confirmed by counsel.

### Website Pages Scanned (PDF page 3)

The PDF lists the pages the scan visited, from the stored page records of the same scan run as the score (`website_pages`, returned
by `/evidence` with `http_status` and `discovered_via`), with a Scan Review built from the crawler's own counts (`frontend/src/report/scanPages.ts`).

* **How pages are chosen** (`scanner/crawler.py`, `scanner/url_normalize.py`): the entered address first; then links found on
  scanned pages and the sitemap when there is one, on the entered domain only, tried in this order: privacy/cookie, terms/legal,
  sign-up/login/checkout/contact, about/pricing/product, everything else, blog/news last; first found first within a group. No
  address is guessed. On the stored gignaati.com five-page scan the four extra pages (privacy, terms, contact, about) came from the sitemap.
* **Status** is read from the record: `seed`/`link`/`sitemap` loaded and are scanned; `failed` and `timeout` are attempts that did
  not load and are shown as Not scanned; a scan stored before the status was kept shows "Recorded; load result not stored", never "Scanned".
* **Order**: the homepage, then the pages that loaded in the order the scan recorded them (pages load concurrently, so this is not
  the order they were tried), then attempts that did not load.
* The section changes no score, weight, coverage or finding.

## Standardisation update (2026-10-26)

- **Framed wrapper (R-012).** When the scanned address only displays another site in a frame, the rule applies per requirement. What the scan did observe stands (violations and positive finds, with a "seen on the wrapper" note). A clean or absent result, which rests on something not being seen, becomes Unable to Verify. Only a scan with no pages at all is wholly unverifiable. The dashboard readiness review and the visual follow the same rule.
- **One-check scores are withheld.** The five-page review score (formerly "Readiness score") needs at least 2 assessed checks and 30% coverage, as the backend does, so it never shows 100 out of 100 from a single check.
- **Rounding and dates.** Percentages round half-up everywhere (the remediation plan used Python `round`, which rounds half to even). The default `as_of` date is today in India (UTC+5:30).
- **Score comparison.** `GET /api/v1/consent/scans/{id}/dpdp-score/compare?against=<scan_id>&as_of=<date>` re-assesses this scan and the previous completed scan of the same website with the same methodology and returns both scores, coverage, weights, every changed requirement (before/after status and evidence), a cause for each (scan coverage, website behaviour, scoring configuration, unexplained) and a sequential score attribution. It returns 409 when there is no earlier completed scan. A score produced by older scoring logic cannot be re-derived; the response says so.
- **Scan transparency section.** Every report carries pages (discovered, selected, scanned, failed, skipped with reasons), duration and stage timings, the three separate coverages (page, requirement, consent state), the consent states actually tested, and the reason a score is zero, unavailable or partial.
- **Classification.** Vendor and cookie matching is host- and path-aware, cookie names match exactly, wildcard prefixes need at least 4 characters, and generic names (`uid`, `token`, `_session_id`) need a matching domain.

## Why were points deducted: backend only (2026-10-26)

The scoring formula and the deduction explanation are **not** in the client-facing report, the dashboard, the score API or the in-browser PDF. They are kept where the Consent Agent runs.

`app/scoring/deductions.py` takes the readiness score apart (the score is `points earned ÷ points assessed × 100`, so it is exactly 100 minus what each check failed to earn): for every Not Met or Partially Met check, its weight, the points earned and lost, the percentage points it takes off, the running score before and after, the rule that gave the outcome and the evidence it cites (requests and cookies with vendor, category, consent state and page). Checks that cost nothing (Unable to Verify, Not Applicable, a duplicate gap counted once) are listed with the reason. The arithmetic is exact and read from the same results as the score; the scoring logic is unchanged.

`app/services/score_deduction_log_service.py` writes it at the end of each analysis run (the `audit_saved` node), best effort, so a failure never costs the run its audit trail:

- **Server log:** one readable block on logger `consiva.score_deductions`, e.g. `docker compose logs backend worker | grep -A40 "Score deductions for scan"`.
- **Database:** one `audit_logs` row, `action = 'scan.score_deductions'`, `entity_id` = the scan, the whole breakdown in `after.deductions`. The audit API (`GET /scans/{id}/audit`) returns an entry's action and ids only, never `after`, so the payload is read from the database: `SELECT after FROM audit_logs WHERE action = 'scan.score_deductions' AND entity_id = '<scan id>' ORDER BY created_at DESC LIMIT 1;`

`GET /scans/{id}/dpdp-score` does not return a `deductions` field.

## Report headers

Every page after the cover now uses the same dark header with white text, in the long report (print header), the Full and Free fixed sheets (`.pg-head-dark`), on the web and in the PDF. Covers keep their own light header.

## Why one website can score 100 in one scan and 80 in another (2026-10-26)

Investigated on a website that scored 100 in nine scans and 80.0 / 76.2 in two. The scoring is a pure function of the stored evidence: 14 real scans, each re-scored from 150 shuffled row orders, gave no difference, and `assess()` now sorts every evidence list into one canonical order (`canonical_evidence`) so row order can never matter. The scans differed in what they RECORDED: in the two lower scans the site's Google Tag Manager and Analytics requests and cookies were observed before consent (6-9 requests, 3 cookies); in the other nine they were not. That moves DPDP-C1 (weight 5) from Met to Not Met, which is exactly 20 points of the 25 assessed. Nothing is forced to 100 and nothing is hidden: a scan that sees a pre-consent request scores it.

What was ruled out by experiment: scrolling or time-on-page counting as consent (no), consent carried from one scan's Accept click to the next (no: five back-to-back scans, none), and machine load (no: four scans running alongside, none). Whether a site fires its tags early on an occasional visit is the site's behaviour, and one visit per page can miss it; re-scan to confirm either result. `GET /scans/{id}/dpdp-score/compare` explains the difference between two scans check by check.

The report also prints a second, separate number, the five-page review score, from its own check list. It is labelled as such and says it is not the headline score. Regression tests: `backend/tests/test_score_consistency.py` (row order, separate processes with different hash seeds, one pre-consent request is the whole 100-to-lower gap, no score from too little evidence) and `frontend/src/report/scoreConsistency.test.tsx` (every fixture score on the dashboard card, long report, Free and Full sheets).

## Checks tested from the privacy policy (methodology 1.6.0, 2026-10-26)

Five checks used to be "not tested from outside" because a website scan cannot see them. Every applicable check now carries a **method**: `website_scan` (observed on the pages), `policy_text` (read from the privacy policy) or `organisation_records` (only the organisation's own records can settle it), and a check that is left unverified lists the records that would settle it (`needed_evidence`).

| Check | Method | Met | Partially Met | Unable to Verify |
|---|---|---|---|---|
| DPDP-R9 contact of the DPO or answering person | policy text | quoted, with an email, phone or form beside it | a role is named, no way to reach it | not found in the policy text read, or the policy could not be read |
| DPDP-R14A means to exercise rights | policy text | quoted, with a concrete means | rights mentioned, no means | as above |
| DPDP-R14B grievance system and response period | policy text | a reachable contact and a period of at most 90 days, in a sentence about grievances, complaints or requests | a contact without a way to reach it, or a stated period above 90 days | no contact, no period, or a period not about grievances |
| DPDP-S6 security safeguards | organisation records | never | never | always; the policy's own description is shown beside it and is explicitly not evidence |
| DPDP-S7 breach intimation | organisation records | never | never | always, same rule |

How it stays accurate and costs nothing: the policy analysis (`app/policy/analysis.py`) already runs once per analysis and returns passages that were checked to appear word for word in the policy page, with contact details and the response period parsed by fixed rules. The score reads that stored result (`scan_repository.get_scan_evidence_for_score`, the one loader every scorer uses); no further model call is made. A statement not found in the text is never a failure, because the site may publish it elsewhere. A policy cannot prove what it only describes. Before the analysis has run, the five checks are unverified exactly as before.

On the real swaransoft scans this lifts requirement coverage from 64.1% to 84.6% (R9, R14A and R14B Met from quotations; S6 and S7 stay unverified) with the score unchanged at 100.

The report gains "What was tested and what was not": every applicable check, how it was tested, and for each one not verified why and what would settle it (long report, Full appendix, dashboard, browser-PDF fallback). A category the report normally leaves out (Data principal rights, Security and breach) is shown as soon as one of its checks was assessed, so the tables always agree with the score. The score comparison records whether the policy text was read in each scan and which statements it contained, so a change caused by that is explained as coverage or as the site changing its policy.

**Why the policy checks do not depend on the model's luck.** Reading the policy is one model call, and a model can miss a statement that is plainly on the page (it did, once, where nine other runs on the same page found the officer's contact: the check flipped between scans). So for the four statements that are recognised by fixed words plus a concrete detail (the contact of an officer or answering person, the means of making a rights request, the grievance contact, the grievance response period), a fixed pattern searches the policy text itself when the model reports "not found" (`locate_by_pattern` in `app/policy/analysis.py`). A passage is accepted only if it is an exact slice of the page, names the role or topic, and carries the contact detail (looked for around the passage exactly as the judging rule does) or, for the period, a "within N days" in the same sentence. It never overrides a passage the model offered, finds nothing in a policy that lacks the statement, and costs no tokens. Results stored by an older version are not reused (`ANALYSIS_VERSION`). Checked against the real stored policy text of the scan where the model had missed the contact: it finds the Grievance Officer's contact, the rights-request portal and the sentence "acknowledge your grievance within 48 hours and resolve it within 30 days".

## Verification status of dates and legal mappings (27 Oct 2026)

The commencement dates used (publication 13 Nov 2025; Rule 4 and Act s.6(9) after one year, 13 Nov 2026; Rules 3, 5-16 and most of the Act after eighteen months, 13 May 2027) agree with secondary published summaries of G.S.R. 843(E) and 846(E).
They have **not been verified against the official Gazette notifications**, and sources disagree on the G.S.R. numbers, on 13 versus 14 Nov 2025 for the first tranche, and on 12 versus 13 May 2027.
The mapping of each check to a provision (`LEGAL_MAPPING_VERSION` 2026.10.1) has not had legal review. Treat both as requiring verification by a qualified professional before they are relied on.
A check that passes (labelled "Passes this technical check" in the readiness review) means only that this technical test found what it looked for; it is not a finding that the website complies with the law.

## Consent-tool evidence rules (27 Oct 2026)

* A consent tool's script on a page is not a banner on screen. `has_reject_all` and `has_granular_choices` are facts about a displayed banner: they are recorded only when Accept or Reject wording was seen, or when an Accept/Reject click proves the banner was displayed. A footer "manage cookies" link alone records neither (unknown), for every recognised tool.
* Reports apply the same rule to scans stored earlier: a tool whose banner was never shown, with no click, reads "Unable to Verify" / "Not observed" for the banner, Reject, and per-purpose choices, never Gap and never Found.
* The crawl ranks pages by what they are: an article (a path segment such as blog, news, articles, posts, press) is ranked last whatever its slug says, so /blog/dpdp-act-compliance-guide no longer outranks /contact. The five-page review excludes such pages when it picks its sign-up, contact, account and grievance pages.

* The rule lives in one backend module, `app/rules/consent_observation.py` (`banner_not_displayed`, `banner_displayed`, `observed_signal`). The consent rules, the DPDP score and the findings read it through `ConsentContext`; the evidence API applies `observed_signal` to every stored consent signal, so a scan stored by an older scanner is read the same way and the response carries `banner_displayed` (false, true or null). The public API summary's `consent_states_tested` uses the score's own rule: before-consent needs a really scanned page, Reject needs the banner to have responded (`reject_click_confirmed`).

## Methodology 1.6.1 (27 Oct 2026): "no tracking observed" when Accept was not tested

DPDP-C2 (a consent mechanism where tracking is used) and DPDP-N2 (cookies and trackers are disclosed) apply only where analytics or marketing tracking is observed. On a site that has a consent tool, tracking starts only after Accept. When the Accept choice could not be tested (`accept_interaction` is `cmp_not_automatable` or `click_failed`), nothing after consent was ever seen, so "no tracking observed" is an untested claim. From 1.6.1 both checks are then **Unable to Verify** (left out of the score, counted in the denominator of coverage) instead of **Not Applicable**.

Unchanged: with Accept tested, or with no consent tool, or on a scan from before three-state testing, an absence of tracking still makes both checks Not Applicable. The score (earned over assessed weight) is not changed by this: only the applicable weight, and so the coverage figure, moves. Evidence: two scans of uat.consiva.ai the same morning had identical pages and identical evidence except that Accept was clicked in one and not in the other; coverage was 76.9% and 72.7% and two checks switched between Applicable and Not Applicable. See `docs/SCORE_INCONSISTENCY_ROOT_CAUSE.md`.
