# DPDP requirements reading (replaces the weighted DPDP Compliance Score)

**There is one score in this application: the ten-check Privacy Readiness Score**, worked out on the server by `backend/app/scoring/ten_checks.py` and served by
`GET /api/v1/consent/scans/{scan_id}/assessment` (see `PRIVACY_READINESS_TEN_CHECKS.md`). The weighted "DPDP Compliance Score" (methodology 1.6.1: requirement weights,
weighted points, coverage percentage, in-force score, per-category scores, score comparison, deductions) was **removed on 2026-11-04**. Nothing in the API, the dashboard, the PDFs, the
logs or the configuration computes or shows a weight, a point total, a weighted percentage, a coverage percentage or a "percent if fixed" gain any more.

## What remains: the requirements reading
The 16 DPDP requirement checks stay, as an evidence and status reading with **no numbers attached**. Each requirement has one status:
`met`, `partially_met`, `not_met`, `unable_to_verify` or `not_applicable`; the evidence behind it (observations, evidence ids, pages, whether it is sufficient and why, its
limitation); the provisions it cites with their effective dates; what to do about a gap; and for a check a scan cannot see, the records that would settle it.

- Code: `backend/app/scoring/requirements.py` (the registry and the checks), `findings.py` (the four finding groups and the remediation plan), `review.py` (the human-review
  box and the gap lists), `sector.py` (the sector reading).
- Endpoint: `GET /api/v1/consent/scans/{scan_id}/requirements`, with the query parameters `as_of`, `processes_children_data`, `significant_data_fiduciary`, `consent_manager`,
  `erasure_class` and `business_sector`. Computed on request from the stored evidence; nothing is stored. The old path `/dpdp-score` and `/dpdp-score/compare` return 404.
- Version: `REQUIREMENTS_VERSION` (2.0.0) for the checks and their wording; `LEGAL_MAPPING_VERSION` for the citations and dates.

### Response keys
`requirements_version`, `as_of`, `requirements` (id, category, title, provisions, effective, in_force, status, observations, evidence_ids, rule_ids, exclusion_reason,
severity, description, criteria, expected_evidence, pages, sufficient, sufficiency_reason, limitation, timing, duplicate_of, why_it_matters, remediation, verification,
check_type, applicability, confidence, legal_note, method, needed_evidence), `disclaimer`, `status_counts`, `evidence_summary`, `breakdown`, `human_review`, `finding_groups`,
`methodology` (words only: duplicate rule, timing rule, scope rule, legal sources, dedup groups), `assessment` (scope and states tested), `remediation_plan`, `ledger` (counts of
checks by status), `sector`, `out_of_scope`.

### Rules that remain (non-numeric)
- Unable to Verify is never treated as met or as not met. Not Applicable is never a pass. No check is Met on no evidence.
- Where two requirements describe one gap, the first in the registry that is Not Met is kept and the other is Not Applicable with the reason (`duplicate_of`); nothing is silently passed.
- A gap in a duty not yet in force is Future Readiness, never a current violation. Only a Confirmed Finding on a duty in force, resting on a direct observation, is rated, and the rating is the
  fixed `severity` of the requirement (High / Medium / Low), not a calculation.
- The remediation plan is ordered by a fixed rule: duties in force first, Not Met before Partially Met, then registry order. It says nothing about the effect on a score.
- The three policy-backed requirements (DPDP-R9, R14A, R14B) are answered from the policy analysis; they change a status, never a number.

## What was removed
Requirement weights and the `DPDP_SCORE_WEIGHTS` setting; the weighted score, in-force score, coverage, category scores and the withholding thresholds; the ledger metrics and
penalty notes; the per-requirement points, penalty and contribution; the "percent if fixed" and gain on remediation steps; `GET /dpdp-score/compare` and `scoring/comparison.py`;
`scoring/deductions.py` and `services/score_deduction_log_service.py` (the server log and the audit-table row of deductions); `scripts/validate_dpdp_score.py`.

Database: no table was dropped. Audit rows already written by the removed deduction log stay in `audit_logs` as history; nothing writes new ones.

Historical documents (`docs/development-logs/`, `docs/development-reports/`, `DPDP_ASSESSMENT_AUDIT.md`) describe the weighted score as it was at the time and are not rewritten.
