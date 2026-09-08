import { useMemo, useState } from "react";
import type { EvidenceCookieItem } from "../api/types";

export function CookiesTable({ cookies }: { cookies: EvidenceCookieItem[] }) {
  const [search, setSearch] = useState("");

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return cookies;
    return cookies.filter(
      (c) => c.name.toLowerCase().includes(q) || (c.domain ?? "").toLowerCase().includes(q) || (c.category ?? "").toLowerCase().includes(q)
    );
  }, [cookies, search]);

  if (cookies.length === 0) return <p className="empty-note">No cookies were detected on this scan.</p>;

  return (
    <div>
      <div className="table-toolbar">
        <input type="search" placeholder="Search cookies by name, domain, or category…" value={search} onChange={(e) => setSearch(e.target.value)} />
        <span className="count">{filtered.length} of {cookies.length}</span>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Cookie Name</th>
              <th>Domain</th>
              <th>Category / Purpose</th>
              <th>First-Party</th>
              <th>Consent State</th>
              <th>Source</th>
              <th>Evidence</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((c) => (
              <tr key={c.id}>
                <td className="mono">{c.name}</td>
                <td>{c.domain ?? "—"}</td>
                <td>{c.category ?? <span className="muted">unclassified</span>}</td>
                <td>{c.is_first_party == null ? "—" : c.is_first_party ? "Yes" : "No"}</td>
                <td>
                  {c.consent_states.map((s) => (
                    <span key={s} className="badge neutral" style={{ marginRight: 4 }}>{s.replace("_", " ")}</span>
                  ))}
                </td>
                <td>{c.source ?? "—"}</td>
                <td className="mono" style={{ fontSize: 11 }}>{c.id.slice(0, 8)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
