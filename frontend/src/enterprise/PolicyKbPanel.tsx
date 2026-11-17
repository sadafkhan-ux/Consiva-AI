import { useEffect, useState } from "react";
import { enterpriseErrorMessage, policyKb, type PolicyDocument, type PolicyKbMetrics } from "../api/enterprise";
import { count, dateOnly, text, words } from "./format";
import { Pill } from "./ui";

/** The policy knowledge base: the policy documents kept for the organisation's websites, with their versions, and the figures behind them. Every number is read from stored data. */
export function PolicyKbPanel() {
  const [m, setM] = useState<PolicyKbMetrics | null>(null);
  const [docs, setDocs] = useState<PolicyDocument[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    Promise.all([policyKb.metrics(), policyKb.documents()])
      .then(([mm, dd]) => { if (live) { setM(mm); setDocs(dd.items); } })
      .catch((e) => { if (live) setError(enterpriseErrorMessage(e)); });
    return () => { live = false; };
  }, []);

  if (error) return <div className="banner banner-error" role="alert">{error}</div>;
  if (!m || !docs) return <p className="muted">Loading the policy knowledge base…</p>;
  if (!m.documents || m.documents.versions === 0) return <p className="empty-note" data-testid="kb-empty">No policy documents are stored yet. They are added after the first completed scan.</p>;

  const cells: [string, number | string][] = [
    ["Websites monitored", m.websites_monitored], ["Documents", m.documents.active], ["Versions kept", m.documents.versions], ["New", m.sync.new], ["Changed", m.sync.changed], ["Unchanged (reused)", m.sync.unchanged],
    ["Removed", m.sync.removed], ["Embeddings generated", m.embeddings.generated], ["Embeddings reused", m.embeddings.reused], ["Awaiting processing", m.sync.awaiting_processing],
    ["Failed extractions", m.sync.extraction_failed], ["Failed synchronisations", m.sync.sync_failed], ["Retrievals", m.retrieval.queries], ["Model calls saved", m.llm.calls_saved],
    ["Tokens sent (estimate)", m.llm.tokens_sent_estimate], ["Tokens not sent (estimate)", m.llm.tokens_saved_estimate],
  ];
  return (
    <div data-testid="policy-kb">
      <div className="count-grid" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 8 }}>
        {cells.map(([label, value]) => (
          <div key={label} className="count-cell"><div className="muted small">{label}</div><strong>{typeof value === "number" ? count(value) : value}</strong></div>
        ))}
      </div>
      <p className="muted small">
        Last successful synchronisation: {m.sync.last_successful_sync ? dateOnly(m.sync.last_successful_sync) : "none yet"}. {m.llm.token_figures_are}.
      </p>
      <table className="ropa-table" data-testid="kb-documents">
        <thead><tr><th>Document</th><th>Type</th><th>Version</th><th>Passages</th><th>Last verified</th><th>State</th></tr></thead>
        <tbody>
          {docs.map((d) => (
            <tr key={d.id}>
              <td className="mono" style={{ wordBreak: "break-all" }}>{d.url}{d.title ? <div className="muted small" style={{ fontFamily: "inherit" }}>{d.title}</div> : null}</td>
              <td>{words(d.doc_type)}</td>
              <td>{d.current_version ?? "-"} of {d.versions}</td>
              <td>{count(d.chunk_count)}</td>
              <td>{d.last_verified_at ? dateOnly(d.last_verified_at) : "-"}</td>
              <td>
                {d.status === "removed" ? <Pill tone="muted">Removed</Pill> : d.stale ? <Pill tone="warn">Not re-verified</Pill> : d.extraction_status === "partial" ? <Pill tone="warn">Partly extracted</Pill> : <Pill tone="ok">Current</Pill>}
                {d.stale_reason ? <div className="small">{text(d.stale_reason)}</div> : null}
                {d.extraction_note ? <div className="muted small">{d.extraction_note}</div> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
