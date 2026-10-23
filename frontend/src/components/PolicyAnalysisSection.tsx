import type { PolicyAnalysis } from "../api/types";
import { buildPolicyView, POLICY_BASIS_LABEL, POLICY_STATUS_LABEL } from "../report/policyAnalysis";

/** What the site's privacy and cookie policies say, with the exact words and the DPDP provisions each point rests on. Informational: the score is not changed by it. */
export function PolicyAnalysisSection({ analysis }: { analysis: PolicyAnalysis | null | undefined }) {
  const v = buildPolicyView(analysis);
  if (!v) return null;
  return (
    <section className="section" data-testid="policy-analysis">
      <h2>Privacy policy content analysis</h2>
      {!v.available ? (
        <div className="info-box" data-testid="policy-unavailable">
          <b>{v.headline}.</b> {v.reason}
          {v.notes.map((n) => <p key={n} className="small" style={{ margin: "4px 0 0" }}>{n}</p>)}
        </div>
      ) : (
        <>
          <p style={{ margin: "4px 0" }}><b data-testid="policy-headline">{v.headline}</b></p>
          <p className="small muted" data-testid="policy-summary">{v.summaryLine}.</p>
          <div style={{ overflowX: "auto" }}>
            <table className="data-table" data-testid="policy-checklist">
              <thead><tr><th>Point the policy should cover</th><th>Status</th><th>Basis</th></tr></thead>
              <tbody>
                {v.checklist.map((c) => (
                  <tr key={c.id} data-testid={`policy-check-${c.id}`}>
                    <td>{c.title}</td>
                    <td><span className={`badge ${c.statusKey === "verified" ? "ok" : c.statusKey === "partial" ? "warn" : c.statusKey === "not_found" ? "bad" : "neutral"}`}>{c.status}</span></td>
                    <td className="small muted">{c.basis}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {v.fixes.length > 0 && (
            <div className="info-box" data-testid="policy-fixes" style={{ marginTop: 10 }}>
              <b>Policy fixes, in order</b>
              <ol style={{ margin: "4px 0 0 18px" }}>
                {v.fixes.map((f) => (
                  <li key={f.row.id} className="small" data-testid={`policy-fix-${f.row.id}`}>
                    <b>{f.row.title}.</b> <span className="badge neutral">{f.kind}</span> {f.row.fix}
                    {f.impactLine && <div><b>Impact:</b> {f.impactLine}</div>}
                    <div className="muted">{f.why}</div>
                  </li>
                ))}
              </ol>
            </div>
          )}
          {v.groups.map((g) => (
            <div key={g.name} style={{ marginTop: 10 }}>
              <h3>{g.name}</h3>
              {g.rows.map((r) => (
                <div key={r.id} className="finding-card" data-testid={`policy-req-${r.id}`}>
                  <div className="finding-head">
                    <strong>{r.title}</strong>
                    <span className={`badge ${r.status === "verified" ? "ok" : r.status === "partial" ? "warn" : r.status === "not_found" ? "bad" : "neutral"}`} data-testid={`policy-status-${r.id}`}>
                      {POLICY_STATUS_LABEL[r.status] ?? r.status}
                    </span>
                    <span className="badge neutral">{POLICY_BASIS_LABEL[r.basis] ?? r.basis}</span>
                  </div>
                  <div className="muted small">{r.provisions.join(" · ")}</div>
                  {r.quote && (
                    <blockquote className="small" style={{ margin: "6px 0", paddingLeft: 10, borderLeft: "3px solid var(--border, #ccd)" }} data-testid={`policy-quote-${r.id}`}>
                      “{r.quote}”
                      {r.source_url && <div className="muted">— <a href={r.source_url} target="_blank" rel="noreferrer">{r.source_url}</a></div>}
                    </blockquote>
                  )}
                  <div className="small">{r.detail}</div>
                </div>
              ))}
            </div>
          ))}
        </>
      )}
      {v.documents.length > 0 && (
        <p className="muted small" data-testid="policy-documents">
          <b>Read:</b> {v.documents.join(" · ")}
        </p>
      )}
      <p className="muted small" data-testid="policy-limits">{v.limitations.join(" ")}</p>
    </section>
  );
}
