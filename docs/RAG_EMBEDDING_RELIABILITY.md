# RAG embedding reliability — 2026-10-22

## What failed

Scan 499b709a (swaransoft.com) ended with `Analysis failed at stage "rag_retrieval": Request timed out.`. The rule engine matched nothing, so
the scan had no findings at all and showed as Failed, although its score was 100%.

## Root cause (measured against the live endpoint, not assumed)

The RAG embeds each retrieval query on NVIDIA's hosted endpoint (`nvidia/llama-nemotron-embed-vl-1b-v2`). That endpoint answers in 0.3 to 3 seconds
or does not answer at all:

| Probe | Result |
|---|---|
| 5 single-query calls, 40s timeout | 4 answered (0.25 to 5.5s), 1 hung for the full 40s |
| 8 batched calls (1, 4 or 6 queries), 30s timeout | 4 answered (0.5 to 3s), 4 hung for the full 30s |
| 3 identical requests sent at the same moment | 2 answered in about 0.47s, 1 hung |

The code waited for each request for 6 seconds, three times, and then my earlier change repeated that once (39s in all). A hung request never
recovers, so waiting longer or retrying one after another could not fix it, and a slow-but-healthy call (up to 7.1s was seen) was also cut off.

## What changed

1. **Hedged embedding calls** (`app/llm/client.py`, `hedged`). If a request has not answered after `embed_hedge_after_seconds` (1.5s), an identical
   request is sent alongside it, up to `embed_max_retries` (3) extra, and the first answer wins. The others are cancelled. `embed_timeout_seconds`
   (12s) is the deadline for the whole call. A healthy call is unchanged (one request). A refusal (400) is raised at once and never hedged.
   Live result: 12 batched calls, 12 answered (0.2 to 7.1s); in a later run 11 of 12, the failure being all four requests hanging for 12s.
2. **Fallback 1: stored provision embeddings** (`app/rag/topic_embeddings.py`, `topic_embeddings.json`). Each retrieval query ends with the provision
   the rule concerns, in brackets (`RULE_DPDP_TOPIC_HINTS`), and that part decides which provision is retrieved. Its embedding is stored once, with
   the model and width it was made with (a mismatch makes the file unusable, never applied). When the service does not answer, the stored vector is
   searched with the same cosine search over the same chunks. The query used when no rule matched is stored too.
3. **Fallback 2: keyword search** (`_text_search`, PostgreSQL full-text search over the approved chunks). Used only for a query that names no stored
   provision. It prefers the bracketed provision words and the DPDP Act and Rules over other sources.
4. **The stage says which was used**: `retrieval_mode` is `embedding`, `topic_embeddings`, `text_search` or `topic_embeddings+text_search`, with
   `embedding_error` when the service did not answer. Retrieval no longer fails the analysis because the embedding service is down.
5. The earlier node-level retry was removed (the client now does the retrying, in parallel).

## Fidelity of the fallbacks (7 real rule queries, top 3 chunks, compared with the normal full-query result)

| Method | Chunks in common with the normal result |
|---|---|
| Stored provision embeddings | 10 of 21 |
| Keyword search | 4 of 21 |

Neither equals the normal result: the stored vectors drop the rule's own wording, and keyword search matches words rather than meaning. Both return
chunks of the approved sources only, and the checks on what the model may cite still apply to them. A scan that used a fallback is visible on its
`rag_retrieval` stage.

## Regenerating the stored embeddings

After changing `NVIDIA_EMBED_MODEL` or its dimensions, or the hints in `RULE_DPDP_TOPIC_HINTS`, with the service up:

    docker compose -f docker-compose.prod.yml exec worker python -m app.rag.topic_embeddings /tmp/topic_embeddings.json
    docker compose -f docker-compose.prod.yml cp worker:/tmp/topic_embeddings.json backend/app/rag/topic_embeddings.json

## Not changed

The hosted endpoint itself is still unreliable. Ingestion (`rag/ingest.py`) embeds documents with the same client and so also benefits from hedging,
but was not re-run. The 54 unreadable chunks are still in the database (see LLM_TOKEN_OPTIMIZATION.md).

## Status, warnings and the score (same day)

**Why a 100% score sat beside "Failed".** The score and the checks are computed from the scan's own evidence and do not depend on the analysis step
(explanations, citations, model-assisted findings). The page, however, collapsed any failed analysis stage into "Failed" whenever the rules had produced
no findings, and said "No findings could be produced... the deterministic rules matched nothing to fall back on", which read as a broken result.

**Now:** a failure in an analysis stage (`rag_retrieval`, `llm_analysis`, `output_validation`, `findings_generated`, `rule_findings_generated`) after the
scan completed leaves the scan "degraded", labelled "Scan complete — analysis incomplete" (or "Findings ready — explanations unavailable" when the rules
did produce findings). "Failed" is kept for a scan that did not complete. The page shows a notice stating that the checks and score do not depend on the
analysis, that there are no explanations or citations, and that no findings is not a statement the analysis would have found nothing; the score card and
the PDF carry the same one-line note; the empty Findings and Recommendations sections say why they are empty.

**A second cause found by running the complete workflow.** The vendor lists given to the model, and used to check what it writes, included functional items
(fonts, static assets, the consent tool's own script). The model then reported them as a HIGH "continued firing after Reject" finding on a scan whose score
was 100%. The lists now hold analytics, marketing and unclassified items only, the prompt says functional items are not tracking and that tracking seen only
after Accept is not an issue, and the validator therefore rejects a finding that calls a functional item tracking.

## End-to-end validation (real services, real worker, Claude as the reasoning model)

| Scan | Result |
|---|---|
| swaransoft.com, before the prompt fix | all stages completed; 4 findings incl. one HIGH about functional scripts, against a 100% score |
| swaransoft.com, after | all stages completed (analysis 33s), 0 findings, run completed, score 100%, coverage 64.1% |
| honda2wheelersindia.com | all stages completed (78s), retrieval by embedding, 5 findings with 5 recommendations and citations, score 45.5% |
