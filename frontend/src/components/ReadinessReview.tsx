import { READINESS_STATUS_TONE, type ReadinessReview as Review, type ReadinessStatus } from "../report/readiness";

const BADGE = { good: "ok", bad: "bad", warn: "warn", mute: "neutral" } as const;
const ORDER: ReadinessStatus[] = ["Passes this technical check", "Gap", "Not Found", "Unable to Verify", "Requires document review"];

/**
 * The five-page DPDP readiness review: which five pages were chosen and why, one row per check with its status, the evidence behind it and the
 * fix, and a summary (a readiness score out of 100, the five gaps that matter most, and the riskiest page). Built in report/readiness.ts from the
 * scan's own evidence and printed the same way in the PDF. A readiness check, not a finding of violation; it never changes the DPDP score.
 */
export function ReadinessReview({ review }: { review: Review }) {
  if (!review.available) {
    return (
      <section className="section" data-testid="readiness">
        <h2>Five-page DPDP readiness review</h2>
        <p className="hint" data-testid="readiness-unavailable">{review.reason}</p>
      </section>
    );
  }
  return (
    <section className="section" data-testid="readiness">
      <h2>Five-page DPDP readiness review</h2>
      <p className="hint">{review.timing}</p>

      <h3>A. The five pages</h3>
      <div style={{ overflowX: "auto" }}>
        <table className="data-table" data-testid="readiness-pages">
          <thead><tr><th>#</th><th>Page</th><th>URL</th><th>Why selected</th></tr></thead>
          <tbody>
            {review.pages.map((p) => (
              <tr key={p.n} data-testid={`readiness-page-${p.n}`}>
                <td>{p.n}</td>
                <td>{p.role}</td>
                <td style={{ wordBreak: "break-all" }}>
                  {p.url ? <a href={p.url} target="_blank" rel="noreferrer">{p.url}</a> : <span className="badge bad">NOT FOUND</span>}
                  {p.url && !p.scanned && <div className="muted small">Named but not loaded in this scan.</div>}
                </td>
                <td>{p.why}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3>B. Findings, one row per check</h3>
      <p style={{ display: "flex", gap: 6, flexWrap: "wrap", margin: "4px 0" }} data-testid="readiness-counts">
        {ORDER.map((s) => <span key={s} className={`badge ${BADGE[READINESS_STATUS_TONE[s]]}`}>{review.counts[s]} {s}</span>)}
      </p>
      <div style={{ overflowX: "auto" }}>
        <table className="data-table" data-testid="readiness-findings">
          <thead><tr><th>Page</th><th>DPDP section</th><th>Check</th><th>Status</th><th>Evidence</th><th>Fix</th></tr></thead>
          <tbody>
            {review.rows.map((r, i) => (
              <tr key={i} data-testid={`readiness-row-${i}`}>
                <td>{r.page}</td>
                <td>{r.sections}</td>
                <td>{r.check}</td>
                <td><span className={`badge ${BADGE[READINESS_STATUS_TONE[r.status]]}`} style={{ whiteSpace: "normal" }}>{r.status}</span><div className="muted small">{r.evidenceKind}</div></td>
                <td>{r.evidence}</td>
                <td>{r.fix}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3>C. Summary</h3>
      <p data-testid="readiness-score"><strong>Five-page review score:</strong> {review.scoreLine}</p>
      <h4>Top gaps, ranked by risk</h4>
      {review.topGaps.length === 0 ? (
        <p className="small" data-testid="readiness-no-gaps">No gap or missing item was found in the checks the scan could assess.</p>
      ) : (
        <ol data-testid="readiness-top-gaps">
          {review.topGaps.map((g) => (
            <li key={g.rank} className="small">
              <strong>{g.row.check}</strong> <span className={`badge ${g.riskLabel === "High" ? "bad" : g.riskLabel === "Medium" ? "warn" : "neutral"}`}>{g.riskLabel} risk</span>
              <div className="muted">{g.row.page}: {g.row.status}. Fix: {g.row.fix}</div>
            </li>
          ))}
        </ol>
      )}
      <p className="small" data-testid="readiness-riskiest">
        <strong>Highest-risk page:</strong> {review.riskiest ? `${review.riskiest.n}. ${review.riskiest.page}. ${review.riskiest.why}` : "None: no page carries a gap on what the scan could assess."}
      </p>
      {review.notes.map((n) => <p key={n} className="muted small">{n}</p>)}
      <p className="small" data-testid="readiness-closing"><em>{review.closing}</em></p>
    </section>
  );
}
