// Agent 1 (Consent) UI, extracted verbatim from the previous App.tsx so the shell
// could add authentication and a second agent around it. Nothing in this view's
// behaviour changed -- only the function name and its new home.
import { useState } from "react";
import { FailureNotice, SimpleErrorNotice } from "./components/FailureNotice";
import { friendlyError } from "./errors/friendlyError";
import { evidenceStatus, failedStageName, hasPartialResults, scanEvidenceUnavailable, useConsentScan } from "./hooks/useConsentScan";
import { ScanStatusSection } from "./components/ScanStatusSection";
import { ConsentSummarySection } from "./components/ConsentSummarySection";
import { buildScoreView, isDpdpScore } from "./report/dpdpScore";
import { DpdpScoreSection } from "./components/DpdpScoreSection";
import { ScoreComparisonPanel } from "./components/ScoreComparisonPanel";
import { ThreeStateSection } from "./components/ThreeStateSection";
import { TrackersTable } from "./components/TrackersTable";
import { CookiesTable } from "./components/CookiesTable";
import { FormsSection, PoliciesSection } from "./components/FormsAndPoliciesSection";
import { FindingsSection } from "./components/FindingsSection";
import { InsightSections } from "./components/InsightSections";
import { ReadinessReview } from "./components/ReadinessReview";
import { PolicyAnalysisSection } from "./components/PolicyAnalysisSection";
import { ScanHero } from "./components/ScanHero";
import { TokenUsagePanel } from "./components/TokenUsagePanel";
import { DpdpReferencesSection, RecommendationsSection } from "./components/DpdpAndRecommendations";
import { AuditSection, DiagnosticsSection } from "./components/AuditAndDiagnostics";
import { ANALYSIS_INCOMPLETE_NOTE, untestedConsentStates } from "./report/buildReport";
import { reportModelFromState } from "./report/modelFromState";
import { buildInsightSections } from "./report/insights";
import { buildReadinessReview } from "./report/readiness";
import { downloadReport } from "./report/downloadReport";
import { fetchReportPdf, saveBlob } from "./api/client";
import { ApiError } from "./api/types";

// The stages that turn rule results into findings; if one fails, "no findings" says nothing about what the rules found.
const RULE_STAGES = new Set(["findings_generated", "rule_findings_generated"]);

export function ConsentAgentView() {
  const { state, run, afterReviewDecision } = useConsentScan();
  const [url, setUrl] = useState("https://example.com");
  const [authorized, setAuthorized] = useState(true);
  const [buildingReport, setBuildingReport] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);

  // Every terminal phase, not just the happy ones. Omitting "degraded" here left
  // the Scan button disabled forever after a run that produced findings without a
  // narrative -- a finished run that the page still treated as in flight.
  const TERMINAL_PHASES = ["idle", "completed", "degraded", "failed", "awaiting_review"];
  const busy = !TERMINAL_PHASES.includes(state.phase);

  function handleScan() {
    if (!url.trim()) return;
    run(url.trim(), authorized);
  }

  const hasRun = state.phase !== "idle";
  // A failed scan that collected nothing must not render "none detected" sections: see
  // scanEvidenceUnavailable. The status and diagnostics sections stay -- they carry the failure.
  const notScanned = scanEvidenceUnavailable(state);
  const evStatus = evidenceStatus(state);
  const partial = hasPartialResults(state);

  // Offered once the analysis has actually produced findings -- i.e. from the
  // human-review pause (analysis done, audit not yet saved) onward, and still after
  // the run closes out. Purely additive: it reads state the app already holds and
  // triggers no request, so the scan/review/audit workflow is untouched.
  // Said on the score card and in the PDF whenever the analysis step did not complete, so a score is never read as the result of a finished analysis.
  const analysisNote = state.phase === "degraded" ? ANALYSIS_INCOMPLETE_NOTE : null;
  // A finished scan always has a report, whether or not it produced findings: a clean site (no findings, a full score) is a real result
  // and the report is how it is shared. When the analysis did not complete the report says so.
  const canDownloadReport = state.phase === "awaiting_review" || state.phase === "completed" || state.phase === "degraded";
  const reportPending = state.phase === "starting" || state.phase === "scanning" || state.phase === "analyzing";
  const reportIsProvisional = state.findings.some((f) => f.status === "pending");

  async function handleDownloadReport() {
    setReportError(null);
    setBuildingReport(true);
    try {
      // The server prints the same report page the web shows (GET .../report.pdf). Only when the server cannot be reached or cannot make it (a
      // network or server error, not "not found" or "not allowed") is the PDF drawn here in the browser instead, from the same report model.
      try {
        if (!state.scanId) throw new ApiError(0, "No scan is open.");
        const { blob, filename } = await fetchReportPdf(state.scanId);
        saveBlob(blob, filename);
      } catch (serverErr) {
        const status = serverErr instanceof ApiError ? serverErr.status : 0;
        if (status !== 0 && status < 500) throw serverErr;
        await downloadReport(reportModelFromState(state));
      }
    } catch (err) {
      // Surface it rather than failing silently -- the PDF library is fetched on
      // demand, so an offline/blocked load is a real possibility worth reporting.
      setReportError(err instanceof ApiError ? err.detail : err instanceof Error ? err.message : "Could not generate the PDF report.");
    } finally {
      setBuildingReport(false);
    }
  }

  return (
    <div className="usage-layout">
      <div className="usage-main">
      <div className="agent-intro">
        <h1>Consent Agent</h1>
        <div className="tagline">Enter a public website address to scan it. We check its consent banner, cookies and trackers, and show what needs a human review.</div>
      </div>

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
        state.scan?.failure
          ? <FailureNotice failure={state.scan.failure} outcome={state.scan.outcome} />
          : <SimpleErrorNotice {...(({ message, next }) => ({ message, next }))(friendlyError(state.error))} />
      )}
      {state.phase !== "failed" && state.scan?.outcome === "PARTIALLY_COMPLETED" && <FailureNotice failure={state.scan.failure} outcome={state.scan.outcome} />}
      {state.phase === "degraded" && state.findings.length > 0 && (
        <div className="info-box" data-testid="degraded-notice">
          The findings below were produced by the deterministic rules from evidence the scan actually collected, so what
          they report was measured. The step that writes the plain-English explanation and attaches regulatory citations
          did not complete, so each finding carries its rule's own summary, cites nothing, and requires
          human review. Re-running the analysis will add the explanations; the evidence does not need re-scanning.
        </div>
      )}
      {state.phase === "degraded" && state.findings.length === 0 && RULE_STAGES.has(state.failedStage ?? "") && (
        <div className="info-box" data-testid="degraded-notice">
          <strong>The scan completed; the findings step did not.</strong> The checks and the score below are computed from
          the scan's own evidence. The step that turns rule results into findings did not complete, so
          the absence of findings here does not mean the rules found nothing. Re-running the analysis completes it.
        </div>
      )}
      {state.phase === "degraded" && state.findings.length === 0 && !RULE_STAGES.has(state.failedStage ?? "") && (
        <div className="info-box" data-testid="degraded-notice">
          <strong>The scan completed; the analysis did not.</strong> The checks and the score below are computed from the
          scan's own evidence and do not depend on the analysis step. That step, which writes the explanations, attaches
          the regulatory citations and can add model-assisted findings, did not complete. The rules matched
          nothing on the scanned pages, so there are no findings and no recommendations. That is not a statement that the
          analysis would have found nothing. Re-running the analysis completes it; the evidence does not need re-scanning.
        </div>
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
            <strong>Privacy report</strong>
            <span>
              A front page with the Privacy Readiness Score and the scan summary, then every check, finding, fix and piece of evidence.
              {reportIsProvisional && " Marked provisional until every finding is reviewed."}
            </span>
            {reportError && <span className="report-error">{reportError}</span>}
          </div>
          {state.scanId && <a className="report-link" href={`/?report=${state.scanId}`} target="_blank" rel="noreferrer" data-testid="open-web-report">Open web report</a>}
          <button className="report-btn" onClick={handleDownloadReport} disabled={buildingReport}>
            {buildingReport ? "Preparing…" : "Download Report (PDF)"}
          </button>
        </div>
      )}

      {reportPending && (
        <div className="report-bar" data-testid="report-pending">
          <div className="report-bar-text">
            <strong>Privacy report</strong>
            <span>The PDF report becomes available when the scan finishes.</span>
          </div>
          <button className="report-btn" disabled>Download Report (PDF)</button>
        </div>
      )}

      {hasRun && (
        <>
          <ScanStatusSection state={state} />
          {notScanned ? (
            <section className="section" data-testid="not-scanned">
              <h2>Results</h2>
              <div className="error-box">
                {(state.scan?.evidence_counts.pages ?? 0) > 0 ? (
                  <>
                    <strong>Results could not be loaded.</strong> The scan collected{" "}
                    {state.scan?.evidence_counts.pages} page{state.scan?.evidence_counts.pages === 1 ? "" : "s"}, but
                    this page lost its connection to the backend before the results arrived. Nothing below is
                    shown as "none detected", because the evidence exists and has not been read: this is not a clean
                    result. Check the connection, then run the scan again.
                  </>
                ) : (
                  <>
                    <strong>Not scanned — no result.</strong> The website scan failed
                    {failedStageName(state.stages) ? ` at the "${failedStageName(state.stages)}" stage` : ""} before any
                    page was collected, so nothing is known about this site's consent banner, cookies, trackers, forms
                    or policies. Zero is not shown for any of them, because nothing was measured: this is not a clean
                    result. The reason is under Errors / Diagnostics below; fix it and scan again.
                  </>
                )}
              </div>
            </section>
          ) : (
        <>
          {partial && (
            <div className="status-banner warn" role="status" data-testid="partial-results" style={{ marginBottom: 14 }}>
              <strong>Failed — Partial Results Available.</strong> The scan did not complete. What it collected before it failed is shown below and is real, but the assessment is
              incomplete: absent evidence here is not a clean result, and nothing below is the outcome of a completed scan.
            </div>
          )}
          <ScanHero score={isDpdpScore(state.dpdpScore) ? buildScoreView(state.dpdpScore) : null} siteUrl={state.scan?.url ?? ""} screenshot={state.screenshot} scanId={state.scanId} />
          <ConsentSummarySection meta={state.websiteScanMeta} />
          <DpdpScoreSection score={state.dpdpScore} analysisNote={analysisNote} />
          {isDpdpScore(state.dpdpScore) && <ScoreComparisonPanel scanId={state.scanId} />}
          <PolicyAnalysisSection analysis={state.policyAnalysis} />
          <InsightSections sections={buildInsightSections({ policy: state.policyAnalysis, meta: state.websiteScanMeta, evidence: state.evidence, sector: isDpdpScore(state.dpdpScore) ? state.dpdpScore.sector : null })} />
          <ReadinessReview review={buildReadinessReview({ siteUrl: state.scan?.url ?? "", evidence: state.evidence, meta: state.websiteScanMeta, policy: state.policyAnalysis, sector: isDpdpScore(state.dpdpScore) ? state.dpdpScore.sector : null })} />
          <ThreeStateSection meta={state.websiteScanMeta} trackers={state.evidence?.trackers ?? []} cookies={state.evidence?.cookies} />

          <section className="section">
            <h2>4. Trackers</h2>
            <p className="hint" data-testid="trackers-scope">
              Requests the scanned main-domain pages made to other services. A request shows the browser contacted a service; it
              does not show that a tracker ran or what it sent, and it is not by itself a violation.
            </p>
            <TrackersTable trackers={state.evidence?.trackers ?? []} status={evStatus} />
          </section>

          <section className="section">
            <h2>5. Cookies</h2>
            <p className="hint" data-testid="cookies-scope">
              Cookies stored while the scanned main-domain pages loaded. A cookie is not by itself a violation; what it is for decides that.
            </p>
            <CookiesTable status={evStatus} cookies={state.evidence?.cookies ?? []} untestedStates={untestedConsentStates(state.websiteScanMeta)} />
          </section>

          <section className="section">
            <h2>6. Forms</h2>
            <FormsSection forms={state.evidence?.forms ?? []} status={evStatus} />
          </section>

          <section className="section">
            <h2>7. Policies</h2>
            <PoliciesSection policies={state.evidence?.policies ?? []} status={evStatus} />
          </section>

          <section className="section">
            <h2>8. Findings &amp; Compliance</h2>
            <FindingsSection
              findings={state.findings}
              onDecision={afterReviewDecision}
              analysisFailed={state.failedStage !== null}
            />
          </section>

          <section className="section">
            <h2>9. DPDP References</h2>
            <DpdpReferencesSection findings={state.findings} analysisFailed={state.failedStage !== null} />
          </section>

          <section className="section">
            <h2>10. Recommendations</h2>
            <RecommendationsSection findings={state.findings} analysisFailed={state.failedStage !== null} />
          </section>

        </>
          )}

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
      <TokenUsagePanel scanId={state.scanId} live={busy} status={busy ? "running" : state.phase === "failed" ? "failed" : state.phase === "idle" ? "idle" : "completed"} />
    </div>
  );
}
