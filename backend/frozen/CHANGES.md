# Changes made to the frozen 5-page agent

Each entry is a change the owner asked for after the freeze. The manifest (`five_page_agent.manifest.json`) was regenerated after it, and the full backend suite was run.

## 13 Nov 2026: links on a redirected site, and a privacy page that was linked but did not load

Reported by the owner from a scan of xpenseai.in: 8 of 9 pages did not load, and the requirements reading said "No privacy policy page was found" (Not met, high) while the scorecard said the notice was found.

Root cause, verified against the stored scan and with curl: the homepage redirects from `xpenseai.in` to `www.xpenseai.in`. The crawler resolved the page's relative links against the address it had asked for, not the address the page ended at, so `/privacy-policy` became `https://xpenseai.in/privacy-policy` (404) instead of `https://www.xpenseai.in/privacy-policy` (200).

| File | Change |
|---|---|
| `backend/app/scanner/crawler.py` | `parse_page(html, final_url or url, ...)`: links resolve against the final address |
| `backend/app/rules/consent_rules.py` | `ConsentContext.unloaded_policy_urls`: privacy or policy pages the scan found a link to but could not load |
| `backend/app/scoring/requirements.py` | DPDP-N1: a policy page that was named but did not load is `unable_to_verify`, not `not_met`. No page found anywhere is still `not_met` |

Not changed: the score, its formula, the scorecard, any other rule, the report layout.
Tests: `tests/test_scanner_link_base.py` (new), `tests/test_requirements_reading.py::TestAPrivacyPageThatWasLinkedButDidNotLoad` (new).

## 16 Nov 2026: coverage no longer withholds the score

Asked by the owner: "if a site's coverage is below 60%, show the score it came to; do not remove coverage." Before this, a scan whose assessed marks were under 60% of the 100 showed "No score" (the score was kept only as an internal `indicative_score`).

| File | Change |
|---|---|
| `backend/app/scoring/scorecard.py` | the `coverage < 60` branch is removed: the score is `round half up of 100 x awarded / in play` whenever at least one row is in play; coverage is still computed and reported beside it. "No score" remains only when no row could be assessed. The low-confidence band rule and the "100 is held at 99 unless a Deep Scan with full coverage and high confidence" rule are unchanged |
| `backend/app/services/assessment_service.py` | the summary sentence for a missing score now says "no row could be assessed" (the 60% wording is gone) |

Not changed: the score's formula, the rows and their marks, the coverage figure and where it is shown, the legacy rollback engines (`domain_score.py`, `ten_checks.py`), the report layout.
Tests: `tests/test_scorecard.py::test_the_score_is_shown_whatever_the_coverage_and_coverage_never_withholds_it` replaces the test that pinned the 60% rule. The same rule was applied to the Enterprise score (`backend/app/enterprise/scoring/engine.py`), which is not part of the frozen agent.

## 16 Nov 2026: the score is the marks earned out of 100; rows not assessed or not applicable earn nothing

Decided by the owner: "if the consent banner is not detected, cookies are not detected, or any required content is missing, the marks must be deducted... not applicable ke bhi marks cut karo." Before this the score was `100 x awarded / in play`, so rows left out of play cost nothing and a site with no banner could score 91.

| File | Change |
|---|---|
| `backend/app/scoring/scorecard.py` | `score = round half up of awarded`, out of the 100 the rows are worth; a row not assessed or not applicable earns 0 and stays in the 100. Category `assessable` is the category's full marks. Coverage is still computed and shown, and never changes the score. The 100 held at 99 rule is unchanged. "No score" only when no row could be assessed |
| `backend/app/services/report_qc.py` | `check_scorecard` requires the categories to total 100 and the score to equal the marks awarded |
| `backend/app/services/assessment_service.py` | `as_scorecard` wording: "0 of N pts (not assessed / not applicable)", "so no marks were earned", `applicable_max` 100 |
| `frontend/src/report/**` | wording only ("N of 10 checks passed (X marks)", rows that earn no marks) |

Not changed: the rows, their marks, the pass/partial/fail rules, bands, the coverage figure, the report layout.
Tests: `tests/test_scorecard.py` (76 and 52 scenarios, hold at 99 through the engine's result function), `tests/test_report_qc.py` fixtures. The same rule was applied to the Enterprise engine, which is not part of the frozen agent.
