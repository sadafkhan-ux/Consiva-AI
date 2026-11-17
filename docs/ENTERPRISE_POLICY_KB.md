# Enterprise policy knowledge base

Added 16 Nov 2026 (migration 0058). Code: `backend/app/enterprise/policykb/`, API `backend/app/enterprise/api/policy_kb.py`, check `backend/tools/policy_kb_integration.py`, tests `backend/tests/enterprise/test_enterprise_policy_kb.py`.
The original 5-page agent does not import any of it (a test checks this) and none of its files were changed.

## What it does

After every finished Enterprise scan, the pages the analysis read as policies (privacy, cookie, terms, grievance, contact) are cleaned, hashed and compared with the stored documents of that website.

| What the scan found | What happens |
|---|---|
| Same document, same content hash | Nothing is re-chunked or re-embedded. The document is re-verified (`last_verified_at`). |
| Same document, different content | A new version. Chunks that did not change reuse their stored embedding; only changed chunks are embedded. The old version is kept. |
| A new document | Cleaned, chunked, embedded, stored as version 1. |
| A known document at a new address | It keeps its identity; the address is added to `url_history`; no new version if the text is unchanged. |
| A known document that answered 404 or 410 | Marked `removed`. Versions and chunks are kept; its chunks are no longer searched. |
| A known document that could not be fetched, or whose extraction failed | The last verified version stays current and the document is marked `stale` with the reason. |
| The whole site could not be read | Nothing is overwritten; every active document is marked `stale`. |

A new version becomes current only after its chunks exist and every embedding was produced or reused, in one transaction. A failure earlier leaves the last verified version exactly as it was.
A processing failure is recorded in the sync log and the scan's audit trail. It is never recorded as an assessment or finding about the website.

## Two separate things

* The knowledge base holds what the organisation **declares** (policy text).
* What the website **does** (cookies, trackers, forms, requests, consent behaviour, headers) is still captured by the crawl into `ent_evidence` and assessed by deterministic rules. It is not stored in, or mixed with, the knowledge base.

## Reused, not rebuilt

Same Postgres and the pgvector extension already installed; the same 1024-dimension embedding model and client the 5-page product's knowledge base uses; the same crawler, evidence store, page classifier and scan history. No second database, queue or embedding service.
The 5-page product's `knowledge_documents` and `knowledge_chunks` (the DPDP legal texts) are untouched.

## Schema (migration 0058, additive)

`ent_policy_docs` (identity per website), `ent_policy_versions` (full cleaned text, hash, classification, status, one current version per document), `ent_policy_chunks` (stable id, heading, content hash, `vector(1024)`, full-text `tsvector`, `is_active` only for the current version), `ent_policy_sync_log`, `ent_policy_retrievals`.
Also: `ent_analysis_chunks.text_hash` and `.extractor_key`, and `ent_frontier.page_category_strength`. Every table carries `org_id`; row-level security is enabled and forced, and every query also filters by `org_id` because the application's database role bypasses row-level security.

## Chunking and retrieval

HTML is cleaned with a real parser (BeautifulSoup): scripts, styles, navigation, site header and footer and cookie-banner containers are removed; headings, paragraphs, lists, tables (one row per line), links and the text of expanded sections are kept. Without stored HTML, or when the cleaned HTML is under half of the rendered text, the rendered text is used and the version says so.
Chunks follow section boundaries (about 1,800 characters, whole paragraphs under one heading); one oversized paragraph is split by length. Nothing is dropped: the chunks hold every paragraph in order.
Retrieval is hybrid: keyword (`websearch_to_tsquery`) and semantic (cosine over pgvector) fused by reciprocal rank, always within one organisation and website, over current chunks only. If the question cannot be embedded, the result is keyword-only and `mode` says `keyword_fallback`.

## Fewer model calls

The analysis reads every chunk of every policy page once to extract facts (unchanged). A later scan of the same organisation now **reuses** the facts of a passage whose text, extraction prompt and model are identical (`ent_analysis_chunks.text_hash` + `extractor_key`), instead of calling the model again. A changed text, prompt or model changes the key and the passage is read again. A passage answered by a fallback model is not offered for reuse. Switch off with `ENTERPRISE_REUSE_ANALYSIS=false`; the knowledge base itself with `ENTERPRISE_POLICY_KB_ENABLED=false`.

## API (`/api/v1/enterprise/policy-kb`)

`GET /metrics`, `GET /documents`, `GET /documents/{id}/versions`, `GET /documents/{id}/diff?from_version=&to_version=` (with the DPDP parameters to look at again), `GET /documents/{id}/text`, `POST /search`, `GET /consistency`, `POST /repair-embeddings`, `GET /sync-log`. Metrics come from stored rows; token figures are labelled estimates (characters / 4).

## Limits

* The assessment still reads every chunk's extracted facts. Retrieval is available (API and library) and returns sources, but the DPDP parameter rules are not yet fed by retrieved chunks instead of facts: doing so would change how parameters are assessed and needs the owner's decision.
* Reuse is per passage. The later levels (mapping, cross-page comparison) are still computed each scan.
* Dedicated "Data Retention Policy" or "DPO" pages are stored under whichever category the existing page classifier gives them.
* Cost in money is not shown: the per-model price is not configured, and exact token counts are not stored per Enterprise passage.

## Measured on real scans (swaransoft.com, 25-page limit, Claude as the model)

Counted from `ent_analysis_chunks`: passages are the page-level passages analysed in a scan.

| Scan | Passages sent to the model | Characters sent | Passages reused from an earlier scan | Characters not sent |
|---|---|---|---|---|
| Before this feature (6c93a9b4) | 34 | 119,412 | 0 | 0 |
| First scan with it (ecd628f5, cache empty) | 32 | 112,949 | 0 | 0 |
| Second scan (b67c1352, a different set of 25 pages) | 25 | 68,110 | 10 | 53,267 |
| Third scan (d85cade6) | 7 | 30,232 | 35 | 139,628 |

Every passage whose text appeared in an earlier scan was reused (the second scan's 25 other passages were pages the first scan had not read). Scan time did not change (about 3.5 minutes): it is dominated by crawling, not by the model. Token counts are not stored per passage, so characters are the measure; money cost is not computed. These figures come from one site and three scans; they are not a promise for other sites.
The knowledge base on the third scan: 2 new documents, 3 unchanged (no embedding requested for those), 48 embeddings generated for the new ones.

## The duplicate rule was removed, and what replaced it (16 Nov 2026, owner's decision)

A page was dropped when its text equalled another page's. On the first real scan that dropped `/privacy` (this site serves the same text at `/cookie-policy`), so N1 read "no privacy notice". The owner asked that no page be counted as a duplicate at all. The rule is gone: two addresses serving the same text are both read, each under its own address. Identical evidence is stored once and an identical passage is not sent to the model twice (the reuse above), so nothing is paid for twice.

**The model reads the site's addresses** (`crawl/slug_triage.py`). After the sitemap and again after the entry page's links are recorded, the addresses that no crawl pattern ranks first (blog and news paths are not sent) go to the model as numbered paths, 400 to a call, up to 1,200 per scan. It answers as JSON which probably hold a privacy policy, cookie policy, terms or grievance page, and how sure it is. A confident answer raises the crawl priority (the page is read early) and is kept as a hint (`slug_kinds`). A hint never classifies a page: when nothing in the page settles it, the page goes to the model as ambiguous and its content decides. A failed call costs only the hint. Checked with the real model on addresses such as `/gizlilik-politikasi`, `/kvkk`, `/datenschutz`, `/legal` (all four kinds), `/help/grievance-redressal`; it did not flag `/p/12` (an address with no meaning) and gave `/trust/dp` low confidence, which is ignored. On swaransoft.com it flagged nothing, because that site's addresses are ordinary.

**Several policies in one page** (`analysis/embedded.py`). A heading that names a policy ("2. Cookie Policy", "Our Privacy Notice", "Terms of Service", "Grievance Officer", "Data Protection Officer"), with at least 400 characters under it (its sub-sections included), marks the page as also holding that policy. A heading that only mentions one ("Is your cookie policy ready?") names nothing, and a one-line mention under "Cookies" is not a cookie policy. A page with no category of its own (a "Legal" hub) takes the first policy it holds as its category. The embedded section is then:
* counted for its own category in the parameter rules (as a virtual source: read with the page, never sent to the model a second time);
* credited with the facts whose quotes lie inside it (the same quote and source, attributed to the cookie policy as well as the page);
* stored in the knowledge base as its own document, `<page address>#cookie_policy`, holding only that section's text.

Real scan (swaransoft.com): `/privacy` was found to hold the cookie policy as a section, `/privacy#cookie_policy` was stored as its own document, `/cookie-policy` was kept as well, nothing was excluded as a duplicate (only 3 query variants), N1 and the cookie parameters were assessed.
Not done: the model is not asked to find embedded policies; they are found from headings and structure only, so a policy under a heading that does not name it is not found.

## A bug found on the first real scan, and fixed


That duplicate rule dropped `/privacy`; it was first narrowed (policy addresses exempt) and then removed altogether at the owner's request. Fake-database tests did not show it; the real scan did. A second bug of the same kind was also found and fixed: an unchanged document kept a wrong type, now corrected from the latest classification.
