import type { TenAssessment, TenResult } from "../report/tenChecks";
import { KIND_LABEL, scoreContext } from "../report/tenChecks";

const BADGE: Record<TenResult, string> = { Passed: "ok", "Partially Passed": "warn", Failed: "bad", "Not Assessed": "neutral", "Not Applicable": "neutral" };

/**
 * How the Privacy Readiness Score was reached, check by check: the result, the marks, the evidence and the page it came from, the finding, how good the evidence is, and what it does not cover.
 * It reads the same 10-check assessment (report/tenChecks.ts) the PDF prints, so the dashboard and the PDF cannot disagree about a check. Collapsed by default; the detail is one click away.
 */
export function TenChecksPanel({ ten }: { ten: TenAssessment | null }) {
  if (!ten) return null;
  const ctx = scoreContext(ten);
  const sc = ten.scanner;
  return (
    <section className="section" data-testid="ten-checks-panel">
      <details>
        <summary style={{ cursor: "pointer", fontWeight: 700 }}>{sc ? `How the score was reached (${ten.scoreText})` : `How the ten checks were marked (${ten.totalText} marks)`}</summary>
        {sc && ctx && (
          <div data-testid="scanner-context" style={{ margin: "8px 0" }}>
            <p className="small" data-testid="scanner-explanation" style={{ margin: "0 0 6px" }}>{sc.explanation}</p>
            <p style={{ margin: "0 0 6px" }}>
              <span className="badge neutral" data-testid="scanner-band">{ctx.band}</span>{" "}
              <span className="badge neutral" data-testid="scanner-coverage">Coverage {ctx.coverage}%</span>{" "}
              <span className="badge neutral" data-testid="scanner-confidence">Evidence confidence: {ctx.confidence}</span>
            </p>
            {[...sc.bandNotes, ...sc.confidence.reasons].length > 0 && <ul className="small muted" style={{ margin: "0 0 6px 18px" }}>{[...sc.bandNotes, ...sc.confidence.reasons].map((n) => <li key={n}>{n}</li>)}</ul>}
          </div>
        )}
        <p className="small muted" data-testid="ten-summary" style={{ margin: "8px 0 4px" }}>{ten.summary}</p>
        <p className="small muted" data-testid="ten-note" style={{ margin: "0 0 8px" }}>{sc ? "Points are earned parameter by parameter. The score is the share of the assessable points earned, expressed out of 100, as the server worked it out (the same figure the web page, the email and every PDF show). Parameters that are not applicable or could not be assessed are left out, with the reason." : "These marks are the Privacy Readiness Score, check by check: the score above is these marks added up, as the server worked them out (the same figure the web page, the email and every PDF show)."}</p>
        <div style={{ overflowX: "auto" }}>
          <table className="data-table">
            <thead><tr><th>{sc ? "Ref" : "#"}</th><th>Assessment</th><th>Result</th><th>Marks</th><th>Evidence and source</th><th>Finding</th></tr></thead>
            <tbody>
              {ten.checks.map((c) => (
                <tr key={c.id} data-testid={`ten-check-${c.id}`} data-status={c.result}>
                  <td>{c.ref ?? c.id}</td>
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
        {sc?.categories && (
          <p data-testid="scorecard-categories" style={{ margin: "8px 0" }}>
            {sc.categories.map((c) => <span key={c.category} className="badge neutral" style={{ marginRight: 6 }}>{c.category}: {c.awarded} of {c.assessable} marks</span>)}
          </p>
        )}
        {sc?.rows && (
          <div data-testid="scorecard-details" className="small" style={{ margin: "8px 0" }}>
            {sc.rows.filter((r) => r.subChecks.length > 0).map((r) => (
              <details key={r.id} data-testid={`details-${r.id}`}>
                <summary style={{ cursor: "pointer", fontWeight: 600 }}>{r.id} {r.name}: detailed checks ({r.subChecks.length})</summary>
                <ul style={{ margin: "4px 0 6px 18px" }}>{r.subChecks.map((c) => <li key={c.id}><span className="mono">{c.id}</span> {c.name}: {c.result}</li>)}</ul>
              </details>
            ))}
          </div>
        )}
        {sc && (sc.indicators?.length ?? 0) > 0 && (
          <div data-testid="scanner-indicators" style={{ margin: "12px 0 0", overflowX: "auto" }}>
            <b>Readiness indicators (not scored)</b>
            <p className="small muted" style={{ margin: "2px 0 6px" }}>What the notice says on matters the Act and Rules relate to a website. They carry evidence but no marks and never change the score.</p>
            <table className="data-table">
              <thead><tr><th>#</th><th>Indicator</th><th>Outcome</th><th>Evidence</th><th>Provisions</th></tr></thead>
              <tbody>
                {sc.indicators!.map((i) => (
                  <tr key={i.id} data-testid={`indicator-${i.id}`}>
                    <td>{i.id}</td>
                    <td><b>{i.name}</b></td>
                    <td><span className={`badge ${i.outcome === "Observed" ? "ok" : "neutral"}`}>{i.outcome}</span></td>
                    <td className="small">{i.found}{i.evidence[0] && <div className="muted">{i.evidence[0].observed}</div>}</td>
                    <td className="small">{i.statutory.map((s) => `${s.provision}${s.inForce ? "" : " (from " + s.commences + ")"}`).join("; ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {sc?.purposes?.tested && (
          <div data-testid="scanner-purposes" style={{ margin: "12px 0 0", overflowX: "auto" }}>
            <b>Choices by purpose</b>
            <p className="small muted" style={{ margin: "2px 0 6px" }}>Each purpose was switched on alone in a fresh browser session. Requests were observed; cookies were not read.</p>
            <table className="data-table">
              <thead><tr><th>Purpose switched on</th><th>Analytics or advertising contacted</th><th>Result</th></tr></thead>
              <tbody>
                {sc.purposes.purposes.map((p) => (
                  <tr key={p.label} data-testid={`purpose-${p.kind}`}>
                    <td><b>{p.label}</b></td>
                    <td className="small">{p.observed.length ? p.observed.map((o) => `${o.vendor ?? o.host} (${o.category})`).join(", ") : "nothing"}</td>
                    <td><span className={`badge ${p.verdict === "as_expected" ? "ok" : "bad"}`}>{p.verdict === "as_expected" ? "As expected" : "Unexpected"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {sc && sc.beyondQuickScan.length > 0 && (
          <div data-testid="scanner-beyond" className="small muted" style={{ margin: "8px 0 0" }}>
            <b>Not assessed in a Quick Scan (left out of the score; a Deep Scan assesses them):</b> {sc.beyondQuickScan.map((p) => `${p.ref} ${p.name}`).join("; ")}.
          </div>
        )}
        {sc && sc.whatWasNotAssessed.length > 0 && <p className="small muted" data-testid="scanner-not-assessed" style={{ margin: "8px 0 0" }}>{sc.whatWasNotAssessed.join(" ")}</p>}
        {sc && <p className="small muted" data-testid="scanner-disclaimer" style={{ margin: "8px 0 0" }}>{sc.disclaimer}</p>}
        {ten.limitations.length > 0 && (
          <ul className="small muted" data-testid="ten-limitations" style={{ margin: "8px 0 0 18px" }}>{ten.limitations.map((l) => <li key={l}>{l}</li>)}</ul>
        )}
      </details>
    </section>
  );
}
