import type { TenAssessment, TenResult } from "../report/tenChecks";
import { KIND_LABEL } from "../report/tenChecks";

const BADGE: Record<TenResult, string> = { Passed: "ok", "Partially Passed": "warn", Failed: "bad", "Not Assessed": "neutral", "Not Applicable": "neutral" };

/**
 * How the Privacy Readiness Score was reached, check by check: the result, the marks, the evidence and the page it came from, the finding, how good the evidence is, and what it does not cover.
 * It reads the same 10-check assessment (report/tenChecks.ts) the PDF prints, so the dashboard and the PDF cannot disagree. Collapsed by default; the detail is one click away.
 */
export function TenChecksPanel({ ten }: { ten: TenAssessment | null }) {
  if (!ten) return null;
  return (
    <section className="section" data-testid="ten-checks-panel">
      <details>
        <summary style={{ cursor: "pointer", fontWeight: 700 }}>How the Privacy Readiness Score was calculated ({ten.totalText} marks, {ten.scoreText})</summary>
        <p className="small muted" data-testid="ten-summary" style={{ margin: "8px 0 4px" }}>{ten.summary}</p>
        <p className="small muted" data-testid="ten-formula" style={{ margin: "0 0 8px" }}>{ten.formula}</p>
        <div style={{ overflowX: "auto" }}>
          <table className="data-table">
            <thead><tr><th>#</th><th>Assessment</th><th>Result</th><th>Marks</th><th>Evidence and source</th><th>Finding</th></tr></thead>
            <tbody>
              {ten.checks.map((c) => (
                <tr key={c.id} data-testid={`ten-check-${c.id}`} data-status={c.result}>
                  <td>{c.id}</td>
                  <td><b>{c.name}</b><div className="small muted">{KIND_LABEL[c.kind]}{c.legal ? ` · ${c.legal}` : ""}</div></td>
                  <td><span className={`badge ${BADGE[c.result]}`}>{c.result}</span>{c.result !== "Passed" && c.result !== "Not Applicable" && <div className="small muted">{c.basis}</div>}{c.result !== "Not Applicable" && c.result !== "Not Assessed" && <div className="small muted">Evidence: {c.quality}</div>}</td>
                  <td style={{ whiteSpace: "nowrap" }}>{c.marksText}</td>
                  <td className="small">
                    {c.evidence || <span className="muted">No evidence excerpt</span>}
                    {c.sourceUrl ? <div>{/^https?:\/\//i.test(c.sourceUrl) ? <a href={c.sourceUrl} target="_blank" rel="noreferrer">{c.sourceUrl.replace(/^https?:\/\//, "").replace(/\/$/, "")}</a> : c.sourceUrl}</div> : c.id !== 10 && <div className="muted">No source URL available</div>}
                  </td>
                  <td className="small">{c.explanation}{c.coverage && <div className="muted">Coverage: {c.coverage}</div>}{c.limitation && <div className="muted">Limitation: {c.limitation}</div>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {ten.limitations.length > 0 && (
          <ul className="small muted" data-testid="ten-limitations" style={{ margin: "8px 0 0 18px" }}>{ten.limitations.map((l) => <li key={l}>{l}</li>)}</ul>
        )}
      </details>
    </section>
  );
}
