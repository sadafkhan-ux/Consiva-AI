import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ScanListItem } from "../api/types";

function when(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** The organisation's recent scans, so a scan can be reopened after a reload or an interrupted page. Hidden when there are none or the list cannot be loaded. */
export function RecentScans({ busy, refreshKey, currentScanId, onOpen }: {
  busy: boolean;
  /** Changes when a scan ends, so the list picks up the new one. */
  refreshKey: string;
  currentScanId: string | null;
  onOpen: (scan: ScanListItem) => void;
}) {
  const [scans, setScans] = useState<ScanListItem[]>([]);

  useEffect(() => {
    let alive = true;
    api.listScans().then((rows) => { if (alive) setScans(rows); }).catch(() => { /* the list is a convenience; the scan form still works */ });
    return () => { alive = false; };
  }, [refreshKey]);

  if (scans.length === 0) return null;
  return (
    <section className="section" data-testid="recent-scans">
      <h3>Recent scans</h3>
      <div className="small">
        {scans.map((s) => (
          <div key={s.id} data-testid="recent-scan" style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", padding: "4px 0" }}>
            <span style={{ flex: "1 1 260px", minWidth: 0, overflowWrap: "anywhere" }}>{s.url}</span>
            <span className={`badge ${s.status === "completed" ? "ok" : s.status === "failed" ? "bad" : "warn"}`}>{s.status}</span>
            <span className="muted">{when(s.completed_at ?? s.created_at)}</span>
            <button onClick={() => onOpen(s)} disabled={busy || s.id === currentScanId}>{s.id === currentScanId ? "Shown" : "Open"}</button>
          </div>
        ))}
      </div>
    </section>
  );
}
