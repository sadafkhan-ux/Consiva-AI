import type { ConsentScanState } from "../hooks/useConsentScan";
import { deriveFailureReason } from "../hooks/useConsentScan";
import { formatMs, formatTimestamp } from "./shared";

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
  "rules_check", "rag_retrieval", "llm_analysis", "output_validation",
  // Alternatives, not sequential steps: a run reaches findings_generated OR
  // rule_findings_generated, never both. Listed adjacently so whichever one ran
  // appears in the same position in the progress list.
  "findings_generated", "rule_findings_generated", "audit_saved",
];
const STAGE_LABELS: Record<string, string> = {
  url_validation: "URL Validation", website_scan: "Website Scan",
  data_structuring: "Data Structuring", classification: "Tracker/Cookie Classification",
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
  audit_saved: "Audit Saved",
};

export function ScanStatusSection({ state }: { state: ConsentScanState }) {
  if (state.phase === "idle") return null;

  const failureReason = state.phase === "failed" ? deriveFailureReason(state.error, state.websiteScanMeta) : null;
  const label = failureReason ? `Failed — ${failureReason}` : PHASE_LABELS[state.phase];
  const tone = PHASE_TONE[state.phase] ?? "progress";

  const byStage = Object.fromEntries(state.stages.map((s) => [s.stage, s]));
  const currentStage = STAGE_ORDER.find((name) => byStage[name] && byStage[name].status === "running");

  return (
    <section className="section">
      <h2>1. Scan Status</h2>
      <div className={`status-banner ${tone}`} style={{ marginBottom: 14 }}>
        <span className="dot" />
        {label}
      </div>

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
              <div className="stage-name">{STAGE_LABELS[name]}</div>
              <div className="stage-dur">{s?.duration_ms != null ? formatMs(s.duration_ms) : ""}</div>
              <div className="stage-status">{status}</div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
