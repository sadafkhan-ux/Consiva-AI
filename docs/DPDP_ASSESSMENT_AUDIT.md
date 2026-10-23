# DPDP assessment: current-state audit, plan and validation

Companion to `DPDP_COMPLIANCE_SCORE.md` (the methodology). This records what the Consent Agent had, what was added, how the result was
validated, and what it cannot claim. Written 21 Oct 2026. Nothing here is committed or deployed.

## 1. Current-state audit (what existed, reused as it was)

| Area | What existed | Used here |
|---|---|---|
| Scanner (`app/scanner`) | Playwright crawl of the main domain, three consent-state passes (Before Consent, After Accept, After Reject), cookie/tracker/policy/form/banner evidence persisted per scan, page attribution on every tracker, per-cookie sightings | Unchanged. The score reads the persisted evidence only, so it never re-scans. |
| Rules engine (`app/rules/consent_rules.py`) | Rules R-001 to R-012 producing findings; the facts they depend on were computed inline | The facts are now one function, `build_consent_context`, used by the rules and the score, so a finding and a check cannot disagree. Rules and their review workflow are untouched. |
| Findings / LLM / RAG | LangGraph analysis with local LLM and RAG, human review gate, per-finding severity | Untouched. The four finding groups are derived from the requirement checks, not from LLM output, and the LLM/RAG path is not involved in the score. |
| API | Scan, evidence, findings, audit routes, JWT auth, org scoping | One read-only route added, `GET /consent/scans/{id}/dpdp-score`, with the same authentication and org scoping as the evidence route. |
| Dashboard / PDF | Evidence tables, findings with review actions, a PDF with a risk banner | A full score section on the dashboard. The PDF is a fixed two-page report with the score, the formula and the findings, and no legal wording; its "Overall risk" banner was replaced by the score. |
| Tests | 2,700+ backend tests, frontend suite | Extended; no existing test was weakened except those that asserted the old three-page cap or the old risk banner. |

**Gaps found** and closed: no legal mapping or effective dates; no evidence-based score; risk shown as the highest finding severity;
no separation of website checks from business checks; unverified results indistinguishable from clean ones; no remediation order;
scope and scan time not stated in the report.

## 2. Scope decisions (read these)

* **The page limit is 5 pages of the entered main domain (confirmed by the owner on 22 Oct 2026).** It is one constant, `MAX_WEB_PAGES`;
  the findings, the PDF brief, the methodology text and the API documentation all read that number instead of carrying their own, and tests
  fail if they drift.
* Only the entered main domain is assessed. Subdomains and other websites are not crawled, listed or reported. Third-party requests
  seen on the scanned pages are kept as evidence and are never treated as violations by themselves.
* Authentication, org scoping, the local LLM/RAG architecture and the SSRF protections are unchanged.

## 3. What was delivered

* **Versioned requirements and legal mapping** (`LEGAL_MAPPING_VERSION`, `REQUIREMENTS`, `DETAILS`, `APPLICABILITY_TEXT`): 16 checks, each
  with ID, description, criteria, expected evidence, provisions, effective date, applicability, weight and rationale.
* **Versioned methodology** (`METHODOLOGY_VERSION`): the formula, 100/50/0 points, coverage as a separate figure, "Insufficient Evidence",
  duplicate suppression, configurable weights, category weights.
* **Backend assessment** (`app/scoring`): score, categories, coverage, status counts, four finding groups, remediation plan,
  Human Review box, methodology, scope and scan time. Reproducible: a pure function of the recorded evidence, the date and the declared
  applicability.
* **Dashboard and PDF** render one shared view of that data. The dashboard shows everything; the PDF is deliberately two pages and plain.
* **Tests:** backend `test_dpdp_score`, `_checks`, `_review`, `_findings`, `_methodology`, `_assessment`; frontend `dpdpScore.test.tsx`,
  `dpdpScorePdf.test.ts` (which compare dashboard and PDF text for the same fixtures, generated from the backend).
* **Validation harness:** `backend/scripts/validate_dpdp_score.py` (read-only).

## 4. Validation

**Automated, against recorded evidence.** `validate_dpdp_score.py` re-scores every completed scan in the database and checks invariants
(score and coverage in range, status counts add up, nothing Met without pages, every gap has remediation, severity only on duties in force,
no requirement in two groups, category weights add up, nothing about other domains) and agreement with the rules engine (a check is
never Not Met unless its rule fired, never Met when it did), and that two runs give identical results. Result on 21 Oct 2026:
**22 completed scans checked, 0 violated invariants.**

**Manual review.** Six sites were reviewed by reading the recorded raw evidence next to the result:

| Site | Raw evidence (recorded) | Result | Reviewed |
|---|---|---|---|
| swaransoft.com | Consent tool shown; Accept and Reject clicked and confirmed; no known tracker before consent or after Reject; privacy and cookie policy found | 100.0%, coverage 64.1%, all seven website checks Met | Agrees. |
| consiva.ai | Consent tool; 24 known tracker items before consent; Reject not automatable | 76.2%, coverage 53.8%; C1 Not Met, C3 Unable to Verify | Agrees: before-consent tracking is Not Met (a readiness gap), Reject untested is not scored. |
| corp.gignaati.com | 12 known items before consent; Reject not automatable; no cookie policy | 71.4%, coverage 53.8%; C1 Not Met, N2 Partially Met, C3 Unable to Verify | Agrees. |
| digitalglobal.ai | No banner; 6 known items before consent; no privacy policy; 1 page | 0.0%, coverage 39.1%; C1 and N1 Not Met, C2 and N2 counted under them | Agrees; the caution flags the thin basis. |
| agentmira.ai | No banner; no known trackers; privacy policy found | 100.0%, coverage 39.1%; only C1 and N1 assessed | Agrees on the facts, but 100% on two checks is flattering: it now carries a caution. This was found by the review and fixed. |
| www.honda2wheelersindia.com | No banner; 46 known items before consent; privacy policy, no cookie policy (an older 25-page scan) | 45.5%, coverage 44.0%; C1 Not Met, N2 Partially Met | Agrees. |

The same worked numbers are fixed as unit tests (`TestHandReviewedGoldenExamples`), calculated on paper from the weights.

**Not validated:** the agreement of the legal mapping with the statute beyond the Rules (see limitations); behaviour on sites whose banner
only appears to some regions; scoring of pages beyond the first 5.

## 5. Limitations (disclosed in the report too)

1. **Website readiness is not legal compliance.** The result is an evidence-based assessment of what a scan of the entered main domain can
   see. It is not a guarantee, certification or legal advice; organisational evidence (retention, DPIA, grievance handling, breach response,
   contact details, a record of processing) and legal interpretation are needed for compliance and are shown as Unable to Verify or Needs
   Human Review. A ROPA is not required by name under the Act or Rules.
2. **Most duties are not yet in force** (13 May 2027), so today's gaps are Future Readiness, not violations.
3. A scan sees at most 5 main-domain pages in at most three consent states; Reject can only be tested when the banner can be operated.
4. A network request or cookie shows something was observed, not that a tracker ran or what it sent, and is never a violation by itself.
5. The scanner's vantage point is one server; a banner shown only to some regions may not appear.
6. Business impact and remediation are generic texts per check. Weights are a proposal; a configured set is declared in the report.
7. The Act's sub-section numbers should be verified against the statute before client use.

## 6. Acceptance checklist

| Criterion | Where |
|---|---|
| Every score reproducible from recorded evidence | `assess()` is pure; `test_the_same_recorded_evidence_gives_an_identical_result`; the validation script re-runs every stored scan |
| Legal mappings traceable | every result lists its provisions; `TestLegalMappingIsVersioned` pins the mapping to the version |
| Unverified checks transparent | Unable to Verify group, coverage, "Insufficient Evidence", the caution |
| Future vs current separated | Future Readiness group, readiness and in-force scores, effective dates |
| Dashboard and PDF match | shared view; `twoPageReport.test.ts` compares score, coverage, categories, counts, findings and fix order with the dashboard for the same fixtures |
| Website readiness distinguished from legal compliance | nature note in both, Human Review box |
