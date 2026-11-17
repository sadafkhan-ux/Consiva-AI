import { useEffect, useState } from "react";
import { enterpriseScans, type DocumentItem, type EvidenceItem, type EvidenceRecord, type UrlItem } from "../api/enterprise";
import { count, text, words, type Tone } from "./format";
import { useLoaded, usePaged } from "./hooks";
import { ErrorNote, PagedView, Pill, Skeleton } from "./ui";

const URL_STATUS: Record<string, Tone & { text: string }> = {
  pending: { label: "Waiting", text: "Waiting", tone: "muted" },
  in_progress: { label: "In progress", text: "In progress", tone: "info" },
  done: { label: "Crawled", text: "Crawled", tone: "ok" },
  failed: { label: "Failed", text: "Failed", tone: "bad" },
  excluded: { label: "Excluded", text: "Excluded", tone: "warn" },
};
const ANALYSIS_LABEL: Record<string, string> = { not_started: "Not started", done: "Analysed", failed: "Analysis failed", not_applicable: "Not applicable" };
const EXTRACTION: Record<string, Tone> = {
  extracted: { label: "Extracted", tone: "ok" },
  ocr_extracted: { label: "OCR extracted", tone: "ok" },
  partly_extracted: { label: "Partly extracted", tone: "warn" },
  unreadable: { label: "Unreadable", tone: "bad" },
  encrypted: { label: "Encrypted", tone: "bad" },
  unsupported: { label: "Unsupported", tone: "warn" },
  too_large: { label: "Too large", tone: "warn" },
};

function useDebounced(value: string, ms = 350): string {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function UrlsTab({ scanId, refresh }: { scanId: string; refresh: string }) {
  const [status, setStatus] = useState("");
  const [analysis, setAnalysis] = useState("");
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const p = usePaged<UrlItem>((offset, limit) => enterpriseScans.urls(scanId, { status, analysis_status: analysis, q: dq, offset, limit }), `${scanId}|${status}|${analysis}|${dq}`, 25, refresh);
  return (
    <div data-testid="urls-tab">
      <div className="inline-row" role="search">
        <label htmlFor="u-status">Status</label>
        <select id="u-status" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All</option>
          {Object.entries(URL_STATUS).map(([k, v]) => <option key={k} value={k}>{v.text}</option>)}
        </select>
        <label htmlFor="u-analysis">Analysis</label>
        <select id="u-analysis" value={analysis} onChange={(e) => setAnalysis(e.target.value)}>
          <option value="">All</option>
          {Object.entries(ANALYSIS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <label htmlFor="u-q">Search</label>
        <input id="u-q" type="search" placeholder="Part of an address" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <PagedView p={p} noun="URLs" empty="No URLs match.">
        {(items) => (
          <table className="ropa-table">
            <thead><tr><th>URL</th><th>Depth</th><th>Status</th><th>Analysis</th><th>HTTP</th><th>Reason</th></tr></thead>
            <tbody>
              {items.map((it) => {
                const st = URL_STATUS[it.status] ?? { label: words(it.status), tone: "muted" as const };
                const reason = it.status === "excluded" || it.status === "failed" ? text(it.status_reason ?? it.last_error, "No reason recorded") : text(it.status_reason ?? it.last_error, "");
                return (
                  <tr key={it.url}>
                    <td className="mono" style={{ wordBreak: "break-all" }}>
                      {it.url}
                      {it.title ? <div className="muted small" style={{ fontFamily: "inherit" }}>{it.title}</div> : null}
                      {it.text_chars != null ? (
                        <div className="muted small" style={{ fontFamily: "inherit" }} data-testid="url-evidence">
                          {count(it.text_chars)} characters read{it.category ? `, read as ${words(it.category)}` : ""}
                          {it.chunks_created != null ? `, ${it.chunks_analysed ?? 0} of ${it.chunks_created} passages analysed` : ""}
                        </div>
                      ) : null}
                      {it.also_categories && it.also_categories.length > 0 ? <div className="small" data-testid="url-also">Also holds: {it.also_categories.map((c) => words(c)).join(", ")} (as sections of this page)</div> : null}
                      {it.evidence_note ? <div className="small" data-testid="url-evidence-note">Read limited: {it.evidence_note}</div> : null}
                      {it.analysis_note ? <div className="small" data-testid="url-analysis-note">{it.analysis_note}</div> : null}
                    </td>
                    <td>{count(it.depth)}</td>
                    <td><Pill tone={st.tone}>{st.label}</Pill></td>
                    <td>{text(ANALYSIS_LABEL[it.analysis_status ?? ""] ?? words(it.analysis_status))}</td>
                    <td>{it.http_status ?? "-"}</td>
                    <td>{reason}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </PagedView>
    </div>
  );
}

export function DocumentsTab({ scanId, refresh }: { scanId: string; refresh: string }) {
  const p = usePaged<DocumentItem>((offset, limit) => enterpriseScans.documents(scanId, { offset, limit }), scanId, 25, refresh);
  return (
    <div data-testid="documents-tab">
      <PagedView p={p} noun="documents" empty="No documents were found on this site.">
        {(items) => (
          <table className="ropa-table">
            <thead><tr><th>Document</th><th>Type</th><th>Extraction</th><th>Method</th><th>Pages read</th><th>Needing OCR</th><th>Analysis</th></tr></thead>
            <tbody>
              {items.map((d) => {
                const ex = d.extraction_status ? EXTRACTION[d.extraction_status] ?? { label: words(d.extraction_status), tone: "muted" as const } : null;
                return (
                  <tr key={d.url}>
                    <td className="mono" style={{ wordBreak: "break-all" }}>{d.url}{d.status_reason ? <div className="muted small">{d.status_reason}</div> : null}</td>
                    <td>{text(d.content_type)}</td>
                    <td>{ex ? <Pill tone={ex.tone}>{ex.label}</Pill> : <span className="muted">not recorded yet</span>}</td>
                    <td>{text(words(d.extraction_method))}</td>
                    <td>{count(d.pages_read)}</td>
                    <td>{count(d.pages_needing_ocr)}</td>
                    <td>{text(ANALYSIS_LABEL[d.analysis_status ?? ""] ?? words(d.analysis_status))}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </PagedView>
    </div>
  );
}

const evId = (e: EvidenceItem) => String(e.id ?? e.evidence_id ?? "");
export const stripRef = (ref: string) => ref.replace(/^ev:/, "");

function EvidenceViewer({ scanId, evidenceId, onClose }: { scanId: string; evidenceId: string; onClose: () => void }) {
  const r = useLoaded<EvidenceRecord>(() => enterpriseScans.evidenceRecord(scanId, evidenceId), `${scanId}|${evidenceId}`);
  return (
    <section className="finding-card" data-testid="evidence-viewer" aria-label="Evidence record">
      <div className="finding-head">
        <strong>Evidence record</strong> <span className="mono small">{evidenceId}</span>{" "}
        <button type="button" className="secondary small" onClick={onClose}>Close</button>
      </div>
      {r.error ? <ErrorNote message={r.error} onRetry={r.reload} /> : !r.data ? <Skeleton rows={3} /> : (
        <>
          <p className="muted small">{words(r.data.kind)} from <span className="mono">{text(r.data.source_url)}</span></p>
          {r.data.truncated && <p className="banner banner-warn" role="note" data-testid="truncated-note">This record was cut for display. The stored record is longer.</p>}
          {/* Plain text only: React escapes it, and nothing here is ever rendered as markup. */}
          <pre className="mono" data-testid="evidence-content" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 360, overflow: "auto", margin: 0 }}>{text(r.data.content, "(no content recorded)")}</pre>
        </>
      )}
    </section>
  );
}

export function EvidenceTab({ scanId, refresh, focusId, onFocus }: { scanId: string; refresh: string; focusId: string | null; onFocus: (id: string | null) => void }) {
  const [kind, setKind] = useState("");
  const dkind = useDebounced(kind);
  const p = usePaged<EvidenceItem>((offset, limit) => enterpriseScans.evidence(scanId, { kind: dkind, offset, limit }), `${scanId}|${dkind}`, 25, refresh);
  return (
    <div data-testid="evidence-tab">
      <div className="inline-row" role="search">
        <label htmlFor="e-kind">Kind</label>
        <input id="e-kind" type="search" placeholder="For example cookie, policy_text" value={kind} onChange={(e) => setKind(e.target.value)} />
      </div>
      {focusId && <EvidenceViewer scanId={scanId} evidenceId={focusId} onClose={() => onFocus(null)} />}
      <PagedView p={p} noun="evidence records" empty="No evidence records yet.">
        {(items) => (
          <table className="ropa-table">
            <thead><tr><th>Kind</th><th>Source</th><th>Excerpt</th><th>Record</th></tr></thead>
            <tbody>
              {items.map((e, i) => (
                <tr key={evId(e) || i}>
                  <td>{words(e.kind)}</td>
                  <td className="mono" style={{ wordBreak: "break-all" }}>{text(e.source_url)}</td>
                  <td>{text(e.excerpt)}</td>
                  <td><button type="button" className="secondary small" disabled={!evId(e)} onClick={() => onFocus(evId(e))} aria-label={`View evidence ${evId(e)}`}>View</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </PagedView>
    </div>
  );
}
