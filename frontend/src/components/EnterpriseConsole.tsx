import { useCallback, useEffect, useRef, useState } from "react";
import {
  FINAL_STATES,
  enterpriseApi,
  enterpriseErrorMessage,
  siteHost,
  type CoverageResponse,
  type EnterpriseScanStatus,
  type FrontierState,
} from "../api/enterprise";

/**
 * Enterprise scanner console: start a full-website scan, watch it, and read which URLs were discovered and what became of each.
 *
 * It reuses the shell's existing styles, and shows ONLY what the Enterprise API returns today: the crawl's coverage. There is no score and no finding here on purpose, because
 * the Enterprise scoring and legal assessment are not built; a page that showed a number would be inventing one. "Discovered" is never presented as "scanned".
 */

const POLL_MS = 4000;
const PAGE_SIZE = 50;
const STATE_TONE: Record<string, string> = { completed: "ok", running: "info", queued: "info", failed: "bad", cancelled: "muted" };
const URL_TONE: Record<string, string> = { done: "ok", failed: "bad", excluded: "warn", pending: "muted", in_progress: "info" };
const URL_LABEL: Record<string, string> = { done: "Scanned", failed: "Failed", excluded: "Not scanned", pending: "Waiting", in_progress: "In progress" };

export function EnterpriseConsole() {
  const [url, setUrl] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const [scanId, setScanId] = useState<string>(() => {
    try {
      return window.sessionStorage.getItem("consiva.enterprise.scan") ?? "";
    } catch {
      return "";
    }
  });
  const [status, setStatus] = useState<EnterpriseScanStatus | null>(null);
  const [coverage, setCoverage] = useState<CoverageResponse | null>(null);
  const [filter, setFilter] = useState<FrontierState | "">("");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<number | null>(null);
  // Domain ownership: the one thing that stops a full-site scan of a site nobody has proved they own.
  const [txt, setTxt] = useState<string | null>(null);
  const [verifyNote, setVerifyNote] = useState<{ ok: boolean; text: string } | null>(null);
  const needsVerification = !!error && /DNS ownership verification/i.test(error);

  const remember = (id: string) => {
    setScanId(id);
    try {
      window.sessionStorage.setItem("consiva.enterprise.scan", id);
    } catch {
      /* remembering the scan across a reload is a convenience */
    }
  };

  const refresh = useCallback(async (id: string, f: FrontierState | "", o: number) => {
    try {
      const [s, c] = await Promise.all([enterpriseApi.status(id), enterpriseApi.coverage(id, { status: f, limit: PAGE_SIZE, offset: o })]);
      setStatus(s);
      setCoverage(c);
      setError(null);
      return s;
    } catch (e) {
      setError(enterpriseErrorMessage(e));
      return null;
    }
  }, []);

  // Poll while the scan can still change; stop the moment it is final.
  useEffect(() => {
    if (!scanId) return;
    let cancelled = false;
    const tick = async () => {
      const s = await refresh(scanId, filter, offset);
      if (cancelled) return;
      if (s && FINAL_STATES.has(s.status)) return;
      timer.current = window.setTimeout(tick, POLL_MS);
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [scanId, filter, offset, refresh]);

  async function start(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const made = await enterpriseApi.createScan(url.trim(), authorized);
      setStatus(null);
      setCoverage(null);
      setFilter("");
      setOffset(0);
      remember(made.scan_id);
    } catch (err) {
      setError(enterpriseErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function getTxt() {
    setBusy(true);
    setVerifyNote(null);
    try {
      setTxt((await enterpriseApi.verificationToken(siteHost(url))).txt_record);
    } catch (err) {
      setVerifyNote({ ok: false, text: enterpriseErrorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function verify() {
    setBusy(true);
    setVerifyNote(null);
    try {
      const r = await enterpriseApi.verifyDomain(siteHost(url));
      setVerifyNote({ ok: true, text: `${r.domain} is verified. You can start the scan now.` });
      setError(null);
    } catch (err) {
      setVerifyNote({ ok: false, text: enterpriseErrorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function act(kind: "cancel" | "resume") {
    if (!scanId) return;
    setBusy(true);
    try {
      await (kind === "cancel" ? enterpriseApi.cancel(scanId) : enterpriseApi.resume(scanId));
      await refresh(scanId, filter, offset);
    } catch (err) {
      setError(enterpriseErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const final = status ? FINAL_STATES.has(status.status) : false;
  const canStart = url.trim().length > 0 && authorized && !busy;

  return (
    <div className="section" data-testid="enterprise-console">
      <h2>Enterprise scanner</h2>
      <p className="muted">
        Full-website scan: it follows the site's links and sitemap past five pages and reads linked PDF and Word documents. This shows what was reached. It does not score the site or assess it against the
        law yet: no score, finding or report is produced.
      </p>

      <form className="scan-form" onSubmit={start}>
        <div className="field grow">
          <label htmlFor="ent-url">Website address</label>
          <input id="ent-url" type="text" placeholder="https://example.com" value={url} onChange={(e) => setUrl(e.target.value)} autoComplete="off" />
        </div>
        <label className="checkbox-row">
          <input type="checkbox" checked={authorized} onChange={(e) => setAuthorized(e.target.checked)} />
          <span>I own this website or am permitted to scan it.</span>
        </label>
        <button type="submit" disabled={!canStart}>{busy ? "Starting…" : "Start full scan"}</button>
        <p className="hint">Only an organisation administrator can start a full-site scan. If this server requires it, the domain must also pass DNS ownership verification.</p>
      </form>

      {error && <div className="banner banner-error" role="alert" data-testid="enterprise-error">{error}</div>}

      {(needsVerification || txt) && (
        <div className="section" data-testid="enterprise-verify">
          <h3>Verify ownership of {siteHost(url) || "the domain"}</h3>
          <p className="muted">A full-site scan reads far more than five pages, so Consiva scans only a domain you have proved you control.</p>
          <ol>
            <li><button type="button" className="secondary small" disabled={busy || !siteHost(url)} onClick={getTxt}>Get the DNS record</button></li>
            {txt && (
              <li>
                Add a DNS <strong>TXT</strong> record on <span className="mono">{siteHost(url)}</span> with exactly this value, and wait a few minutes for it to spread:
                <div className="code-panel"><span className="mono" data-testid="enterprise-txt" style={{ wordBreak: "break-all" }}>{txt}</span></div>
              </li>
            )}
            {txt && <li><button type="button" className="secondary small" disabled={busy} onClick={verify}>I have added it: verify</button></li>}
          </ol>
          {verifyNote && <div className={`banner ${verifyNote.ok ? "banner-ok" : "banner-error"}`} role="status" data-testid="enterprise-verify-note">{verifyNote.text}</div>}
        </div>
      )}

      {status && (
        <div className="section" data-testid="enterprise-status">
          <h3>
            <span className="mono">{status.website_url}</span>{" "}
            <span className={`pill pill-${STATE_TONE[status.status] ?? "muted"}`} data-testid="enterprise-state">{status.status}</span>
          </h3>
          <div className="bar" aria-label="progress"><div style={{ width: `${status.progress}%` }} /></div>
          <p data-testid="enterprise-counts">
            <strong>{status.urls.total}</strong> URLs found · <strong>{status.urls.done}</strong> scanned · <strong>{status.urls.failed}</strong> failed ·{" "}
            <strong>{status.urls.excluded}</strong> not scanned · <strong>{status.urls.pending + status.urls.in_progress}</strong> waiting
          </p>
          {!final && <p className="muted">The scan is running. New links are found as pages are read, so the total can still grow.</p>}
          {status.stop_reason && <div className="banner banner-info">Stopped because: {status.stop_reason}.</div>}
          {status.error && <div className="banner banner-error">{status.error}</div>}
          <div className="inline-row">
            {!final && <button className="secondary small" disabled={busy} onClick={() => act("cancel")}>Cancel</button>}
            {!final && <button className="secondary small" disabled={busy} onClick={() => act("resume")} title="Carry on a scan whose worker stopped">Resume</button>}
          </div>
        </div>
      )}

      {coverage && (
        <div className="section" data-testid="enterprise-coverage">
          <h3>Coverage</h3>
          <p className="muted">{coverage.note}</p>
          <div className="inline-row">
            <label htmlFor="ent-filter">Show</label>
            <select id="ent-filter" value={filter} onChange={(e) => { setOffset(0); setFilter(e.target.value as FrontierState | ""); }}>
              <option value="">All URLs</option>
              <option value="done">Scanned</option>
              <option value="failed">Failed</option>
              <option value="excluded">Not scanned</option>
              <option value="pending">Waiting</option>
            </select>
            <span className="muted">{coverage.total} URL(s)</span>
          </div>
          {coverage.items.length === 0 ? (
            <p className="empty-note">No URLs here yet.</p>
          ) : (
            <div className="check-table-wrap">
              <table className="ropa-table">
                <thead>
                  <tr><th>URL</th><th>Depth</th><th>Found by</th><th>Result</th><th>Why</th></tr>
                </thead>
                <tbody>
                  {coverage.items.map((it) => (
                    <tr key={`${it.url}`}>
                      <td className="mono" style={{ wordBreak: "break-all" }}>{it.url}</td>
                      <td>{it.depth}</td>
                      <td>{it.discovery_sources.join(", ")}</td>
                      <td><span className={`pill pill-${URL_TONE[it.status] ?? "muted"}`}>{URL_LABEL[it.status] ?? it.status}</span></td>
                      <td>{it.status_reason ?? it.last_error ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="inline-row">
            <button className="secondary small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
            <button className="secondary small" disabled={offset + PAGE_SIZE >= coverage.total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
          </div>
        </div>
      )}
    </div>
  );
}
