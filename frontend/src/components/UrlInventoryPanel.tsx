import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { UrlInventoryFilters, UrlInventoryResponse, UrlStatus } from "../api/types";

const PAGE = 50;
const STATUS_WORDS: Record<UrlStatus, string> = {
  PENDING: "Discovered, not scanned", IN_PROGRESS: "In progress", COMPLETED: "Scanned", FAILED: "Failed", PARTIALLY_SCANNED: "Partially scanned", EXCLUDED: "Excluded",
};
const BADGE: Record<UrlStatus, string> = { PENDING: "neutral", IN_PROGRESS: "neutral", COMPLETED: "ok", FAILED: "bad", PARTIALLY_SCANNED: "warn", EXCLUDED: "neutral" };
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "medium", timeZone: "UTC" }) : "–");

/**
 * Every URL the scan discovered and what became of it. The counts cover the whole inventory; the table is one page of what matches the filters. A discovered page is not a
 * scanned page, and the panel says how many of the first the scan actually reached. Scans made before the inventory existed have none, and the panel says so.
 */
export function UrlInventoryPanel({ scanId }: { scanId: string | null }) {
  const [filters, setFilters] = useState<UrlInventoryFilters>({});
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<UrlInventoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { setOffset(0); }, [filters, scanId]);
  useEffect(() => {
    if (!scanId) return;
    let live = true;
    const t = setTimeout(() => {
      api.getUrlInventory(scanId, filters, PAGE, offset)
        .then((r) => { if (live) { setData(r); setError(null); } })
        .catch((e) => { if (live) setError(e instanceof Error ? e.message : "Could not load the URL inventory."); });
    }, 250);
    return () => { live = false; clearTimeout(t); };
  }, [scanId, filters, offset]);

  if (!scanId) return null;
  const s = data?.summary;
  const set = (patch: Partial<UrlInventoryFilters>) => setFilters((f) => ({ ...f, ...patch }));
  const num = (v: string) => (v.trim() === "" ? null : Number(v));
  const filtered = !!(filters.status || filters.q || filters.failure || filters.source || filters.http_status != null || filters.depth != null);
  const tiles: [string, number | undefined, string][] = [
    ["Discovered", s?.total, "all"], ["Pending", s?.pending, "PENDING"], ["In progress", s?.in_progress, "IN_PROGRESS"], ["Scanned", s?.completed, "COMPLETED"],
    ["Failed", s?.failed, "FAILED"], ["Partially scanned", s?.partially_scanned, "PARTIALLY_SCANNED"], ["Excluded", s?.excluded, "EXCLUDED"],
  ];

  return (
    <section className="section" data-testid="url-inventory">
      <h2>URL inventory</h2>
      {error && <div className="info-box" data-testid="inventory-error">{error}</div>}
      {s && s.total === 0 && !filtered && (
        <p className="muted small" data-testid="inventory-empty">No URL inventory was recorded for this scan (scans made before it existed have none).</p>
      )}
      {s && s.total > 0 && (
        <p className="small muted" data-testid="inventory-note">
          {s.completed + s.partially_scanned} of {s.total} discovered URLs were scanned. A discovered URL is not a scanned page: pending, failed and excluded URLs have not been assessed.
        </p>
      )}
      <div data-testid="inventory-tiles" style={{ display: "flex", flexWrap: "wrap", gap: 8, margin: "8px 0" }}>
        {tiles.map(([label, n, key]) => (
          <button key={label} type="button" className="badge neutral" data-testid={`tile-${key}`} onClick={() => set({ status: key === "all" ? "" : (key as UrlStatus) })}
            aria-pressed={(filters.status || "all") === key} style={{ cursor: "pointer", padding: "6px 10px" }}>
            <b>{n ?? "–"}</b> {label}
          </button>
        ))}
      </div>
      <div data-testid="inventory-filters" style={{ display: "flex", flexWrap: "wrap", gap: 8, margin: "8px 0" }}>
        <select aria-label="Status" value={filters.status ?? ""} onChange={(e) => set({ status: e.target.value as UrlStatus | "" })}>
          <option value="">Any status</option>
          {(Object.keys(STATUS_WORDS) as UrlStatus[]).map((k) => <option key={k} value={k}>{STATUS_WORDS[k]}</option>)}
        </select>
        <input aria-label="HTTP status" inputMode="numeric" placeholder="HTTP status" value={filters.http_status ?? ""} style={{ width: 110 }} onChange={(e) => set({ http_status: num(e.target.value) })} />
        <select aria-label="Discovery source" value={filters.source ?? ""} onChange={(e) => set({ source: e.target.value })}>
          <option value="">Any source</option><option value="seed">Entered address</option><option value="link">Link</option><option value="sitemap">Sitemap</option>
        </select>
        <input aria-label="Crawl depth" inputMode="numeric" placeholder="Depth" value={filters.depth ?? ""} style={{ width: 80 }} onChange={(e) => set({ depth: num(e.target.value) })} />
        <input aria-label="URL contains" placeholder="URL contains" value={filters.q ?? ""} onChange={(e) => set({ q: e.target.value })} />
        <input aria-label="Reason contains" placeholder="Failure or exclusion reason" value={filters.failure ?? ""} onChange={(e) => set({ failure: e.target.value })} />
        <button type="button" onClick={() => setFilters({})} data-testid="inventory-clear">Clear filters</button>
      </div>
      {data && data.urls.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table className="data-table" data-testid="inventory-table">
            <thead><tr><th>URL</th><th>Title</th><th>Status</th><th>HTTP</th><th>Depth</th><th>Source</th><th>Reason</th><th>Discovered</th><th>Scanned</th></tr></thead>
            <tbody>
              {data.urls.map((u) => (
                <tr key={u.id} data-testid="inventory-row" data-status={u.scan_status}>
                  <td className="small" style={{ overflowWrap: "anywhere" }}>
                    <a href={u.original_url} target="_blank" rel="noreferrer">{u.original_url}</a>
                    {u.final_url && u.final_url !== u.original_url && <div className="muted" data-testid="inventory-final">→ {u.final_url}</div>}
                  </td>
                  <td className="small">{u.page_title ?? "–"}</td>
                  <td><span className={`badge ${BADGE[u.scan_status]}`}>{STATUS_WORDS[u.scan_status]}</span></td>
                  <td>{u.http_status ?? "–"}</td>
                  <td>{u.crawl_depth}</td>
                  <td className="small">{u.discovery_sources.join(", ") || u.discovery_source}</td>
                  <td className="small" data-testid="inventory-reason">{u.failure_reason ?? u.status_reason ?? "–"}</td>
                  <td className="small">{when(u.discovered_at)}</td>
                  <td className="small">{u.scan_completed_at ? when(u.scan_completed_at) : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && data.total === 0 && s && s.total > 0 && <p className="muted small" data-testid="inventory-no-match">No URL matches these filters.</p>}
      {data && data.total > PAGE && (
        <div className="small" style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }} data-testid="inventory-paging">
          <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
          <span>{offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}</span>
          <button type="button" disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
        </div>
      )}
    </section>
  );
}
