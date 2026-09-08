import { useMemo, useState } from "react";
import type { EvidenceTrackerItem } from "../api/types";

type SortKey = "domain" | "category" | "source" | "consent_states";

function domainOf(scriptSrc: string): string {
  try {
    return new URL(scriptSrc).hostname;
  } catch {
    return scriptSrc;
  }
}

// A real, deterministic risk read from the ALREADY-classified category/consent_states
// -- never a guess: firing pre_consent is the exact condition R-001 (the backend's own
// rule) flags as high risk; still firing post_reject is exactly R-003/R-009's territory.
function trackerRisk(t: EvidenceTrackerItem): "high" | "medium" | "low" {
  const nonEssential = t.category === "analytics" || t.category === "marketing";
  if (nonEssential && t.consent_states.includes("pre_consent")) return "high";
  if (nonEssential && t.consent_states.includes("post_reject")) return "high";
  if (nonEssential) return "medium";
  return "low";
}

export function TrackersTable({ trackers }: { trackers: EvidenceTrackerItem[] }) {
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("consent_states");
  const [sortAsc, setSortAsc] = useState(false);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    let rows = trackers;
    if (q) {
      rows = rows.filter(
        (t) =>
          t.script_src.toLowerCase().includes(q) ||
          (t.vendor ?? "").toLowerCase().includes(q) ||
          (t.category ?? "").toLowerCase().includes(q)
      );
    }
    const sorted = [...rows].sort((a, b) => {
      const av = sortKey === "domain" ? domainOf(a.script_src) : sortKey === "consent_states" ? a.consent_states.length : (a[sortKey] ?? "");
      const bv = sortKey === "domain" ? domainOf(b.script_src) : sortKey === "consent_states" ? b.consent_states.length : (b[sortKey] ?? "");
      if (av < bv) return sortAsc ? -1 : 1;
      if (av > bv) return sortAsc ? 1 : -1;
      return 0;
    });
    return sorted;
  }, [trackers, search, sortKey, sortAsc]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortAsc((a) => !a);
    else {
      setSortKey(key);
      setSortAsc(true);
    }
  }

  if (trackers.length === 0) return <p className="empty-note">No trackers were detected on this scan.</p>;

  return (
    <div>
      <div className="table-toolbar">
        <input
          type="search"
          placeholder="Search trackers by domain, vendor, or category…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <span className="count">{filtered.length} of {trackers.length}</span>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table className="data-table">
          <thead>
            <tr>
              <th onClick={() => toggleSort("domain")}>Domain</th>
              <th>Tracker (full URL)</th>
              <th onClick={() => toggleSort("category")}>Type / Purpose</th>
              <th onClick={() => toggleSort("source")}>Source</th>
              <th onClick={() => toggleSort("consent_states")}>Consent State</th>
              <th>Risk</th>
              <th>Evidence</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((t) => (
              <tr key={t.id}>
                <td>{t.vendor ?? domainOf(t.script_src)}</td>
                <td className="mono" title={t.script_src}>{t.script_src.length > 60 ? t.script_src.slice(0, 60) + "…" : t.script_src}</td>
                <td>{t.category ?? <span className="muted">unclassified</span>}</td>
                <td>{t.source ?? "—"}</td>
                <td>
                  {t.consent_states.map((s) => (
                    <span key={s} className="badge neutral" style={{ marginRight: 4 }}>{s.replace("_", " ")}</span>
                  ))}
                </td>
                <td><span className={`badge ${trackerRisk(t)}`}>{trackerRisk(t)}</span></td>
                <td className="mono" style={{ fontSize: 11 }}>{t.id.slice(0, 8)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
