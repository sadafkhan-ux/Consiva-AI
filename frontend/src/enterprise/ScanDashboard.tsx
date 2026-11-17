import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { enterpriseDashboard, enterpriseErrorMessage, type DashboardAgent, type DashboardScan, type DashboardSummary, type RetentionStatus } from "../api/enterprise";
import { count, dateTime, text } from "./format";
import { Pill } from "./ui";

type Outcome = "all" | "completed" | "failed" | "pending" | "blocked";
type AgentFilter = "all" | DashboardAgent;
const OUTCOME_LABEL: Record<Outcome, string> = { all: "All", completed: "Completed", failed: "Failed", pending: "Pending", blocked: "Blocked" };
const OUTCOME_TONE: Record<Exclude<Outcome, "all">, "ok" | "bad" | "warn"> = { completed: "ok", failed: "bad", pending: "warn", blocked: "warn" };
const AGENT_LABEL: Record<AgentFilter, string> = { all: "Both agents", five_page: "Five-page", enterprise: "Enterprise" };
/** How often the figures refresh by themselves while a scan is still running. */
const AUTO_REFRESH_MS = 15_000;

function duration(a: string | null, b: string | null): string {
  if (!a || !b) return "-";
  const s = Math.max(0, Math.round((new Date(b).getTime() - new Date(a).getTime()) / 1000));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function Kpi({ label, value, hint, tone, active, onClick, testId, children }: {
  label: string; value: string; hint: string; tone?: "ok" | "bad" | "warn"; active?: boolean; onClick?: () => void; testId: string; children?: React.ReactNode;
}) {
  const body = (
    <>
      <span className="kpi-label">{label}</span>
      <span className={`kpi-value${tone ? ` kpi-${tone}` : ""}${/^\d[\d,]*$/.test(value) ? "" : " kpi-text"}`}>{value}</span>
      {children}
      <span className="kpi-hint">{hint}</span>
    </>
  );
  return onClick ? (
    <button type="button" className={`kpi kpi-button${active ? " kpi-active" : ""}`} onClick={onClick} aria-pressed={active} title={hint} data-testid={testId}>{body}</button>
  ) : (
    <div className="kpi" title={hint} data-testid={testId}>{body}</div>
  );
}

/**
 * The scan history of both agents and the counts behind it. It shows only what is kept for every scan (agent, website, start and finish, completed or failed, and why it failed), so it is complete
 * after the scan data has been cleared. A scan opens only while its data still exists (an Enterprise scan not yet cleared).
 */
export function ScanDashboard({ onOpen, refreshKey = "" }: { onOpen: (scanId: string) => void; refreshKey?: string }) {
  const [data, setData] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome>("all");
  const [agent, setAgent] = useState<AgentFilter>("all");
  const [q, setQ] = useState("");
  const [limit, setLimit] = useState(10);
  const [updated, setUpdated] = useState<Date | null>(null);
  const [retention, setRetention] = useState<RetentionStatus | null>(null);
  const [showRetention, setShowRetention] = useState(false);
  const seq = useRef(0);

  const load = useCallback((fresh = false) => {
    const n = ++seq.current;
    enterpriseDashboard.summary({ outcome, agent, q: q.trim() || undefined, limit, refresh: fresh })
      .then((d) => { if (n === seq.current) { setData(d); setError(null); setUpdated(new Date()); } })
      .catch((e) => { if (n === seq.current) setError(enterpriseErrorMessage(e)); });
  }, [outcome, agent, q, limit]);

  useEffect(() => { const t = setTimeout(() => load(false), q ? 250 : 0); return () => clearTimeout(t); }, [load, refreshKey, q]);

  const pending = data?.kpis.scans_pending ?? 0;
  useEffect(() => {
    if (!pending) return undefined;
    const t = setInterval(() => load(true), AUTO_REFRESH_MS);
    return () => clearInterval(t);
  }, [pending, load]);

  useEffect(() => {
    if (!showRetention || retention) return;
    enterpriseDashboard.retention().then(setRetention).catch(() => setRetention(null));
  }, [showRetention, retention]);

  const k = data?.kpis;
  const last = data?.last_scan ?? null;
  const items = data?.history.items ?? [];
  const total = data?.history.total ?? 0;
  const pick = (o: Outcome) => setOutcome((cur) => (cur === o ? "all" : o));
  const pickAgent = (a: DashboardAgent) => setAgent((cur) => (cur === a ? "all" : a));
  const counts = useMemo(() => ({ all: k?.scans_total ?? 0, completed: k?.scans_completed ?? 0, failed: k?.scans_failed ?? 0, pending: k?.scans_pending ?? 0, blocked: k?.scans_blocked ?? 0 }), [k]);

  if (error && !data) return <div className="banner banner-error" role="alert" data-testid="dashboard-error">{error} <button type="button" className="secondary small" onClick={() => load(true)}>Try again</button></div>;
  if (!data || !k) return <p className="muted" data-testid="dashboard-loading">Loading the dashboard…</p>;

  return (
    <div data-testid="scan-dashboard">
      <div className="kpi-grid" aria-label="Summary figures">
        <Kpi label="Total scans" value={count(k.scans_total)} hint="Every scan of both agents." testId="kpi-total" active={outcome === "all" && agent === "all"} onClick={() => { setOutcome("all"); setAgent("all"); }} />
        <Kpi label="Completed scans" value={count(k.scans_completed)} tone="ok" hint={data.definitions.completed} active={outcome === "completed"} onClick={() => pick("completed")} testId="kpi-completed" />
        <Kpi label="Failed scans" value={count(k.scans_failed)} tone="bad" hint={data.definitions.failed} active={outcome === "failed"} onClick={() => pick("failed")} testId="kpi-failed" />
        <Kpi label="Pending or incomplete" value={count(k.scans_pending)} tone="warn" hint={data.definitions.pending} active={outcome === "pending"} onClick={() => pick("pending")} testId="kpi-pending" />
        <Kpi label="Blocked by website" value={count(k.scans_blocked ?? 0)} tone="warn" hint={data.definitions.blocked} active={outcome === "blocked"} onClick={() => pick("blocked")} testId="kpi-blocked" />
        <Kpi label="Five-page scans" value={count(k.scans_five_page)} hint="Scans by the original five-page privacy policy agent." active={agent === "five_page"} onClick={() => pickAgent("five_page")} testId="kpi-five-page" />
        <Kpi label="Enterprise scans" value={count(k.scans_enterprise)} hint="Scans by the Enterprise agent." active={agent === "enterprise"} onClick={() => pickAgent("enterprise")} testId="kpi-enterprise" />
        {last ? (
          <Kpi label="Last scan" value={last.detail} tone={OUTCOME_TONE[last.outcome]} hint={`${AGENT_LABEL[last.agent]} · ${last.website_url} · ${dateTime(last.started_at)}`}
               onClick={last.agent === "enterprise" && !last.data_cleared ? () => onOpen(last.scan_id) : undefined} testId="kpi-last">
            <span className="kpi-sub mono">{last.website_url}</span>
          </Kpi>
        ) : <Kpi label="Last scan" value="none yet" hint="No scan has been started." testId="kpi-last" />}
      </div>

      <div className="history-head">
        <h3 style={{ margin: 0 }}>Scan history</h3>
        <div className="chips" role="group" aria-label="Filter by status">
          {(Object.keys(OUTCOME_LABEL) as Outcome[]).map((o) => (
            <button key={o} type="button" className={`chip${outcome === o ? " chip-on" : ""}`} aria-pressed={outcome === o} onClick={() => { setOutcome(o); setLimit(10); }} data-testid={`chip-${o}`}>
              {OUTCOME_LABEL[o]} <span className="chip-n">{counts[o]}</span>
            </button>
          ))}
        </div>
        <div className="chips" role="group" aria-label="Filter by agent">
          {(Object.keys(AGENT_LABEL) as AgentFilter[]).map((a) => (
            <button key={a} type="button" className={`chip${agent === a ? " chip-on" : ""}`} aria-pressed={agent === a} onClick={() => { setAgent(a); setLimit(10); }} data-testid={`agent-${a}`}>{AGENT_LABEL[a]}</button>
          ))}
        </div>
        <input type="search" aria-label="Search the history by website" placeholder="Search website" value={q} onChange={(e) => { setQ(e.target.value); setLimit(10); }} className="history-search" />
        <button type="button" className="secondary small" onClick={() => load(true)} data-testid="dashboard-refresh">Refresh</button>
      </div>
      <p className="muted small" data-testid="dashboard-updated">
        {updated ? `Updated ${updated.toLocaleTimeString()}` : ""}{pending ? " · refreshes every 15 seconds while a scan is running" : ""}{data.cached ? " · from a recent copy" : ""}
      </p>
      {error && <div className="banner banner-error" role="alert">{error}</div>}

      {items.length === 0 ? (
        <p className="empty-note" data-testid="history-empty">{outcome === "all" && agent === "all" && !q ? "No scans yet. Start one above." : "No scan matches this filter."}</p>
      ) : (
        <div className="table-scroll">
          <table className="ropa-table history-table" data-testid="scan-list">
            <thead><tr><th>Website</th><th>Agent</th><th>Status</th><th>Started</th><th>Finished</th><th>Took</th><th>Reason</th><th><span className="sr-only">Open</span></th></tr></thead>
            <tbody>
              {items.map((s: DashboardScan) => {
                const openable = s.agent === "enterprise" && !s.data_cleared;
                return (
                  <tr key={`${s.agent}-${s.scan_id}`} className={openable ? "history-row" : undefined} onClick={openable ? () => onOpen(s.scan_id) : undefined}>
                    <td className="mono" style={{ wordBreak: "break-all" }}>{s.website_url}</td>
                    <td>{AGENT_LABEL[s.agent]}</td>
                    <td><Pill tone={OUTCOME_TONE[s.outcome]}>{s.detail}</Pill></td>
                    <td>{dateTime(s.started_at)}</td>
                    <td>{s.finished_at ? dateTime(s.finished_at) : "-"}</td>
                    <td>{duration(s.started_at, s.finished_at)}</td>
                    <td className="small" style={{ maxWidth: 320, wordBreak: "break-word" }}>{s.outcome === "failed" || s.outcome === "blocked" ? text(s.reason, "No reason recorded") : <span className="muted">-</span>}</td>
                    <td>{openable ? <button type="button" className="secondary small" onClick={(e) => { e.stopPropagation(); onOpen(s.scan_id); }} aria-label={`Open scan of ${text(s.website_url)}`}>Open</button> : null}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {items.length < total && (
        <button type="button" className="secondary small" style={{ marginTop: 8 }} onClick={() => setLimit((n) => n + 10)} data-testid="history-more">Show more ({total - items.length} more)</button>
      )}

      <div style={{ marginTop: 16 }}>
        <button type="button" className="linklike" aria-expanded={showRetention} onClick={() => setShowRetention((v) => !v)} data-testid="retention-toggle">
          {showRetention ? "Hide" : "Show"} what is kept
        </button>
        {showRetention && (retention ? (
          <div className="retention" data-testid="retention-panel">
            <p className="small"><strong>Kept for every scan:</strong> {retention.keeps}.</p>
            <p className="small"><strong>Cleared:</strong> {retention.clears}.</p>
            <p className="small muted">Cleared {retention.when}.</p>
          </div>
        ) : <p className="muted small">Loading…</p>)}
      </div>
    </div>
  );
}
