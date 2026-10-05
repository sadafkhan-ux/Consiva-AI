import { Fragment, useMemo, useState } from "react";
import type { EvidenceCookieItem } from "../api/types";
import { buildCookieInventory, NOT_AVAILABLE, type CookieRow, type GroupCount } from "../report/cookieInventory";

function Missing({ value }: { value: string }) {
  return value === NOT_AVAILABLE ? <span className="muted">{value}</span> : <>{value}</>;
}

function GroupTable({ title, groups }: { title: string; groups: GroupCount[] }) {
  return (
    <div className="cookie-group">
      <h4>{title}</h4>
      <table className="data-table">
        <tbody>
          {groups.map((g) => (
            <tr key={g.label}>
              <td><Missing value={g.label} /></td>
              <td className="num">{g.count}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The complete evidence for one cookie -- every field, shown when a row is expanded. */
function CookieDetail({ row }: { row: CookieRow }) {
  const fields: [string, string][] = [
    ["Path", row.path],
    ["Vendor / service", row.vendor],
    ["Expiry", row.expiry],
    ["Secure", row.secure],
    ["HttpOnly", row.httpOnly],
    ["SameSite", row.sameSite],
    ["Evidence ID", row.id],
  ];
  return (
    <tr className="cookie-detail-row">
      <td colSpan={6}>
        <div className="cookie-detail">
          <div className="cookie-detail-fields">
            {fields.map(([label, value]) => (
              <div key={label} className="cookie-detail-field">
                <div className="muted small">{label.toUpperCase()}</div>
                <div className="mono"><Missing value={value} /></div>
              </div>
            ))}
          </div>
          <div className="muted small" style={{ marginTop: 10, marginBottom: 4 }}>
            CONSENT STATE · EXACT PAGE · DETECTION METHOD · TIMESTAMP · SOURCE REQUEST
          </div>
          {row.sightings.map((s) => (
            <div key={s.state} className="cookie-sighting">
              <span className="badge neutral">{s.stateLabel}</span>
              <div className="mono">
                {s.page === NOT_AVAILABLE ? <span className="muted">Exact page: {NOT_AVAILABLE}</span> : s.page}
              </div>
              {s.candidatePages.length > 0 && (
                <div className="muted small">
                  First appeared while these pages loaded together: {s.candidatePages.join(", ")}
                </div>
              )}
              <div className="small">Detected: <Missing value={s.detectedAt} /></div>
              <div className="muted small">
                Detection method: {s.how}
                {s.sourceRequest && <> — source request: <span className="mono">{s.sourceRequest}</span></>}
              </div>
            </div>
          ))}
        </div>
      </td>
    </tr>
  );
}

export function CookiesTable({ cookies }: { cookies: EvidenceCookieItem[] }) {
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const inventory = useMemo(() => buildCookieInventory(cookies), [cookies]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return inventory.rows;
    return inventory.rows.filter((r) => r.searchText.includes(q));
  }, [inventory, search]);

  if (cookies.length === 0) return <p className="empty-note">No cookies were detected on this scan.</p>;

  const toggle = (id: string) => setExpanded((cur) => (cur === id ? null : id));

  return (
    <div>
      <h3 className="cookie-found-heading">Cookies found</h3>
      <p className="cookie-found-total">
        Total unique cookies: <strong>{inventory.total}</strong>
      </p>

      <div className="cookie-summary">
        <GroupTable title="By consent state" groups={inventory.byState} />
        <GroupTable title="By purpose / category" groups={inventory.byCategory} />
        <GroupTable title="By domain" groups={inventory.byDomain} />
      </div>
      <p className="muted cookie-note">
        A cookie seen in more than one consent state counts once in the total and once in each state it was seen in.
        "{NOT_AVAILABLE}" means the scan did not record that value; nothing is filled in by guesswork. Click a cookie
        to see its complete evidence.
      </p>

      <div className="table-toolbar">
        <input type="search" placeholder="Search cookies by name, domain, category, vendor, or page…" value={search} onChange={(e) => setSearch(e.target.value)} />
        <span className="count">{filtered.length} of {inventory.total}</span>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table className="data-table cookie-table">
          <thead>
            <tr>
              <th>Cookie Name</th>
              <th>Domain</th>
              <th>Party</th>
              <th>Category / Purpose</th>
              <th>Consent State(s)</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <Fragment key={r.id}>
                <tr
                  className="cookie-row"
                  onClick={() => toggle(r.id)}
                  role="button"
                  tabIndex={0}
                  aria-expanded={expanded === r.id}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      toggle(r.id);
                    }
                  }}
                >
                  <td className="mono">{r.name}</td>
                  <td><Missing value={r.domain} /></td>
                  <td><Missing value={r.party} /></td>
                  <td>{r.category === "Unclassified" ? <span className="muted">unclassified</span> : r.category}</td>
                  <td>
                    {r.states.map((s) => (
                      <span key={s} className="badge neutral" style={{ marginRight: 4 }}>
                        {s.replace("_", " ")}
                      </span>
                    ))}
                  </td>
                  <td className="cookie-row-chevron muted">{expanded === r.id ? "▲" : "▼"}</td>
                </tr>
                {expanded === r.id && <CookieDetail row={r} />}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
