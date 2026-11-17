import { Fragment, useCallback, useEffect, useState } from "react";
import { ago, monitoringApi, type ErrorEvent, type ErrorSummary } from "../api/monitoring";
import { ApiError } from "../api/types";

/**
 * Error Monitoring: every important error of both agents and the platform, as recorded by the server (one event per distinct problem, with how often it happened).
 * Nothing here is a secret: the server stores no keys, tokens or personal data in an event. Acknowledging stops reminder emails but keeps the event visible; it is
 * marked resolved by itself when the failing service works again.
 */

const POLL_MS = 30_000;
const AGENT_LABEL: Record<string, string> = { enterprise: "Enterprise", five_page: "Five-page", platform: "Platform" };
const GROUPS: Array<[string, string]> = [["", "All categories"], ["api", "API"], ["rag", "RAG / policy KB"], ["embedding", "Embedding"], ["llm", "LLM"], ["database", "Database"], ["crawler", "Crawler"], ["worker", "Worker"], ["website_access", "Website blocked"], ["config", "Configuration"]];
const SEVERITY_TONE: Record<string, string> = { critical: "bad", warning: "warn", info: "muted" };
const STATUS_TONE: Record<string, string> = { open: "bad", acknowledged: "warn", resolved: "ok" };

function message(e: unknown): string {
  if (e instanceof ApiError) return e.status === 403 ? "Error monitoring is for administrators." : e.detail || e.message;
  return e instanceof Error ? e.message : String(e);
}

function Card({ label, value, tone, testId }: { label: string; value: string | number; tone?: string; testId: string }) {
  return (
    <div className="kpi" data-testid={testId}>
      <span className="kpi-label">{label}</span>
      <span className={`kpi-value${tone ? ` kpi-${tone}` : ""}${typeof value === "number" ? "" : " kpi-text"}`}>{value}</span>
    </div>
  );
}

export function ErrorMonitoring() {
  const [summary, setSummary] = useState<ErrorSummary | null>(null);
  const [items, setItems] = useState<ErrorEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [agent, setAgent] = useState("");
  const [grouping, setGrouping] = useState("");
  const [status, setStatus] = useState("");
  const [website, setWebsite] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [limit, setLimit] = useState(25);
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, l] = await Promise.all([
        monitoringApi.errorSummary(),
        monitoringApi.errors({ agent, grouping, status, website: website.trim(), since: since ? `${since}T00:00:00Z` : "", until: until ? `${until}T23:59:59Z` : "", limit }),
      ]);
      setSummary(s);
      setItems(l.items);
      setTotal(l.total);
      setError(null);
    } catch (e) {
      setError(message(e));
    }
  }, [agent, grouping, status, website, since, until, limit]);

  useEffect(() => {
    const t = window.setTimeout(() => void load(), website ? 250 : 0);
    const i = window.setInterval(() => void load(), POLL_MS);
    return () => { window.clearTimeout(t); window.clearInterval(i); };
  }, [load, website]);

  async function act(kind: "ack" | "resolve", ev: ErrorEvent) {
    setBusy(true);
    try {
      await (kind === "ack" ? monitoringApi.errorAcknowledge(ev.error_id) : monitoringApi.errorResolve(ev.error_id));
      await load();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div data-testid="error-monitoring" style={{ marginTop: 24 }}>
      <h3>Error monitoring</h3>
      <p className="muted small">Every important error of the Enterprise agent, the five-page agent and the platform. Normal validation mistakes are not listed. Critical problems are emailed once, then reminded, and a recovery email follows when they clear.</p>
      {error && <div className="banner banner-error" role="alert" data-testid="errors-error">{error}</div>}

      {summary && (
        <div className="kpi-grid" aria-label="Error figures">
          <Card label="Total errors" value={summary.total} testId="err-total" />
          <Card label="Critical" value={summary.critical} tone={summary.critical ? "bad" : undefined} testId="err-critical" />
          <Card label="API" value={summary.api} testId="err-api" />
          <Card label="RAG / policy KB" value={summary.rag} testId="err-rag" />
          <Card label="Database" value={summary.database} testId="err-database" />
          <Card label="Bot-blocked" value={summary.bot_blocked} tone={summary.bot_blocked ? "warn" : undefined} testId="err-blocked" />
          <Card label="Open" value={summary.open} tone={summary.open ? "bad" : "ok"} testId="err-open" />
          <Card label="Latest error" value={summary.latest_error_at ? ago(summary.latest_error_at) : "none"} testId="err-latest" />
          <Card label="Last recovery" value={summary.last_recovery_at ? ago(summary.last_recovery_at) : "none"} testId="err-recovery" />
        </div>
      )}

      <div className="inline-row" role="group" aria-label="Filter errors">
        <select aria-label="Agent" value={agent} onChange={(e) => { setAgent(e.target.value); setLimit(25); }}>
          <option value="">Both agents + platform</option>
          <option value="enterprise">Enterprise</option>
          <option value="five_page">Five-page</option>
          <option value="platform">Platform</option>
        </select>
        <select aria-label="Category" value={grouping} onChange={(e) => { setGrouping(e.target.value); setLimit(25); }}>
          {GROUPS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <select aria-label="Status" value={status} onChange={(e) => { setStatus(e.target.value); setLimit(25); }}>
          <option value="">Any status</option>
          <option value="open">Open</option>
          <option value="acknowledged">Acknowledged</option>
          <option value="resolved">Resolved</option>
        </select>
        <input type="search" aria-label="Website" placeholder="Website" value={website} onChange={(e) => { setWebsite(e.target.value); setLimit(25); }} />
        <label className="small">From <input type="date" aria-label="From date" value={since} onChange={(e) => setSince(e.target.value)} /></label>
        <label className="small">To <input type="date" aria-label="To date" value={until} onChange={(e) => setUntil(e.target.value)} /></label>
      </div>

      {items.length === 0 ? (
        <p className="empty-note" data-testid="no-errors">{summary?.total ? "No error matches this filter." : "No errors recorded."}</p>
      ) : (
        <div className="check-table-wrap">
          <table className="ropa-table" data-testid="error-table">
            <thead><tr><th>Error id</th><th>Agent</th><th>Category</th><th>Severity</th><th>HTTP</th><th>Message</th><th>Website</th><th>Last seen</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {items.map((ev) => (
                <Fragment key={ev.id}>
                  <tr data-testid={`error-${ev.error_id}`}>
                    <td className="mono">{ev.error_id}</td>
                    <td>{AGENT_LABEL[ev.agent] ?? ev.agent}</td>
                    <td>{ev.category.replace(/_/g, " ")}</td>
                    <td><span className={`pill pill-${SEVERITY_TONE[ev.severity] ?? "muted"}`}>{ev.severity}</span></td>
                    <td>{ev.http_status ?? "-"}</td>
                    <td style={{ maxWidth: 320, wordBreak: "break-word" }}>{ev.message}{ev.occurrences > 1 ? <span className="muted small"> ×{ev.occurrences}</span> : null}</td>
                    <td className="mono" style={{ wordBreak: "break-all" }}>{ev.website_url ?? "-"}</td>
                    <td>{ago(ev.last_seen_at)}</td>
                    <td><span className={`pill pill-${STATUS_TONE[ev.status] ?? "muted"}`}>{ev.status}</span></td>
                    <td>
                      <button type="button" className="secondary small" aria-expanded={openId === ev.id} onClick={() => setOpenId(openId === ev.id ? null : ev.id)}>{openId === ev.id ? "Hide" : "Details"}</button>
                    </td>
                  </tr>
                  {openId === ev.id && (
                    <tr data-testid={`error-detail-${ev.error_id}`}>
                      <td colSpan={10}>
                        <dl className="small" style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 12px", margin: 0 }}>
                          <dt>Module</dt><dd>{ev.module}{ev.service ? ` · ${ev.service}` : ""}</dd>
                          <dt>First seen</dt><dd>{new Date(ev.first_seen_at).toLocaleString()}</dd>
                          <dt>Request id</dt><dd className="mono">{ev.request_id ?? "-"}</dd>
                          <dt>Scan id</dt><dd className="mono">{ev.scan_id ?? "-"}</dd>
                          <dt>Temporary</dt><dd>{ev.temporary ? "Yes, it may clear by itself" : "No, it needs a fix"}</dd>
                          <dt>Email</dt><dd>{ev.notify_count} sent{ev.notify_status && ev.notify_status !== "sent" ? ` (last attempt: ${ev.notify_status})` : ""}</dd>
                          {ev.detail && <><dt>Detail</dt><dd style={{ wordBreak: "break-word" }}>{ev.detail}</dd></>}
                          {ev.suggested_steps && <><dt>What to do</dt><dd>{ev.suggested_steps}</dd></>}
                          {ev.resolved_at && <><dt>Resolved</dt><dd>{new Date(ev.resolved_at).toLocaleString()}</dd></>}
                        </dl>
                        <div className="inline-row" style={{ marginTop: 8 }}>
                          {ev.status === "open" && <button type="button" className="secondary small" disabled={busy} onClick={() => act("ack", ev)}>Acknowledge (stop reminders)</button>}
                          {ev.status !== "resolved" && <button type="button" className="secondary small" disabled={busy} onClick={() => act("resolve", ev)}>Mark resolved</button>}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {items.length < total && <button type="button" className="secondary small" style={{ marginTop: 8 }} onClick={() => setLimit((n) => n + 25)}>Show more ({total - items.length} more)</button>}
    </div>
  );
}
