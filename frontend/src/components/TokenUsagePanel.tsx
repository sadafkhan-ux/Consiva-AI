import { useState } from "react";
import type { ScanUsage, UsageStage } from "../api/types";
import { useScanUsage, useUsageHistory } from "../hooks/useScanUsage";

// The token usage panel: the right-hand side of the Consent Agent console. It shows the model usage of the scan on screen as the provider reported it, by stage,
// by page where a call read exactly one page, the cost, and the usage of earlier scans. It lives in the console only: nothing here is part of the report or the PDF.
//
// Three kinds of value, always marked: MEASURED (the provider's own count), CALCULATED (cost = measured tokens x a verified price) and UNAVAILABLE (not recorded).
// The server never estimates a token count, so "estimated" is a label the legend explains and nothing is drawn with it.

export const fmtTokens = (n: number | null | undefined): string => (n === null || n === undefined ? "—" : n.toLocaleString("en"));

export function fmtCost(n: number | null | undefined, currency = "USD"): string {
  if (n === null || n === undefined) return "—";
  const symbol = currency === "USD" ? "$" : `${currency} `;
  return `${symbol}${n < 0.1 ? n.toFixed(5) : n.toFixed(4)}`;
}

const when = (iso: string | null): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })} ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" })} UTC`;
};

const path = (url: string): string => {
  try {
    const u = new URL(url);
    return `${u.pathname === "/" ? "Homepage" : u.pathname}${u.search}`;
  } catch {
    return url;
  }
};

function Mark({ kind }: { kind: "measured" | "calculated" | "unavailable" | "stage_records" }) {
  const text = { measured: "Measured", calculated: "Calculated", unavailable: "Unavailable", stage_records: "From stage records" }[kind];
  return <span className={`usage-mark usage-mark-${kind}`} data-testid={`usage-mark-${kind}`}>{text}</span>;
}

function Stat({ label, value, sub, testid }: { label: string; value: string; sub?: string; testid: string }) {
  return (
    <div className="usage-stat" data-testid={testid}>
      <div className="usage-stat-value">{value}</div>
      <div className="usage-stat-label">{label}</div>
      {sub && <div className="usage-stat-sub">{sub}</div>}
    </div>
  );
}

function StageRow({ s, source }: { s: UsageStage; source: string }) {
  const unavailable = s.kind === "llm" && s.total_tokens === null;
  return (
    <tr className={`usage-stage usage-stage-${s.kind}${s.status === "running" ? " running" : ""}`} data-testid={`usage-stage-${s.stage}`}>
      <td>
        <div className="usage-stage-name">
          {s.status === "running" && <span className="usage-dot" title="Running" />}
          {s.label}
        </div>
        <div className="usage-stage-sub">
          {s.kind === "non_llm" ? "Not an LLM stage" : s.kind === "embedding" ? "Embedding model, not an LLM" : s.models.join(", ") || (s.status === "running" ? "waiting for the model" : "model not recorded")}
          {s.failed_calls > 0 && <span className="usage-failed"> · {s.failed_calls} failed</span>}
        </div>
        {s.kind === "embedding" && s.embedding_tokens !== null && <div className="usage-stage-sub" data-testid="usage-embedding">Embedding tokens: {fmtTokens(s.embedding_tokens)} (measured)</div>}
        {s.kind === "llm" && source === "stage_records" && s.total_tokens !== null && <div className="usage-stage-sub">from the stage record</div>}
      </td>
      <td className="num">{s.kind === "non_llm" ? "0" : unavailable ? "—" : s.calls}</td>
      <td className="num">{s.kind === "non_llm" ? "0" : fmtTokens(s.input_tokens)}</td>
      <td className="num">{s.kind === "non_llm" ? "0" : fmtTokens(s.output_tokens)}</td>
      <td className="num" title={unavailable ? "No usage was recorded for this stage" : undefined}>{s.kind === "non_llm" ? "0" : fmtTokens(s.total_tokens)}</td>
      <td className="num">{s.kind === "llm" ? fmtCost(s.cost) : "—"}</td>
    </tr>
  );
}

function Usage({ u, live }: { u: ScanUsage; live: boolean }) {
  const t = u.totals;
  const measured = u.source === "measured" ? "measured" : u.source === "stage_records" ? "stage_records" : "unavailable";
  const hasCache = t.cache_read_tokens > 0 || t.cache_write_tokens > 0;
  const pages = u.pages;
  return (
    <>
      <div className="usage-summary" data-testid="usage-summary">
        <Stat testid="usage-total" label="Total tokens" value={u.source === "none" ? "—" : fmtTokens(t.total_tokens)} sub="input + output + cache" />
        <Stat testid="usage-calls" label={t.calls === 1 ? "AI call" : "AI calls"} value={u.source === "none" ? "—" : String(t.calls)} sub={t.failed_calls ? `${t.failed_calls} failed` : "none failed"} />
        <Stat testid="usage-input" label="Input tokens" value={u.source === "none" ? "—" : fmtTokens(t.input_tokens)} />
        <Stat testid="usage-output" label="Output tokens" value={u.source === "none" ? "—" : fmtTokens(t.output_tokens)} />
        <Stat testid="usage-cached" label="Cached input" value={u.source === "none" ? "—" : hasCache ? `${fmtTokens(t.cache_read_tokens)} read` : "none used"} sub={t.cache_write_tokens ? `${fmtTokens(t.cache_write_tokens)} written` : undefined} />
        <Stat testid="usage-reasoning" label="Reasoning" value={t.reasoning_tokens === null ? "—" : fmtTokens(t.reasoning_tokens)} sub={t.reasoning_tokens === null ? "not reported" : "inside output"} />
      </div>
      <div className="usage-source" data-testid="usage-source">
        <Mark kind={measured} />
        {live && <span className="usage-live" data-testid="usage-live"><span className="usage-dot" /> updating</span>}
        {t.embedding_tokens !== null && <span className="usage-muted"> · embedding {fmtTokens(t.embedding_tokens)} tokens (not LLM)</span>}
      </div>

      <h4>By stage</h4>
      <div className="usage-table-wrap">
        <table className="usage-table" data-testid="usage-stages">
          <thead><tr><th>Stage</th><th className="num">Calls</th><th className="num">In</th><th className="num">Out</th><th className="num">Total</th><th className="num">Cost</th></tr></thead>
          <tbody>{u.stages.map((s) => <StageRow key={s.stage} s={s} source={u.source} />)}</tbody>
        </table>
      </div>

      <details className="usage-pages" data-testid="usage-pages">
        <summary>By page{u.pages_scanned !== null ? ` · ${u.pages_scanned} scanned` : ""}</summary>
        {pages.attributed.length === 0 && pages.shared.length === 0 && !pages.unattributed && <p className="usage-muted">No model call has been recorded yet.</p>}
        {pages.attributed.length > 0 && (
          <table className="usage-table">
            <thead><tr><th>Page</th><th className="num">Calls</th><th className="num">In</th><th className="num">Out</th><th className="num">Total</th></tr></thead>
            <tbody>
              {pages.attributed.map((r) => (
                <tr key={r.url} data-testid="usage-page-row"><td className="usage-url" title={r.url}>{path(r.url)}</td><td className="num">{r.calls}</td><td className="num">{fmtTokens(r.input_tokens)}</td><td className="num">{fmtTokens(r.output_tokens)}</td><td className="num">{fmtTokens(r.total_tokens)}</td></tr>
              ))}
            </tbody>
          </table>
        )}
        {pages.shared.map((r) => (
          <div className="usage-shared" key={r.label} data-testid="usage-shared-row">
            <div><b>Shared:</b> {r.label} <span className="usage-muted">· {r.calls} call{r.calls === 1 ? "" : "s"} · {fmtTokens(r.total_tokens)} tokens</span></div>
            <div className="usage-muted">{r.reason}</div>
            <ul>{r.page_urls.map((p) => <li key={p} title={p}>{path(p)}</li>)}</ul>
          </div>
        ))}
        {pages.unattributed && (
          <div className="usage-shared" data-testid="usage-unattributed-row">
            <div><b>Unattributed:</b> {pages.unattributed.calls} call{pages.unattributed.calls === 1 ? "" : "s"} · {fmtTokens(pages.unattributed.total_tokens)} tokens</div>
            <div className="usage-muted">{pages.unattributed.reason}</div>
          </div>
        )}
      </details>

      <h4>Cost <Mark kind={u.cost.total === null ? "unavailable" : "calculated"} /></h4>
      <dl className="usage-cost" data-testid="usage-cost">
        <dt>Input</dt><dd>{fmtCost(u.cost.input, u.cost.currency)}</dd>
        <dt>Cached input</dt><dd>{fmtCost(u.cost.cache_read === null ? null : u.cost.cache_read + (u.cost.cache_write ?? 0), u.cost.currency)}</dd>
        <dt>Output</dt><dd>{fmtCost(u.cost.output, u.cost.currency)}</dd>
        <dt className="total">Total AI cost ({u.cost.currency})</dt><dd className="total" data-testid="usage-cost-total">{fmtCost(u.cost.total, u.cost.currency)}</dd>
      </dl>
      {u.cost.unpriced_calls > 0 && <p className="usage-muted" data-testid="usage-unpriced">{u.cost.unpriced_calls} call(s) used a model with no verified price: tokens counted, cost unavailable.</p>}
      {u.cost.price_source && <p className="usage-muted small">Price: {u.cost.price_source}. Calculated from the measured tokens; infrastructure (browser, database, PDF) is not included.</p>}
      {u.notes.map((n) => <p key={n} className="usage-muted small">{n}</p>)}
    </>
  );
}

/** `scanId` is the scan open in the console; `live` is true while it runs; `status` is the console's own phase word for the chip. */
export function TokenUsagePanel({ scanId, live, status }: { scanId: string | null; live: boolean; status: "idle" | "running" | "completed" | "failed" }) {
  const [open, setOpen] = useState(true);
  const [viewing, setViewing] = useState<string | null>(null);   // a scan chosen from the history; null follows the open scan
  const selected = viewing ?? scanId;
  const liveHere = live && (viewing === null || viewing === scanId);
  const { usage, error } = useScanUsage(selected, liveHere);
  const { items, error: historyError } = useUsageHistory(`${scanId ?? ""}:${live ? "live" : "idle"}`);
  const chip = viewing && viewing !== scanId ? (usage?.state ?? "completed") : status === "idle" ? (usage?.state ?? "idle") : status;

  if (!open) {
    return <aside className="usage-panel collapsed" data-testid="usage-panel"><button className="usage-toggle" onClick={() => setOpen(true)} data-testid="usage-open">Token usage ‹</button></aside>;
  }
  return (
    <aside className="usage-panel" data-testid="usage-panel" aria-label="Token usage">
      <div className="usage-head">
        <h3>Token usage</h3>
        <span className={`usage-chip usage-chip-${chip}`} data-testid="usage-status">{chip === "running" ? "Scan running" : chip === "completed" ? "Scan finished" : chip === "failed" ? "Scan failed" : "No scan"}</span>
        <button className="usage-toggle" onClick={() => setOpen(false)} aria-label="Hide the usage panel">›</button>
      </div>
      {viewing && viewing !== scanId && (
        <div className="usage-viewing" data-testid="usage-viewing">
          Viewing an earlier scan{usage ? `: ${usage.domain}` : ""}. <button className="usage-link" onClick={() => setViewing(null)}>Back to the current scan</button>
        </div>
      )}
      {usage && usage.current_stage && chip === "running" && <div className="usage-current" data-testid="usage-current-stage"><span className="usage-dot" /> {usage.current_stage}</div>}
      {selected && usage && <div className="usage-scan">{usage.domain} · {usage.scan_id.slice(0, 8)}</div>}
      {selected && !usage && !error && <p className="usage-muted" data-testid="usage-loading">Reading the usage…</p>}
      {error && <p className="usage-error" data-testid="usage-error">{error}</p>}
      {!selected && <p className="usage-muted" data-testid="usage-empty">Run a scan to see its model usage here, or open an earlier scan below.</p>}
      {usage && <Usage u={usage} live={liveHere} />}

      <h4>Earlier scans</h4>
      {historyError && <p className="usage-error">{historyError}</p>}
      {items && items.length === 0 && <p className="usage-muted">No scans yet.</p>}
      {!items && !historyError && <p className="usage-muted">Reading the history…</p>}
      <ul className="usage-history" data-testid="usage-history">
        {(items ?? []).map((h) => (
          <li key={h.scan_id} className={selected === h.scan_id ? "current" : ""} data-testid="usage-history-row">
            <div className="usage-history-top"><b>{h.domain}</b><span className={`usage-chip usage-chip-${h.status}`}>{h.status}</span></div>
            <div className="usage-muted small">{when(h.created_at)} · {h.scan_id.slice(0, 8)} · {h.pages_scanned ?? "—"} pages</div>
            <div className="usage-history-nums">
              {h.source === "none" ? <span className="usage-muted">{h.analysis_run ? "usage not recorded" : "no AI analysis run"}</span> : (
                <>
                  <span>{h.calls} {h.calls === 1 ? "call" : "calls"}</span><span>{fmtTokens(h.input_tokens)} in</span><span>{fmtTokens(h.output_tokens)} out</span><span>{fmtTokens(h.total_tokens)} total</span>
                  <span>{h.cost === null ? "cost unavailable" : `${fmtCost(h.cost, h.currency)}${h.cost_complete ? "" : "+"}`}</span>
                </>
              )}
            </div>
            {h.source === "stage_records" && <div className="usage-muted small">from stage records (no per-call detail)</div>}
            <button className="usage-link" onClick={() => setViewing(h.scan_id === scanId ? null : h.scan_id)} data-testid="usage-history-open">Open usage</button>
          </li>
        ))}
      </ul>
      <p className="usage-legend small" data-testid="usage-legend">
        <Mark kind="measured" /> the provider's own count · <Mark kind="calculated" /> measured tokens × a verified price · <Mark kind="unavailable" /> not recorded. Token counts are never estimated.
      </p>
    </aside>
  );
}
