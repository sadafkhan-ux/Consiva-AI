import { useEffect, useState } from "react";
import type { ConsentScanState } from "../hooks/useConsentScan";
import { deriveFailureReason } from "../hooks/useConsentScan";
import { formatMs, formatTimestamp } from "./shared";
import { summariseStages, unaccountedMs } from "./stageTiming";

const PHASE_LABELS: Record<string, string> = {
  idle: "Idle",
  starting: "Starting…",
  scanning: "Scanning…",
  analyzing: "Analyzing…",
  awaiting_review: "Completed — awaiting human review",
  completed: "Completed",
  degraded: "Findings ready — explanations unavailable",
  failed: "Failed",
};

const PHASE_TONE: Record<string, string> = {
  starting: "progress",
  scanning: "progress",
  analyzing: "progress",
  awaiting_review: "ok",
  completed: "ok",
  // Not "bad". The scan found real violations and is showing them; red would tell
  // the reader to discard a result they should be acting on. Not "ok" either --
  // the findings are unexplained and every one needs a person.
  degraded: "warn",
  failed: "bad",
};

const STAGE_ORDER = [
  "url_validation", "website_scan", "data_structuring", "classification",
  // normalize_evidence is the analyze job's own first node -- it loads the scan
  // evidence the rest of the graph reasons over, distinct from the scan job's
  // "classification" above (tracker/cookie vendor tagging).
  "normalize_evidence",
  "rules_check", "rag_retrieval", "llm_analysis", "output_validation",
  // Alternatives, not sequential steps: a run reaches findings_generated OR
  // rule_findings_generated, never both. Listed adjacently so whichever one ran
  // appears in the same position in the progress list.
  "findings_generated", "rule_findings_generated",
  "human_review_gate",
  "audit_saved",
];
const STAGE_LABELS: Record<string, string> = {
  url_validation: "URL Validation", website_scan: "Website Scan",
  data_structuring: "Data Structuring", classification: "Tracker/Cookie Classification",
  normalize_evidence: "Loading Scan Evidence",
  rules_check: "Rules Check", rag_retrieval: "RAG Retrieval",
  // Not "NVIDIA LLM Analysis". The provider is chosen at run time -- self-hosted
  // first, NVIDIA only as fallback -- so this label named the wrong one on every
  // run that never reached the fallback. Scan 63497fc6 failed on the SELF-HOSTED
  // provider and the page reported "Failed at NVIDIA LLM Analysis", pointing the
  // reader at the wrong system. The real provider is recorded per run and shown
  // in the provider field; this label states the stage, not a guess at who served it.
  llm_analysis: "LLM Analysis", output_validation: "Structured Output Validation",
  findings_generated: "Findings Generated",
  rule_findings_generated: "Findings From Rules (no narrative)",
  human_review_gate: "Human Review Gate",
  audit_saved: "Audit Saved",
};

export function ScanStatusSection({ state }: { state: ConsentScanState }) {
  // The clock the "still scanning" hint reads, refreshed on a timer so it stays true between polls.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(id);
  }, []);

  if (state.phase === "idle") return null;

  const failureReason = state.phase === "failed" ? deriveFailureReason(state.error, state.websiteScanMeta) : null;
  const label = failureReason ? `Failed — ${failureReason}` : PHASE_LABELS[state.phase];
  const tone = PHASE_TONE[state.phase] ?? "progress";

  const byStage = summariseStages(state.stages);
  const totalMs = state.durationMs ?? (state.startedAt && state.completedAt ? state.completedAt - state.startedAt : null);
  const gapMs = state.completedAt ? unaccountedMs(totalMs, byStage) : null;
  const currentStage = STAGE_ORDER.find((name) => byStage[name] && byStage[name].status === "running");
  const meta = state.websiteScanMeta;
  const knowledgeBaseEmpty = state.stages.some(
    (s) => s.stage === "rag_retrieval" && (s.metadata as Record<string, unknown>)?.knowledge_base_empty === true,
  );
  // Said plainly, because "Pages Scanned: 5" alone reads as "that is all there was".
  const pageLimitReached = meta?.stop_reason === "page_limit" && meta.page_limit != null;
  const skippedPages = meta?.pages_not_attempted_budget_exhausted ?? 0;
  // A page that could not be loaded is not a page that was scanned. Said when it happened, because
  // "Pages Scanned: 3" alone does not tell the reader that two more were tried and lost.
  const unloadedPages = (meta?.pages_failed ?? 0) + (meta?.pages_timeout ?? 0);
  // A long scan is not a failed scan.
  const runningLong =
    state.phase === "scanning" && state.startedAt != null && now - state.startedAt > 120_000;

  return (
    <section className="section">
      <h2>1. Scan Status</h2>
      <div className={`status-banner ${tone}`} style={{ marginBottom: 14 }}>
        <span className="dot" />
        {label}
      </div>
      {runningLong && (
        <p className="muted small" role="status">
          Still scanning — large or slow sites can take several minutes. The scan has not failed; this page
          keeps checking and will show the result when it finishes.
        </p>
      )}
      {unloadedPages > 0 && (
        <div className="status-banner warn" role="status" style={{ marginBottom: 14 }}>
          Partial scan: {unloadedPages} page{unloadedPages === 1 ? "" : "s"}
          {meta?.pages_attempted != null ? ` of the ${meta.pages_attempted} attempted` : ""} could not be loaded
          and {unloadedPages === 1 ? "is" : "are"} not part of the results below — they cover only the pages that
          loaded.
        </div>
      )}
      {knowledgeBaseEmpty && (
        <div className="status-banner warn" role="status" style={{ marginBottom: 14 }} data-testid="kb-empty">
          The regulatory knowledge base is empty, so the findings below cannot cite any DPDP provision. They come
          from the scan's rules only. Ingest the knowledge sources, then run the scan again.
        </div>
      )}
      {pageLimitReached && (
        <div className="status-banner warn" role="status" style={{ marginBottom: 14 }}>
          Page limit reached: a scan covers at most {meta.page_limit} pages per scan, so only the first{" "}
          {meta.page_limit} were scanned
          {skippedPages > 0 ? ` and ${skippedPages} more page${skippedPages === 1 ? "" : "s"} found on the site ${skippedPages === 1 ? "was" : "were"} not scanned` : ""}.
        </div>
      )}

      <div className="grid cols-5" style={{ marginBottom: 14 }}>
        <div className="stat"><div className="n scan-id" style={{ fontSize: 11 }}>{state.scanId?.slice(0, 8) ?? "—"}</div><div className="l">Scan ID</div></div>
        {/* Relabelled. This field is the CRAWL's status, and showing it as "Backend
            Status: completed" directly under a red "Failed" badge read as the page
            contradicting itself -- when in fact the crawl completed and the analysis
            afterwards did not. Naming what it measures removes the contradiction
            without hiding either fact. */}
        <div className="stat">
          <div className="n" style={{ fontSize: 15 }}>{state.scan?.status ?? "—"}</div>
          <div className="l">Crawl Status</div>
        </div>
        <div className="stat"><div className="n" style={{ fontSize: 13 }}>{formatTimestamp(state.startedAt)}</div><div className="l">Started At</div></div>
        <div className="stat"><div className="n" style={{ fontSize: 13 }}>{formatTimestamp(state.completedAt)}</div><div className="l">Completed At</div></div>
        {/* Fall back to the timestamps. `durationMs` is only set on the success
            path, so a failed scan showed "—" for Duration while Started At and
            Completed At were both populated a few pixels away. */}
        <div className="stat">
          <div className="n">
            {formatMs(
              state.durationMs ??
                (state.startedAt && state.completedAt ? state.completedAt - state.startedAt : null),
            )}
          </div>
          <div className="l">Duration</div>
        </div>
      </div>

      <div className="grid cols-3" style={{ marginBottom: 14 }}>
        {/* When a stage failed, name it. It was already known and displayed further
            down the same page, while this field rendered "—". */}
        <div className="stat">
          <div className="n" style={{ fontSize: 14 }}>
            {state.failedStage
              ? `Failed at ${STAGE_LABELS[state.failedStage] ?? state.failedStage}`
              : currentStage
                ? STAGE_LABELS[currentStage]
                : state.phase === "completed" || state.phase === "awaiting_review"
                  ? "Done"
                  : "—"}
          </div>
          <div className="l">Current Stage</div>
        </div>
        <div className="stat"><div className="n">{state.scan?.evidence_counts.pages ?? "—"}</div><div className="l">Pages Scanned</div></div>
        <div className="stat"><div className="n">{state.error ? "Yes" : "No"}</div><div className="l">Errors / Warnings</div></div>
      </div>

      <div className="stages">
        {STAGE_ORDER.map((name) => {
          const s = byStage[name];
          const status = s ? s.status : "pending";
          return (
            <div className="stage-row" key={name}>
              <div className={`stage-dot ${status}`} />
              <div className="stage-name">
                {STAGE_LABELS[name]}
                {s && s.attempts > 1 ? <span className="muted small"> ({s.attempts} attempts)</span> : null}
              </div>
              <div className="stage-dur">{s?.durationMs != null ? formatMs(s.durationMs) : ""}</div>
              <div className="stage-status">{status}</div>
            </div>
          );
        })}
      </div>
      {gapMs != null && gapMs >= 1000 && (
        <p className="muted small" data-testid="stage-gap">
          Stages above add up to {formatMs((totalMs ?? 0) - gapMs)} of the {formatMs(totalMs)} total. The other{" "}
          {formatMs(gapMs)} is the wait between steps (a step is picked up from the job queue only after the
          previous one is saved), not time inside a stage.
        </p>
      )}
    </section>
  );
}
