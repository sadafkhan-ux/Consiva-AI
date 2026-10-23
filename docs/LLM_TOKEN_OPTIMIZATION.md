# LLM token optimisation — 2026-10-22

Scope: the Consent Agent's reasoning call (the only large LLM consumer), the RAG retrieval that feeds it, and a check of the
other agents' LLM calls. Nothing was committed, pushed or deployed beyond restarting the local `backend` and `worker` containers.

## 1. Where the tokens actually went (measured, not assumed)

Source: `agent_run_stages` in the Docker database, 23 stored scans, 40 model calls (the stage metadata holds `usage_by_attempt`).

| Measure | Value |
|---|---|
| Input tokens, all stored calls | 239,783 |
| Output tokens, all stored calls | 74,422 |
| Tokens spent in **validation-retry** stages | **98,757 input (41%)**, 23,275 output (31%) |
| Scans that needed at least one retry | 9 of 23 |
| Scans where every attempt was spent and the run fell back to rule findings | 4 |
| Validation failures that were vendor-timing claims only | 13 of 19 |
| Validation failures that were ungrounded citations only | 5 of 19 (all while the knowledge base was empty) |

**Root cause 1: the vendor-timing check rejected findings that deny a timing.** `validate_output._unsupported_vendor_claims`
searched the whole finding for "before consent" / "after Reject" and then for any vendor name. "No tracker fired before consent;
Google Analytics loaded only after Accept" matched both and was rejected, so the entire analysis ran again with the full prompt.
The stored failures are exactly this shape: the same vendors (Google Ads, Google Analytics, Google Tag Manager) named as firing
"before consent" or "after Reject" on sites where they fire only after Accept. On four scans three attempts were spent and
nothing the model produced was used.

**Root cause 2: fixed prompt text on every call.** A typical call is 4,000 to 8,000 input tokens, and more than half of the user
prompt is text that never changes (instructions, schema, notes). The evidence itself is 2,100 to 3,000 characters.

**Root cause 3: unreadable knowledge chunks.** 54 of 279 knowledge chunks (51 of the 105 DPDP Rules chunks) are legacy-encoded
Hindi that extracts as garbage. Two of the 55 chunks retrieved in recent scans were of this kind: about 320 prompt tokens each
that tell the model nothing. The English text of the same provisions is present.

**Confirmed healthy, no change needed:** evidence compaction (grouped trackers, table format, caps), the batched query
embedding and its cache, `top_k = 3` per query with at most 8 chunks (measured in earlier work), prefix-friendly prompt order.
ROPA makes one capped batch call per run, RegWatch one call per detected change. `prompts/ropa_agent_prompt.md` is not used by any
code and costs nothing.

## 2. Changes

| Change | File | Effect |
|---|---|---|
| Vendor-timing check ignores a claim phrase that is being denied. Negation must come before the phrase in the same sentence with no contrast word between; "did not stop / failed to stop / did not wait" and "except ..." stay assertions | `backend/app/agents/consent_agent/nodes/validate_output.py` | Removes the false-positive retries. The check still flags a false assertion |
| The schema shown in the prompt omits pydantic's auto `title` fields and the model docstring (`_lean_schema`). The schema sent to the provider as the enforced format is unchanged | `backend/app/llm/prompts.py` | −259 characters per call and per retry |
| Shorter "Vendors confirmed by state" preamble, with the explicit allowance that saying nothing fired is fine | `backend/app/llm/prompts.py` | −154 characters and a clearer instruction |
| Unreadable chunks are skipped at ingestion and dropped at retrieval. Retrieval fetches `top_k + 6` rows, drops unreadable ones, returns the best `top_k` | `backend/app/rag/text_quality.py` (new), `chunker.py`, `retriever.py` | A query still returns `top_k` useful chunks. No stored data is changed |
| `ANTHROPIC_EFFORT` setting (low, medium, high), default `medium` | `backend/app/config.py`, `llm/client.py`, `.env.example` | No change by default. An opt-in way to reduce the hidden reasoning that counts as output tokens |
| `tokens_in` and `tokens_out` totals on the `llm_analysis` stage | `.../nodes/llm_reasoning.py` | Per-scan cost is readable without summing `usage_by_attempt` |

## 3. Measured before and after

Prompt size, built from two real stored scans (same evidence, rules and three retrieved chunks, measured inside the container):

| Scan | User prompt before | After | Saved |
|---|---|---|---|
| gignaati.com 277f3f37 | 11,161 chars | 10,699 chars | 462 (4.1%) |
| swaransoft.com 41a03c60 | 11,585 chars | 11,123 chars | 462 (4.0%) |

At the observed ratio of about 2.2 characters per token this is roughly 200 tokens per call, 3% of a typical request including the
system prompt. It is small on its own. The large saving is the retry removal.

Retrieval, four realistic queries against the live knowledge base (one batched call to the project's existing hosted embedding
endpoint):

| Check | Result |
|---|---|
| Embedding generation | 4 vectors, 1,024 dimensions, 1.8 s |
| Chunks returned, before and after | Identical sets and distances for all four queries |
| Unreadable chunks returned | 0 before, 0 after (none ranked in the top 3 for these queries) |

Retrieval quality is unchanged. The filter is protective: historically 2 of 55 retrieved chunks were unreadable, and those now
cannot occur.

**Estimated effect of the validator fix.** Retry stages were 41% of input and 31% of output tokens, and 13 of 19 retries were
vendor-timing claims. If all of those were false positives the ceiling is roughly 28% of input and 21% of output tokens, plus the
four scans that now would keep their model-written findings. The true figure depends on how many of those 13 were real false
assertions, which the stored data cannot show because failed outputs are not kept. **This estimate has not been confirmed with a
live run.**

## 4. Validation

| Check | Result |
|---|---|
| New tests `backend/tests/test_token_optimization.py` | 22 passed |
| Full backend suite, excluding the two live-API files | 3,016 passed, 46 skipped (database-gated), 0 failed |
| Containers | `backend` and `worker` rebuilt and restarted with `--no-deps` so the migration job did not run; both healthy |
| Re-ingestion | Not done, deliberately: the embedding model and dimensions are unchanged, and `rag/ingest.py` has no checksum check, so a second ingest would duplicate every chunk |

The new tests cover: ten phrasings that must not be flagged and must still be flagged (including "did not stop firing after
Reject", "failed to stop", "except Google Analytics", "was not blocked, but fired before consent"); the schema keeps required
fields, enums and field descriptions; unreadable text detection; chunking skips an unreadable page; retrieval drops unreadable
chunks, keeps rank order and still honours the distance ceiling.

## 5. Open issues and things not verified

1. **A live test was run by mistake.** `tests/test_prompt_injection_probe.py` calls the real configured model provider when `.env`
   has keys. It was included in one test run and made about three real external calls. It then **failed**: the model returned the
   schema text instead of findings on all three attempts. Whether this was caused by the leaner prompt schema or is the provider's
   existing behaviour has **not been determined**. Answering that needs the same probe run once against the old prompt and once
   against the new, which makes external calls, so it needs your approval. `tests/test_rag_retrieval_quality.py` is also live.
2. **No end-to-end scan was run.** The primary reasoning provider on the live stack is Claude (`ANTHROPIC_API_KEY` is set), an
   external paid API, so a scan was not run. The validator and prompt changes are verified by tests and offline prompt
   construction, not by a live model response.
3. **The 54 unreadable chunks are still in the database.** They are now ignored, but removing them is a data deletion and was not
   done. A one-off cleanup (or a re-ingest of the bilingual PDFs after deleting the old rows) would also shrink the index.
4. **Fixed prompt text is still about half of every call.** The evidence-format notes (2,030 characters) and the system prompt
   (3,025 characters) overlap in places. Trimming them could save a few hundred tokens per call but changes model behaviour, so it
   needs a live quality comparison first. Provider prompt caching would help only if the static prefix reaches the provider's
   minimum cacheable size, which this prefix (about 3,000 tokens) may not.
5. **Hidden reasoning is the largest output cost on the Claude path** (604 of 1,001 output tokens on the sample call). The new
   `ANTHROPIC_EFFORT=low` option is available but not enabled, because finding quality has not been compared.
6. **The stack's primary provider is external.** Moving the primary to the self-hosted model would remove the external cost
   entirely. That is an architecture decision and was not changed.
7. The vendor check remains regex-based. It is narrower, not semantic: a sentence that asserts a timing in an unusual way can
   still be missed or flagged.
