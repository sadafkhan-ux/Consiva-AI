# DPDP Compliance Score: methodology v1.5.0

**Status: a proposed methodology. It is not an officially prescribed legal formula, not a certification and not legal advice.**
It measures what a public website scan can observe, against the DPDP Act, 2023 and the DPDP Rules, 2025.

Code: `backend/app/scoring/dpdp_score.py` (the only place a score is calculated). Endpoint:
`GET /api/v1/consent/scans/{scan_id}/dpdp-score`. Dashboard: `DpdpScoreSection`. PDF: summary page line plus the
"DPDP Compliance Score" pages. Both render one view, `frontend/src/report/dpdpScore.ts`, so they cannot differ.
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
