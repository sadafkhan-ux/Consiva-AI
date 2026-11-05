# 2026-11-05: Scanner Score wired into /assessment

- New engine `backend/app/scoring/domain_score.py` (Scoring Mechanism v1.0), docs `docs/SCANNER_SCORE.md`, gap analysis `docs/SCANNER_FUNCTIONAL_SCOPE_GAP_ANALYSIS.md`.
- `GET /scans/{id}/assessment` (path unchanged) serves it through `assessment_service.as_assessment`; `AssessmentResponse` gained `engine`, `scanner` and `checks[].ref`. `SCORING_ENGINE=ten_check` rolls back.
- Frontend: band, ceiling, coverage and evidence confidence come from the server and are printed with the score (hero, panel, summary text in every PDF); "ten checks" headings made engine-neutral.
- Tests run: backend 516 (earlier suites) + 45 (new); frontend 53 files, 972 + 5 new tests, `tsc -p tsconfig.app.json` clean.
- Not done: docker rebuild, check against stored real scans, and the Functional Scope items listed in the gap analysis.
