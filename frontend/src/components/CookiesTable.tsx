import type { EvidenceStatus } from "../hooks/useConsentScan";
import { EvidenceNotice } from "./EvidenceNotice";
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
    ["Purpose", row.purpose],
    ["Lifetime", row.lifetime],
    ["Expiry", row.expiry],
    ["Secure", row.secure],
    ["HttpOnly", row.httpOnly],
    ["SameSite", row.sameSite],
    ["Evidence ID", row.id],
  ];
  return (
    <tr className="cookie-detail-row">
      <td colSpan={11}>
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

export function CookiesTable({ cookies, untestedStates = [], status = "loaded" }: { cookies: EvidenceCookieItem[]; untestedStates?: readonly string[]; status?: EvidenceStatus }) {
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const inventory = useMemo(() => buildCookieInventory(cookies, untestedStates), [cookies, untestedStates]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return inventory.rows;
    return inventory.rows.filter((r) => r.searchText.includes(q));
  }, [inventory, search]);

  if (status !== "loaded") return <EvidenceNotice status={status} what="cookies" />;
  if (cookies.length === 0) return <p className="empty-note">No cookies were detected on this scan.</p>;

  const toggle = (id: string) => setExpanded((cur) => (cur === id ? null : id));

  return (
    <div>
      <h3 className="cookie-found-heading">Cookies found</h3>
      <p className="cookie-found-total">
        Total unique cookies: <strong>{inventory.total}</strong>
      </p>

      <div className="cookie-tiles" data-testid="cookie-summary" style={{ display: "flex", gap: 10, flexWrap: "wrap", margin: "8px 0" }}>
        {([
          ["Cookies found", String(inventory.summary.found)],
          ["Third-party", String(inventory.summary.thirdParty)],
          ["Set before consent", String(inventory.summary.beforeConsent)],
          ["Long-lived (over a year)", String(inventory.summary.longLived)],
          ["Seen after Reject", inventory.summary.afterReject === null ? "Not tested" : String(inventory.summary.afterReject)],
        ] as const).map(([label, value]) => (
          <div key={label} className="section" style={{ margin: 0, padding: "8px 14px", minWidth: 110 }}>
            <div style={{ fontSize: 20, fontWeight: 700 }}>{value}</div>
            <div className="muted small">{label}</div>
          </div>
        ))}
      </div>
      <p className="muted small" data-testid="cookie-lifetime-note">
        Long-lived means the cookie expires more than a year after it was set. The Act and Rules set no limit on a cookie's lifetime, so this is information, not a finding.
        A purpose is given only for a recognised cookie, in its provider's own words; for any other the scan says so rather than guessing.
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
              <th>Category</th>
              <th>Provider</th>
              <th>Purpose</th>
              <th>Lifetime</th>
              <th>Before consent</th>
              <th>After Accept</th>
              <th>After Reject</th>
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
                  <td>{r.provider === "Not identified" ? <span className="muted">{r.provider}</span> : r.provider}</td>
                  <td className="small" data-testid={`cookie-purpose-${r.id}`}>
                    {r.purpose}
                    {r.purposeSource !== "catalogue" && <span className="muted"> ({r.purposeSource === "category" ? "from its classification only" : "not identified"})</span>}
                  </td>
                  <td>{r.lifetime}{r.longLived && <span className="badge warn" style={{ marginLeft: 4 }}>long-lived</span>}</td>
                  {(["before", "accept", "reject"] as const).map((k) => (
                    <td key={k} data-testid={`cookie-${k}-${r.id}`}>
                      <span className={`badge ${r.behaviour[k] === "Set" ? "warn" : "neutral"}`}>{r.behaviour[k]}</span>
                    </td>
                  ))}
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
