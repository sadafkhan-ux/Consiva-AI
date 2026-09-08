import { useState } from "react";
import { useConsentScan } from "./hooks/useConsentScan";
import { ScanStatusSection } from "./components/ScanStatusSection";
import { ConsentSummarySection } from "./components/ConsentSummarySection";
import { ThreeStateSection } from "./components/ThreeStateSection";
import { TrackersTable } from "./components/TrackersTable";
import { CookiesTable } from "./components/CookiesTable";
import { FormsSection, PoliciesSection } from "./components/FormsAndPoliciesSection";
import { FindingsSection } from "./components/FindingsSection";
import { DpdpReferencesSection, RecommendationsSection } from "./components/DpdpAndRecommendations";
import { AuditSection, DiagnosticsSection } from "./components/AuditAndDiagnostics";
import { buildReportModel } from "./report/buildReport";
import { downloadReport } from "./report/downloadReport";

export default function App() {
  const { state, run, afterReviewDecision } = useConsentScan();
  const [url, setUrl] = useState("https://example.com");
  const [authorized, setAuthorized] = useState(true);
  const [buildingReport, setBuildingReport] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);

  const busy = state.phase !== "idle" && state.phase !== "completed" && state.phase !== "failed" && state.phase !== "awaiting_review";

  function handleScan() {
    if (!url.trim()) return;
    run(url.trim(), authorized);
  }

  const hasRun = state.phase !== "idle";

  // Offered once the analysis has actually produced findings -- i.e. from the
  // human-review pause (analysis done, audit not yet saved) onward, and still after
  // the run closes out. Purely additive: it reads state the app already holds and
  // triggers no request, so the scan/review/audit workflow is untouched.
  const canDownloadReport =
    (state.phase === "awaiting_review" || state.phase === "completed") && state.findings.length > 0;
  const reportIsProvisional = state.findings.some((f) => f.status === "pending");

  async function handleDownloadReport() {
    setReportError(null);
    setBuildingReport(true);
    try {
      await downloadReport(buildReportModel(state.scan, state.findings, state.websiteScanMeta));
    } catch (err) {
      // Surface it rather than failing silently -- the PDF library is fetched on
      // demand, so an offline/blocked load is a real possibility worth reporting.
      setReportError(err instanceof Error ? err.message : "Could not generate the PDF report.");
    } finally {
      setBuildingReport(false);
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">Consiva AI</div>
        <h1>Consent Agent</h1>
        <div className="tagline">Enter a public website URL to run a real scan → three-state consent test → rules → RAG → NVIDIA LLM → findings → human review.</div>
      </header>

      <div className="scan-form">
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://example.com"
          disabled={busy}
        />
        <label>
          <input type="checkbox" checked={authorized} onChange={(e) => setAuthorized(e.target.checked)} disabled={busy} />
          I own / am authorized to scan this domain
        </label>
        <button onClick={handleScan} disabled={busy || !url.trim()}>
          {busy ? "Scanning…" : "Scan Website"}
        </button>
      </div>
      <div className="hint">SSRF-protected — private/internal/loopback addresses are always rejected regardless of this checkbox.</div>

      {state.phase === "failed" && state.error && (
        <div className="error-box">{state.error}</div>
      )}
      {state.phase === "awaiting_review" && (
        <div className="info-box">
          Analysis complete — one or more findings require human review before the run can close out. Approve, reject,
          or edit each finding below; the pipeline resumes automatically once every pending finding has a decision.
        </div>
      )}

      {canDownloadReport && (
        <div className="report-bar">
          <div className="report-bar-text">
            <strong>One-page summary</strong>
            <span>
              Client-friendly headlines only — overall risk, main problems, key consent issues, DPDP
              references and recommended actions.
              {reportIsProvisional && " Marked provisional until every finding is reviewed."}
            </span>
            {reportError && <span className="report-error">{reportError}</span>}
          </div>
          <button className="report-btn" onClick={handleDownloadReport} disabled={buildingReport}>
            {buildingReport ? "Preparing…" : "Download Report (PDF)"}
          </button>
        </div>
      )}

      {hasRun && (
        <>
          <ScanStatusSection state={state} />
          <ConsentSummarySection meta={state.websiteScanMeta} />
          <ThreeStateSection meta={state.websiteScanMeta} trackers={state.evidence?.trackers ?? []} />

          <section className="section">
            <h2>4. Trackers</h2>
            <TrackersTable trackers={state.evidence?.trackers ?? []} />
          </section>

          <section className="section">
            <h2>5. Cookies</h2>
            <CookiesTable cookies={state.evidence?.cookies ?? []} />
          </section>

          <section className="section">
            <h2>6. Forms</h2>
            <FormsSection forms={state.evidence?.forms ?? []} />
          </section>

          <section className="section">
            <h2>7. Policies</h2>
            <PoliciesSection policies={state.evidence?.policies ?? []} />
          </section>

          <section className="section">
            <h2>8. Findings &amp; Compliance</h2>
            <FindingsSection findings={state.findings} onDecision={afterReviewDecision} />
          </section>

          <section className="section">
            <h2>9. DPDP References</h2>
            <DpdpReferencesSection findings={state.findings} />
          </section>

          <section className="section">
            <h2>10. Recommendations</h2>
            <RecommendationsSection findings={state.findings} />
          </section>

          <section className="section">
            <h2>12. Audit Timeline</h2>
            <AuditSection audit={state.audit} />
          </section>

          <section className="section">
            <h2>13. Errors / Diagnostics</h2>
            <DiagnosticsSection scanError={state.error} stages={state.stages} meta={state.websiteScanMeta} />
          </section>
        </>
      )}
    </div>
  );
}
