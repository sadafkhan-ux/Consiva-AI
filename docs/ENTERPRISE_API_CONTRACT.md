# Enterprise API contract

Base path: `/api/v1/enterprise/scans`. Every route is tenant-scoped through the scan's organisation and needs the same authentication as the existing Enterprise routes. All list routes are
paginated with `offset` (default 0) and `limit` (default 50, at most 200) and return `{ items, total, offset, limit }`. Counts are actual counts; a figure that cannot be known yet is `null`.

Already served (unchanged): `POST ""` (start), `GET /{id}/status`, `GET /{id}/coverage` (URL list), `POST /{id}/cancel`, `POST /{id}/resume`. Their shapes are in
`backend/app/enterprise/api/schemas.py` and `frontend/src/api/enterprise.ts`.

## New routes

| Method and path | Purpose |
|---|---|
| `GET ""` | The organisation's Enterprise scans, newest first: `{ items: [{ scan_id, website_url, status, analysis_status, completion, score, coverage, created_at, completed_at }], total, offset, limit }` |
| `GET /{id}/summary` | One call for the scan page (shape below) |
| `GET /{id}/declaration` | The coverage declaration (shape below) |
| `GET /{id}/parameters` | All 39 parameters with their current assessment (shape below) |
| `GET /{id}/parameters/{pid}/history` | Every assessment of one parameter, oldest first, with assessor, model, reviewer and reason |
| `POST /{id}/parameters/{pid}/review` | Body `{ state, reason }` (reviewer is the caller). `state` is one of met, partly_met, not_met, not_assessed, not_applicable. Supersedes the result, recalculates the score, rewrites the findings, appends an audit event. Returns the new parameter view |
| `GET /{id}/findings?severity=` | Paginated findings (shape below) |
| `GET /{id}/evidence?kind=&source_url=` | Paginated evidence index, without content, with a 300 character `excerpt` |
| `GET /{id}/evidence/{evidence_id}` | One evidence record including `content` (at most 200,000 characters; `truncated` says if it was cut for display) |
| `GET /{id}/urls?status=&q=&analysis_status=` | Paginated URL inventory (shape below) |
| `GET /{id}/documents` | Paginated document inventory with extraction status |
| `GET /{id}/score/history` | Every score calculation, newest first: `{ items: [{ id, version, score, coverage, basis, as_of, created_at }] }` |
| `GET /{id}/method3` | Added 20 Nov 2026 (additive). The latest method 3.0.0 result: `{ id, created_at, name, version, score, marks_earned, applicable_max, not_applicable_max, basis, counts, review, categories, lines: [{ parameter_id, category, name, act, rule, max, earned, status, status_label, explanation, evidence, evidence_refs, basis, urls: [{ url, status, note }], review, limitation, differs, applicability, severity, duty_category }], human_review_pending, human_review_max, site_category: { primary, label, categories, labels, possible, confidence, signals, users_stated, not_for_children, disability, guardian_fields } }` (the last fields added 20 Nov 2026, method 3.0.1, additive; older stored results do not have them); 404 until it has been calculated |
| `GET /{id}/method3/history` | Added 20 Nov 2026 (additive). Every method 3.0.0 calculation, newest first: `{ items: [{ id, version, score, marks_earned, applicable_max, basis, as_of, created_at }] }` |
| `GET /{id}/changes?against={scan_id}` | What changed since an earlier scan of the same site (shape below) |
| `POST /{id}/analyze` | Run (or re-run) the analysis phase. 202 `{ scan_id, analysis_status, message }` |
| `POST /{id}/report` | Generate a new report version. 202 `{ scan_id, version, status }` |
| `GET /{id}/reports` | `{ items: [{ version, status, page_count, byte_size, sha256, generated_at, score_result_id }] }` |
| `GET /{id}/report.pdf?version=` | The PDF (latest ready version when `version` is omitted). 404 until one exists |

## Shapes

`summary`
```json
{
  "scan_id": "uuid", "website_url": "https://example.com/", "domain": "example.com",
  "crawl_status": "queued|running|completed|failed|cancelled",
  "analysis_status": "not_started|running|completed|partial|failed",
  "completion": "queued|crawling|crawled|analysing|partial|complete|failed|cancelled",
  "score": {
    "score": 69, "name": "Website-observable DPDP readiness score", "version": "ent-dpdp-1.0.0", "basis": "readiness|assessment",
    "coverage": 82, "reason_no_score": null, "earned_points": 61.5, "points_in_play": 89.0,
    "not_yet_in_force": ["N1"], "by_group": { "notice": { "label": "Notice", "points_available": 30, "points_in_play": 24.0, "earned_points": 20.0 } },
    "needs_attention": { "not_met": ["C8"], "partly_met": [], "not_assessed": [], "human_review": [], "needs_records": [] },
    "as_of": "2026-11-13", "calculated_at": "iso"
  },
  "declaration": { },
  "legal_review_status": "draft: awaiting a named legal sign-off",
  "report_available": false, "options": { }, "created_at": "iso", "started_at": "iso|null", "completed_at": "iso|null", "error": null
}
```
`score` is `null` until a calculation exists. `score.score` is `null` with `reason_no_score` set only when no scored parameter could be assessed. Coverage is reported beside the score and never withholds it.

`declaration` (the coverage declaration; every number is a count)
```json
{
  "crawl_status": "completed", "analysis_status": "partial", "completion": "partial",
  "urls": { "discovered": 0, "eligible": 0, "crawled": 0, "analysed": 0, "pending": 0, "excluded": 0, "failed": 0, "redirected": 0, "inaccessible": 0, "unsupported": 0,
            "duplicates_merged": 0, "awaiting_analysis": 0, "analysis_failed": 0, "excluded_by_reason": { "robots_txt": 0 } },
  "documents": { "discovered": 0, "extracted": 0, "partly_extracted": 0, "unreadable": 0, "extraction_not_recorded": 0 },
  "evidence": { "records": 0, "sources": 0, "analysed": null },
  "analysis_tasks": { "done": 0, "failed": 0, "pending": 0 },
  "parameters": { "total": 39, "assessed": 0, "not_assessed": 0, "not_applicable": 0, "awaiting_human_review": 0, "needs_records": 0, "recorded": 0 }
}
```

`parameters` items
```json
{
  "id": "C4", "name": "No analytics or advertising cookie or request before a choice", "group": "consent", "group_label": "Consent",
  "kind": "statutory|supporting|statement_only|needs_records|human_decision", "points": 5, "partial_allowed": true, "human_review_required": true, "conditional": false,
  "legal_refs": ["s.6(1)"], "in_force": "2027-05-13", "in_force_on_as_of": false, "penalty_ceiling_cr": 50, "schedule_item": 7,
  "state": "met|partly_met|not_met|not_assessed|not_applicable|needs_records|human_review", "earned": 5.0, "in_play": 5.0,
  "note": "none of 6 pages ...", "confidence": null, "assessor": "rule:consent|rule:technical|llm|review", "model": null, "reviewed_by": null, "review_reason": null,
  "evidence_refs": ["ev:uuid"], "adjustment": null, "assessed_at": "iso"
}
```
A parameter with no assessment yet is returned with `state: "not_assessed"` and `assessor: null`.

`findings` items
```json
{
  "id": "uuid", "finding_key": "C8:withdrawal", "parameter_id": "C8", "title": "...", "description": "...", "severity": "high|medium|low|info", "state": "not_met",
  "legal_refs": ["s.6(6)"], "affected_urls": ["https://..."], "affected_count": 1, "evidence_refs": ["ev:uuid"], "expected": "...", "observed": "...", "reason": "...",
  "confidence": "high|medium|low|null", "recommendation": "...", "human_review": false, "verification_status": "unverified|verified|disputed",
  "schedule_item": 7, "penalty_ceiling_cr": 50
}
```
`affected_urls` holds at most 50 addresses; `affected_count` is the true count.

`urls` items
```json
{ "url": "...", "final_url": "...|null", "depth": 0, "status": "pending|in_progress|done|failed|excluded", "analysis_status": "not_started|done|failed|not_applicable",
  "http_status": 200, "status_reason": "...|null", "last_error": "...|null", "discovery_sources": ["sitemap"], "is_document": false, "extraction_status": null }
```

`documents` items: `{ url, final_url, content_type, status, extraction_status, extraction_method, pages_read, pages_needing_ocr, analysis_status, status_reason }`

`changes`
```json
{
  "scan_id": "uuid", "against": "uuid", "comparable": true,
  "urls": { "added": 0, "removed": 0, "added_examples": [], "removed_examples": [] },
  "policies": { "changed": [{ "url": "...", "previous_hash": "...", "current_hash": "..." }] },
  "cookies": { "added": ["name|domain|path"], "removed": [] },
  "trackers": { "added": ["host"], "removed": [] },
  "forms": { "added": 0, "removed": 0 },
  "consent": { "changes": [{ "test": "withdrawal", "from": "met", "to": "not_met" }] },
  "findings": { "new": [], "resolved": [], "changed": [{ "finding_key": "...", "from": "not_met", "to": "partly_met" }] },
  "coverage": { "previous": { }, "current": { } },
  "score": { "previous": 70, "current": 65, "delta": -5 }
}
```

## Rules the UI and the report must follow

- Show crawl completion, analysis completion and assessment completion as three separate things. `completion: "complete"` is the only state that may be shown as finished; `partial` says what is outstanding.
- A parameter in state `not_assessed`, `needs_records` or `human_review` is never shown as passed or failed.
- While `basis` is `readiness`, the score is labelled a readiness score and the parameters not yet in force are named. It is never described as a finding of breach or as certification.
- `penalty_ceiling_cr` is the most the Act's Schedule lets the Board impose for that kind of breach. It is shown as a ceiling set by law, not a prediction.
- Legal review status (`legal_review_status`) is shown wherever the score is shown while it is a draft.


## Per-page evidence tracking (16 Nov 2026, migration 0057)

`GET /{scan_id}/urls` items carry, besides the earlier fields, what was extracted from each page and how much of it was analysed. All are null for a page that was not read.

| Field | Meaning |
|---|---|
| `title` | the page's title |
| `text_chars` | characters of text extracted |
| `content_hash` | sha256 of that text |
| `extracted_at` | when it was extracted |
| `evidence_note` | anything that limited the read: scrolling stopped at a limit, collapsed sections not opened, embedded text cut, iframes from other sites not read, a page over the evidence size limit kept in parts |
| `category`, `category_reason` | how the page was classified and why |
| `chunks_created`, `chunks_analysed` | passages created from the page and passages analysed |
| `analysis_note` | why a page was not sent to the model, or how many passages failed |

The coverage declaration's `urls` object gains `not_recorded` (links found after the record cap, counted by occurrence), `pages_with_limits`, `pages_not_fully_analysed` and `pages_not_sent_to_model`.
A page that ends at the same address as one already read, or has identical text (400 characters or more), is excluded with the reason `duplicate_page: the same page as <url>`; its links are still recorded.
