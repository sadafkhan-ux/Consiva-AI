# LLM usage and cost: Consent Agent scan and report

Measured on 2026-10-23 from the stored stage records of the live database (14–23 Oct 2026) plus one new measured scan. Nothing here is estimated unless it says so.

## Where tokens are spent

| Operation | Calls an LLM? | Provider / model | Evidence |
|---|---|---|---|
| Crawl, page discovery, banner detection, Accept/Reject testing, cookie and request capture | No (Playwright, HTTP, regex) | none | `scanner/` has no LLM import |
| Cookie / tracker classification, rules R-001..R-012 | No (catalogue, Open Cookie DB, code) | none | `rules/consent_rules.py` |
| **Policy content analysis** (`policy_analysis` stage) | **Yes**, 1 call per analysis run that has a policy page | Claude Haiku 5.5 (NVIDIA/Groq fallback) | `policy/analysis.py` `_default_extractor`, agent `consent-policy` |
| RAG retrieval | Embedding call (NVIDIA `llama-nemotron-embed-vl-1b-v2`), 1 per analysis run | NVIDIA hosted | `rag/retriever.py`; not an LLM, token count now measured |
| **Findings narrative** (`llm_analysis` stage) | **Yes**, 1 call, plus validation retries | Claude Haiku 5.5 (fallback NVIDIA Nemotron) | `agents/consent_agent/nodes/llm_reasoning.py`, agent `consent` |
| Output validation, finding creation, human review gate | No | none | `validate_output.py` is regex and set checks |
| DPDP score, coverage, finding groups, readiness review, sector, policy completeness | No (deterministic code) | none | `scoring/`, `report/readiness.ts` |
| Report data assembly, web report, **PDF generation** | **No** | none (headless Chromium, CPU only) | `services/report_pdf_service.py`; a live PDF made 0 usage events and 0 LLM stages |

Report generation is not part of any scan's AI cost and nothing about it can be double-counted: it reads data that is already stored.

## Measured usage

Pricing: Claude Haiku 5.5, $0.10 per million input tokens and $0.50 per million output tokens for prompts up to 100,000 tokens (cache reads $0.01, 5-minute cache writes $0.125); over 100,000 tokens $0.50 / $2.50. Source: platform.claude.com/docs/en/about-claude/pricing, fetched 2026-10-23. The largest prompt in the sample was 47,377 tokens, so the higher tier never applied. Reasoning tokens are inside `output_tokens` and are billed once, as output.

Sample: 101 Claude calls (84 analysis stages, 17 policy analyses) across 59 scans of 14 sites, 14–23 Oct 2026. Ten NVIDIA Nemotron analysis stages (16–19 Oct) have token counts but no price and are excluded from cost. No cache reads or writes occurred (the code does not use prompt caching).

| Per scan (Claude) | min | median | mean | max |
|---|---:|---:|---:|---:|
| All 59 scans: input tokens | 4,134 | 11,930 | 17,287 | 68,370 |
| All 59 scans: output tokens | 414 | 4,392 | 4,360 | 11,993 |
| All 59 scans: cost | $0.00082 | $0.00344 | $0.00391 | $0.01220 |
| Current pipeline (policy analysis + findings), 17 scans: input | 16,849 | 25,163 | 33,774 | 68,370 |
| Current pipeline: output | 3,515 | 6,974 | 7,645 | 11,993 |
| Current pipeline: cost | $0.00344 | $0.00591 | $0.00720 | $0.01220 |

Total for the sample: 1,019,913 input and 257,246 output tokens, $0.2306. At the current-pipeline mean, 1,000 scans a month is about $7.20 (median basis $5.91); this assumes the same mix of sites, one analysis per scan and no re-runs.

New measured scan (swaransoft.com, 2026-10-23, after the tracking below went live):

| Stage | Calls | Input | Output | of which reasoning | Cost | Time | Request id |
|---|---:|---:|---:|---:|---:|---:|---|
| `policy_analysis` | 1 | 12,106 | 4,989 | 3,875 | $0.00370 | 20.5 s | `req_011CgJiPuuF5AUN82a7UcSXj` |
| `llm_analysis` | 1 | 6,048 | 364 | 354 | $0.00079 | 3.1 s | `req_011CgJiRaS1orURWag6D2XPd` |
| Embedding (RAG) | 1 | 16 | n/a | n/a | not priced | | |
| **Scan total** | 2 LLM calls | 18,154 | 5,353 | 4,229 | **$0.00449** | | |
| PDF report of that scan | 0 | 0 | 0 | 0 | $0 AI (3.6 s of CPU) | 3.6 s | |

## Where the cost is

- **Policy analysis is 66% of the cost** of a current-pipeline scan (findings narrative 34%). Its prompt is the policy text (12k–47k input tokens) and it writes 4.5k–6.8k output tokens; in the measured run 78% of those were reasoning.
- **Reasoning is 66% of the findings narrative's output** (110,203 of 167,767 tokens). In the measured run the model wrote 364 output tokens, 354 of them reasoning, for a site with nothing to report.
- **Validation retries are 31% of analysis input and 30% of its output** (25 retry stages in 84; $0.0453 of the $0.2306 total, 20%). A retry re-sends the whole prompt.
- Since 22 Oct 16:00, 41 validations ended: 19 valid, 16 retried, 6 failed after retrying and fell back to rule findings (tokens spent, no narrative). 15 of the 16 retries were rejections for a vendor-timing claim, 1 for an ungrounded citation, 1 for both.
- The same policy text is not sent twice inside one analysis. It is re-sent on every new scan of the same site (swaransoft.com appears 18 times in the sample), because nothing caches the result.

## Tracking (new)

Before: a flat file `logs/llm_usage.jsonl` inside each container (lost on every rebuild; 260 of its lines in the repository are unit-test calls with zero tokens), and stage metadata that held full usage only for `llm_analysis`. A failed or refused call was never recorded, and when the primary provider failed the fallback's usage overwrote it.

Now (`app/llm/usage_tracking.py`, migration `0041_llm_usage_events.sql`, `app/db/repositories/usage_repository.py`):

- One row in `llm_usage_events` per provider call group: scan, agent run, agent, **stage**, provider, model, provider **request ids**, attempts, input / output / cache read / cache write / **reasoning** tokens, success or failed with the error type, fallback flag, **duration**, cost, currency and price source.
- **Failed calls are recorded**, including a refusal (its usage is now carried out of the client instead of being lost), and the primary and the fallback each keep their own usage.
- **No duplicates:** `call_id` is derived from the call group and provider and is unique; a repeated report of the same call inserts nothing.
- **No mixing between scans:** each call has its own usage sink; the row carries its own scan id.
- **Safe:** never stores prompts, responses or keys; unknown usage is zeros with reasoning and cost NULL; a failure to record is logged and never raised; unpriced models have NULL cost, not zero.
- Tenancy follows the scan (row-level security like `agent_run_stages`); the embedding token count is added to the `rag_retrieval` stage record.
- Report: `python -m app.llm.usage_report [--scan <id>] [--days N]` (run in the worker container).
- Unit tests no longer write to the real usage log or a database (`tests/conftest.py`).

## Recommendations (none applied; each needs a before/after measurement)

1. **Cache the policy analysis by content.** Key on a hash of the policy text, the requirement set and the prompt version; reuse the result while all three are unchanged. It targets the largest cost (66%). Measure: policy calls per scan in `llm_usage_events` before and after, plus a check that results match for an unchanged site. Risk: a stale result if the key leaves something out.
2. **Lower the reasoning effort for the policy extraction only.** Quote-copying is mechanical and 78% of its output was reasoning; effort is currently one global setting (`ANTHROPIC_EFFORT`, medium). Measure on a fixed set of policies: `quotes_rejected` and the not-found rate against the tokens saved. Risk: more points reported as not found.
3. **Find why the vendor-timing check is still involved in 15 of 41 validation outcomes (14 retries plus 1 alongside an ungrounded citation).** Each retry re-sends the prompt. Read the rejected findings and decide whether the check or the prompt is wrong; do not loosen the check without reading them. Measure: retry and failed share in `output_validation`.
4. **Do not skip the findings call when the rules found nothing.** In 12 of 19 such runs the model still drafted findings, so skipping would lose output. (Whether those findings are good is not measured here.)
5. **Cap or trim the policy input.** 47,377 tokens was the largest prompt; check what in it is boilerplate before trimming, since a cut policy changes what can be quoted.

## Limits of this measurement

- Token counts for 14–23 Oct come from the provider usage stored in stage metadata; the per-call table did not exist, so request ids, durations and reasoning for **policy** calls are only available from the new scan onwards.
- Policy-call reasoning for earlier scans is not recorded (that usage block was not stored), so the 66% reasoning figure is for the findings narrative only.
- The 10 NVIDIA Nemotron calls and the embedding calls are not priced: no verified price was available. Embedding tokens were not stored before today.
- A call that failed before the provider answered (network error) has no usage to record; whether such attempts are billed is not visible.
- The sample is mostly repeat scans of a few sites during development, not a production mix. The monthly figure is an extrapolation with stated assumptions.

## Live token usage panel (console only)

The right-hand side of the Consent Agent console shows the usage of the scan on screen. It is not part of the report or the PDF.

- **API**: `GET /api/v1/consent/scans/{scan_id}/usage` (totals, cost, per-stage, per-page) and `GET /api/v1/consent/scans/usage/history?limit=` (earlier scans). Code: `app/services/usage_service.py`, routes in `app/api/v1/routes/consent_scans.py`.
- **Storage**: one row per provider call in `llm_usage_events` (migrations 0041, 0042), written as each response arrives, idempotent on `call_id`, tenant-isolated by scan. Scans made before 0041 are read from their stage records and labelled "From stage records"; scans with nothing recorded show "Unavailable", never zero.
- **Live**: the panel polls every 2 s while a scan is busy and reads once more when it ends; a read still in flight is not repeated.
- **Pages**: a call is attributed to a page only if it read exactly one page. Calls that read several pages are shown as "Shared"; calls with no recorded pages as "Unattributed".
- **Cost**: tokens x verified provider prices (`ANTHROPIC_PRICES`, fetched 2026-10-23). Unpriced models (NVIDIA, self-hosted, embeddings) show tokens and no cost.
- **Zero-LLM stages**: browser scan, classification, deterministic rules, scoring and report/PDF rendering are listed with 0 LLM tokens; the embedding model is listed separately as "not an LLM".
