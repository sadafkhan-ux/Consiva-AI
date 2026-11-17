import { useCallback, useEffect, useState } from "react";
import { ago, monitoringApi, type AlertRow, type MonitoringOverview } from "../api/monitoring";
import { ApiError } from "../api/types";
import { ErrorMonitoring } from "./ErrorMonitoring";

/**
 * System health: whether the things the scanner depends on are working, which problems are open, and who is emailed about them.
 *
 * It shows what the server's own checks found. An alert stays here until the check passes again or someone acknowledges it; acknowledging stops reminder emails but keeps the alert
 * visible, so a known problem is never hidden. Email is shown as working only when the server says a mail server is set up, and the test button proves it with a real message.
 */

const POLL_MS = 30_000;
const STATUS_TONE: Record<string, string> = { ok: "ok", failing: "bad", unknown: "muted" };
const SEVERITY_TONE: Record<string, string> = { critical: "bad", warning: "warn" };

function message(e: unknown): string {
  if (e instanceof ApiError) return e.status === 403 ? "The system health screen is for administrators." : e.detail || e.message;
  return e instanceof Error ? e.message : String(e);
}

export function SystemHealth() {
  const [data, setData] = useState<MonitoringOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await monitoringApi.overview());
      setError(null);
    } catch (e) {
      setError(message(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(t);
  }, [load]);

  async function runNow() {
    setBusy(true);
    setNote(null);
    try {
      setData(await monitoringApi.runNow());
      setError(null);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  async function testEmail() {
    setBusy(true);
    setNote(null);
    try {
      const r = await monitoringApi.testEmail();
      setNote(r.status === "sent" ? { ok: true, text: "Test email sent. Check the inbox (and the spam folder)." } : { ok: false, text: r.reason ?? "The test email was not sent." });
    } catch (e) {
      setNote({ ok: false, text: message(e) });
    } finally {
      setBusy(false);
    }
  }

  async function acknowledge(a: AlertRow) {
    setBusy(true);
    try {
      await monitoringApi.acknowledge(a.id);
      await load();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  if (error && !data) return <div className="section" data-testid="system-error"><h2>System health</h2><div className="banner banner-error" role="alert">{error}</div></div>;
  if (!data) return <div className="section muted">Loading…</div>;

  const live = data.alerts.filter((a) => a.status !== "resolved");
  const resolved = data.alerts.filter((a) => a.status === "resolved");
  const failing = data.health.filter((h) => h.status === "failing").length;

  return (
    <div className="section" data-testid="system-health">
      <h2>System health</h2>
      <p className="muted">
        Checks run automatically every {Math.round(data.check_interval_seconds / 60)} minutes: the Claude and NVIDIA keys, the database, stuck background jobs and failing AI calls. A new problem is
        recorded once, emailed, and shown here until it is fixed.
        {!data.monitoring_enabled && " Monitoring is switched off (ALERT_ENABLED=false), so nothing is being checked."}
      </p>

      {data.email.configured ? (
        <div className="banner banner-ok" data-testid="email-status">
          Alert emails go to <b>{data.email.to.join(", ")}</b>{data.email.cc.length > 0 && <> with <b>{data.email.cc.join(", ")}</b> in copy</>}.
        </div>
      ) : (
        <div className="banner banner-warn" role="status" data-testid="email-status">
          <b>Alert emails are not being sent.</b> {data.email.problem} Problems are still recorded and shown on this screen.
        </div>
      )}

      <div className="inline-row">
        <button className="secondary small" disabled={busy} onClick={runNow}>Run checks now</button>
        <button className="secondary small" disabled={busy || !data.email.configured} onClick={testEmail} title={data.email.configured ? "Send a real test message" : "Set up the mail server first"}>Send test email</button>
        <span className="muted">{failing === 0 ? "All checks passing." : `${failing} check${failing === 1 ? "" : "s"} failing.`}</span>
      </div>
      {note && <div className={`banner ${note.ok ? "banner-ok" : "banner-error"}`} role="status" data-testid="email-note">{note.text}</div>}
      {error && <div className="banner banner-error" role="alert">{error}</div>}

      <h3>Open problems ({live.length})</h3>
      {live.length === 0 ? (
        <p className="empty-note" data-testid="no-alerts">Nothing needs attention.</p>
      ) : (
        <div data-testid="alert-list">
          {live.map((a) => (
            <div key={a.id} className="finding-card" data-testid={`alert-${a.source}`}>
              <div className="finding-head">
                <span className={`pill pill-${SEVERITY_TONE[a.severity]}`}>{a.severity}</span>{" "}
                <strong>{a.title}</strong>{" "}
                {a.status === "acknowledged" && <span className="pill pill-muted">acknowledged</span>}
              </div>
              <p>{a.detail}</p>
              {a.action && <p><b>What to do:</b> {a.action}</p>}
              <p className="muted small">
                Open since {ago(a.first_seen_at)} · seen in {a.occurrences} check{a.occurrences === 1 ? "" : "s"} · {a.notify_count} email{a.notify_count === 1 ? "" : "s"} sent
                {a.notify_status && a.notify_status !== "sent" ? ` (last attempt: ${a.notify_status})` : ""}
              </p>
              {a.status === "open" && <button className="secondary small" disabled={busy} onClick={() => acknowledge(a)}>Acknowledge (stop reminders)</button>}
            </div>
          ))}
        </div>
      )}

      <ErrorMonitoring />

      <h3>Checks</h3>
      <div className="check-table-wrap">
        <table className="ropa-table" data-testid="health-table">
          <thead><tr><th>Check</th><th>Status</th><th>Detail</th><th>Last checked</th></tr></thead>
          <tbody>
            {data.health.length === 0 && <tr><td colSpan={4} className="muted">No check has run yet. Press Run checks now.</td></tr>}
            {data.health.map((h) => (
              <tr key={h.source}>
                <td>{h.label}</td>
                <td><span className={`pill pill-${STATUS_TONE[h.status] ?? "muted"}`}>{h.status === "ok" ? "OK" : h.status === "failing" ? "Failing" : "Unknown"}</span></td>
                <td>{h.detail}{h.latency_ms !== null ? ` (${h.latency_ms} ms)` : ""}</td>
                <td>{ago(h.checked_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {resolved.length > 0 && (
        <details style={{ marginTop: 16 }}>
          <summary>Recently fixed ({resolved.length})</summary>
          {resolved.map((a) => (
            <p key={a.id} className="muted small">{a.title}: fixed {ago(a.resolved_at)} (was open {ago(a.first_seen_at).replace(" ago", "")})</p>
          ))}
        </details>
      )}
    </div>
  );
}
