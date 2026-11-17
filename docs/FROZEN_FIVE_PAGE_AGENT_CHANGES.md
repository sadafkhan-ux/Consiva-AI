# Changes made to the frozen 5-page agent

Each entry is a change the owner asked for after the freeze. The manifest (`five_page_agent.manifest.json`) was regenerated after it, and the full backend suite was run.

## 17 Nov 2026 (third change, the verified root cause of the clyzo.com false banner): a policy page's whole article was read as control text

Found by scanning clyzo.com again on the owner's instruction: the scan reproduced the report exactly (banner "found" on the home page, no reject-all, per-purpose choices, score 19). Its stored consent signal said `matched_keywords: ["privacy preferences"]`, source `control_text_keywords`. Running the scanner's own collectors on the five pages showed where the phrase was: the sentence "To manage your privacy preferences and to ensure you only receive communications that you have requested..." in the privacy policy page, inside 35,256 characters of "rendered control text" for that one page (10,043 for the cookie policy page).

Root cause: `_SHADOW_DOM_CONTROL_TEXT_JS` added the whole text of ANY visible element whose class or id contained cookie, consent, gdpr, ccpa, cmp or privacy, with no length limit. The scanner always opens the privacy and cookie policy pages, and their main container is named that way, so the entire policy article became "controls". The detector then found a strong settings phrase ("privacy preferences") in the prose and an accepting word ("agree", "ok") elsewhere in it, across all pages joined together. This is likely the main source of false banners on any site, not only clyzo.com.

| File | Change |
|---|---|
| `backend/app/scanner/crawler.py` | `_SHADOW_DOM_CONTROL_TEXT_JS`: a container named as consent UI counts only when its text is at most 1,200 characters; `cmp` is matched as a whole word and `privacy` only as privacy-banner/notice/dialog/popup |
| `backend/app/scanner/page_parser.py` | `extract_control_text`: the same 1,200-character limit for the serialized-HTML fallback |

Not changed: the CMP path, the score and its formula, any rule, the report layout. Short banners named cookie/consent are read as before (tests cover both).
Tests: `tests/test_consent_banner_false_positive.py` (a 'privacypolicy' article and a 'cookiepolicy' section are not banners; a short cookie container still is).

## 17 Nov 2026 (second change): pop-ups that use accept/reject words but are not about cookies

Asked by the owner after the clyzo.com review: "any pop-up with accept or reject buttons may be falsely detected as a banner". Tested through the real detector with 11 pages: generic wording ("Subscribe / No thanks", "I agree / Decline", an app-download modal, an age gate) was already not detected, but four non-cookie pop-ups were reported as a cookie banner: "Allow all notifications / Block", "Accept all / Reject all" for friend requests, for marketing e-mail choices, and an "Accept all" button inside any element whose class contained "privacy".

Root cause: the phrases "accept all", "allow all", "reject all" ... were matched as plain substrings of the controls' text, with no check of what they referred to or of the box they were in; and the container-name pattern matched "privacy" and "cmp" anywhere in a class or id.

| File | Change |
|---|---|
| `backend/app/scanner/consent_signal_detector.py` | `_has` / `_any`: whole-phrase matching; a phrase ending in "all" is refused when it goes on to name something that is not a cookie (notifications, friend requests, e-mails, invitations, ...) |
| `backend/app/scanner/page_parser.py` | the consent-container pattern now needs cookie, consent, gdpr, ccpa, `cmp` as a whole word, or privacy-banner/notice/dialog/popup; `_about_something_else`: a bare Accept all / Reject all whose surrounding box is about e-mail, notifications or the like, and says nothing about cookies, consent, tracking or privacy, is not read as consent wording |

Not changed: the CMP path, the score and its formula, any rule, the report layout. Real banners (Accept all / Reject all with cookie wording, cookie dialogs with bare Accept / Decline, CMP scripts) are detected as before; tests cover both directions.
Tests: `tests/test_consent_banner_false_positive.py` (non-cookie pop-ups are not banners; real banners still are; the phrase rule).
Open: a bare "Accept all / Reject all" pair in a box with no text of its own at all (a requests panel with only buttons) is still read as consent wording; telling it apart needs knowing what the page is about, which markup alone does not say.

## 17 Nov 2026: a footer "Cookie settings" link is not a consent banner

Reported by the owner from the clyzo.com report (16 Nov 2026): "Consent Banner DETECTED, Cookie banner, Reject-all No, Choices per purpose Yes" for a site on which a visitor sees no banner. A fresh load of all five pages showed no banner, no CMP script and no consent iframe.

Root cause, verified in code and with the detector alone: in the keyword path of `detect_consent_signal`, a "strong" settings phrase ("cookie settings", "manage cookies", "cookie preferences", "privacy preferences", "manage preferences") on any clickable control was enough to establish a banner by itself. A footer link with that text, on a page with no banner, therefore produced `mechanism=banner, has_reject_all=False, has_granular_choices=True`, which is exactly the pattern of the report. (The live clyzo.com footer today says only "Cookies" and the detector returns `none` for it, so the scan-day evidence that matched could not be recovered; the stored scan data was not available.)

| File | Change |
|---|---|
| `backend/app/scanner/page_parser.py` | `_is_hidden`: a control or consent container hidden in the markup (`hidden` attribute, inline `display:none` or `visibility:hidden`, on it or any parent) is not read as a consent control, by `extract_control_text` and `cookie_dialog_controls` |
| `backend/app/scanner/consent_signal_detector.py` | a settings phrase establishes a banner only beside an accepting word (accept, allow, agree, got it, ok, ...; whole words) or with Accept/Reject wording. On its own it returns `none`, with `evidence.cookie_settings_link_only = true` |

Not changed: the CMP path, the score and its formula, any rule, the report layout, what counts as Accept or Reject wording. A real banner (Accept all / Reject all, a CMP script, a cookie dialog) is detected exactly as before.
Tests: `tests/test_consent_banner_false_positive.py` (`test_a_lone_cookie_settings_link_is_not_a_banner`, `test_a_settings_link_beside_an_accepting_control_is_a_banner`; the old case that pinned "cookie settings alone is a banner" was removed from `test_a_strong_phrase_alone_does_establish_one`).
Tests also: hidden dialogs are not banners; a first-visit 'Download our App' modal whose script calls getCookie/setCookie is not a banner (the case a reviewer suspected on clyzo.com).
Open: styles set by a stylesheet are invisible to the markup check (only inline hiding is seen), and the report does not yet separate BANNER_VISIBLE / HIDDEN / NOT_FOUND / NOT_VERIFIABLE. Not done here.

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

## 16 Nov 2026: the embedding model was retired by the provider; knowledge base re-embedded (owner: "jo tumko thik lage kro")

Found by the owner's own five-page scan: "Failed at RAG Retrieval (205 ms)", reason `APIStatusError: Error code: 410`. NVIDIA no longer serves `nvidia/llama-nemotron-embed-vl-1b-v2` (HTTP 410; it is gone from the provider's model list; it answered a test call earlier the same day), so every analysis ended without explanations or citations (the score is unaffected: it does not depend on the analysis).

No Python of the five-page agent was changed. The configured model is read from `NVIDIA_EMBED_MODEL`, and the client already sends `dimensions=NVIDIA_EMBED_DIMENSIONS` (1024), so the replacement `nvidia/nemotron-3.5-embed-1b` (verified live: 1024 dimensions with that parameter) is a configuration change. Following the procedure `backend/app/rag/topic_embeddings.py` documents:

| What | Change |
|---|---|
| `backend/.env` (not in git) | `NVIDIA_EMBED_MODEL=nvidia/nemotron-3.5-embed-1b` |
| `knowledge_chunks.embedding` (279 rows, the DPDP legal texts) | every chunk re-embedded with the new model in one transaction (all vectors computed first); text, metadata and ids unchanged. Backup of both knowledge tables: `backups/knowledge_base_pre_embed_change_*.dump` |
| `backend/app/rag/topic_embeddings.json` (frozen data file) | regenerated with `python -m app.rag.topic_embeddings`: 7 stored topic vectors, model and width recorded in the file |

Not changed: the scanner, P1-P12, scoring, prompts, report, any API. The frozen manifest was regenerated for the data file.

## 17 Nov 2026: two deployed-vs-local differences on jambulogix.com (owner: "approval hai, bas sahi karo")

The same site scored 86 on the owner's local stack and 42 on the deployed one. 44 marks: 24 from the Reject control not being read, 20 from the notice's AI reading not completing. From the two PDFs: the deployed screenshot had no banner although the Consiva Consent SDK was detected; the SDK's own 800 ms configuration timeout (see `_BANNER_PASS_RETRIES`) is the documented cause of exactly that. The deployed P7-P9 said "The notice could not be read" while P6 had read it (3,501 characters), which hid that the analysis step had not completed.

| File | Change |
|---|---|
| `backend/app/scanner/crawler.py` | `_BANNER_PASS_RETRIES` 1 to 2: a page that loaded a known consent tool but drew no banner is looked at up to two more times in a fresh browser context |
| `backend/app/scoring/domain_score.py` | `dependent()`: when the notice was found and the analysis status is `unavailable`, the reason reads "The notice was found, but its analysis could not be completed in this scan..." instead of "The notice text could not be read." |
| `backend/app/scoring/scorecard.py` | `prereq_a1`: a not-assessed P7-P9 row uses the A2 reason before the generic wording |

Not changed: marks, rules, bands, prompts, the analysis model call (no blind retry added: an earlier measurement found retries were 41% of tokens; the cause of the failed call on the deployed host is still unknown). Tests: `tests/test_scorecard.py` (new), `tests/test_consiva_cmp_scanning.py` (retry count is read from the constant).

## 17 Nov 2026 (later): two flakes of the same site's score, both caused by a consent banner drawn late (owner: "bana do ache sai")

jambulogix.com scored 86, 82, 78, 50 on consecutive scans with the same code. Read from the databases and the reports: (1) the withdrawal test (P5, 8 marks) says "Accept could not be clicked" when the Consiva SDK draws no banner on that page view (its 800 ms configuration timeout), and the test had no retry (a probe: first attempt failed 1 of 8 times alone, a retry in a fresh session fixed it); (2) the home page's control text was read once, and "Reject all" and "Manage preferences" were in it in one scan and not in two others, which flips per-purpose choices and P1 between 8 and 4 marks (has_granular_choices true or false across the last nine scans).

| File | Change |
|---|---|
| `backend/app/scanner/withdrawal.py` | `run_withdrawal_test`: when Accept cannot be clicked, reload the home page and try again, at most `_ACCEPT_RETRIES = 2` times; what was recorded before the retry is dropped; only then "accept_not_clicked" |
| `backend/app/scanner/crawler.py` | new `_settled_control_text`: on the entered address only, the visible control text is read again until it has not grown for two readings in a row (at most three rereads, 700 ms apart), longest reading kept; other pages are read once as before |

Not changed: marks, rules, P1 to P12, bands, the banner passes (`_BANNER_PASS_RETRIES` stays 2), the API. Cost: up to about 1.4 s more on the entered address of a scan; up to two more page loads only when Accept failed. Tests: `tests/test_withdrawal_browser.py` (two new, real browser, offline site that draws no banner on its first one, two or three page views), `tests/test_banner_control_text_settles.py` (new, real browser).

