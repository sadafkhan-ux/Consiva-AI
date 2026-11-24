import { useEffect, useState } from "react";
import { enterpriseApi, enterpriseErrorMessage, enterpriseScans, siteHost, type ScanListItem } from "../api/enterprise";
import { dateTime } from "./format";
import { writeHash } from "../components/dsrRoute";
import { EnterpriseScanView } from "./EnterpriseScanView";

/** An address typed without http:// or https:// is scanned over https by default; an explicit scheme is kept. */
export function withDefaultScheme(u: string): string {
  const t = u.trim();
  return !t || t.includes("://") ? t : `https://${t.replace(/^\/+/, "")}`;
}

function scanFromHash(): string | null {
  const m = /^#\/dpdp\/([0-9A-Za-z-]{1,64})$/.exec(window.location.hash);
  return m ? m[1] : null;
}

function NewScan({ onStarted }: { onStarted: (id: string) => void }) {
  const [url, setUrl] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [txt, setTxt] = useState<string | null>(null);
  const [verifyNote, setVerifyNote] = useState<{ ok: boolean; text: string } | null>(null);
  const needsVerification = !!error && /DNS ownership verification/i.test(error);

  async function start(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const made = await enterpriseApi.createScan(withDefaultScheme(url), authorized);
      onStarted(made.scan_id);
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
  return (
    <form onSubmit={start} data-testid="new-scan">
      <div className="scan-form">
        <input id="dpdp-url" type="text" aria-label="Website address" placeholder="https://example.com" value={url} onChange={(e) => setUrl(e.target.value)}
          onBlur={() => setUrl((u) => withDefaultScheme(u))} autoComplete="off" disabled={busy} />
        <label>
          <input type="checkbox" checked={authorized} onChange={(e) => setAuthorized(e.target.checked)} disabled={busy} />
          I own / am authorized to scan this domain
        </label>
        <button type="submit" disabled={busy || !authorized || url.trim().length === 0}>{busy ? "Starting…" : "Scan Whole Website"}</button>
      </div>
      <div className="hint">SSRF-protected — private/internal/loopback addresses are always rejected regardless of this checkbox. Only an organisation administrator can start a full-site scan.</div>
      {error && <div className="banner banner-error" role="alert" data-testid="enterprise-error">{error}</div>}
      {(needsVerification || txt) && (
        <div data-testid="dpdp-verify">
          <p className="muted">Verify that you control {siteHost(url) || "the domain"}.</p>
          <button type="button" className="secondary small" disabled={busy || !siteHost(url)} onClick={getTxt}>Get the DNS record</button>
          {txt && <div className="code-panel"><span className="mono" style={{ wordBreak: "break-all" }}>{txt}</span></div>}
          {txt && <button type="button" className="secondary small" disabled={busy} onClick={verify}>I have added it: verify</button>}
          {verifyNote && <div className={`banner ${verifyNote.ok ? "banner-ok" : "banner-error"}`} role="status">{verifyNote.text}</div>}
        </div>
      )}
    </form>
  );
}

/**
 * The Enterprise DPDP workspace, laid out like the Privacy Policy screen: the address form at the top, the open scan (its progress circle, score, report
 * button and details) beneath it, and the Enterprise scan history at the right. It reads and writes only Enterprise scans; the 5-page agent's history is its own.
 */
export function EnterpriseWorkspace() {
  const [scanId, setScanId] = useState<string | null>(() => scanFromHash());
  const [nonce, setNonce] = useState(0);

  function open(id: string | null) {
    setScanId(id);
    writeHash(id ? `#/dpdp/${id}` : "#/dpdp");
    setNonce((n) => n + 1);
  }

  return (
    <div className="usage-layout" data-testid="enterprise-workspace">
      <div className="usage-main">
        <div className="agent-intro">
          <h1>Enterprise Scanner</h1>
          <div className="tagline">
            Enter a website address. Every page that can be reached is scanned, not just five, and checked against the 96 DPDP requirements. You get a score and a detailed PDF report.
          </div>
        </div>
        <NewScan onStarted={open} />
        {scanId && <div style={{ marginTop: 16 }}><EnterpriseScanView key={scanId} scanId={scanId} /></div>}
      </div>
      <EarlierScans current={scanId} onOpen={open} refreshKey={nonce} />
    </div>
  );
}

const STATUS_CHIP: Record<string, string> = { completed: "completed", failed: "failed", blocked: "failed", cancelled: "failed" };

/** The right-hand panel, as on the earlier scanner screen: every Enterprise scan with its site, address, time, status and result, newest first. */
function EarlierScans({ current, onOpen, refreshKey }: { current: string | null; onOpen: (id: string) => void; refreshKey: number }) {
  const [open, setOpen] = useState(true);
  const [items, setItems] = useState<ScanListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState(10);
  const [total, setTotal] = useState(0);

  useEffect(() => {
    let alive = true;
    const load = () => enterpriseScans.list({ limit }).then((p) => { if (alive) { setItems(p.items); setTotal(p.total); setError(null); } })
      .catch((err) => { if (alive) setError(enterpriseErrorMessage(err)); });
    void load();
    const t = window.setInterval(load, 15_000);                 // a running scan's status and result update without a reload
    return () => { alive = false; window.clearInterval(t); };
  }, [limit, refreshKey, current]);


  if (!open) {
    return <aside className="usage-panel collapsed" data-testid="earlier-scans"><button className="usage-toggle" onClick={() => setOpen(true)}>Earlier scans ‹</button></aside>;
  }
  return (
    <aside className="usage-panel" data-testid="earlier-scans" aria-label="Earlier Enterprise scans">
      <div className="usage-head">
        <h3>Enterprise scans</h3>
        <span className="usage-chip">{total} {total === 1 ? "scan" : "scans"}</span>
        <button className="usage-toggle" onClick={() => setOpen(false)} aria-label="Hide the scans panel">›</button>
      </div>
      <h4>Earlier scans</h4>
      {error && <p className="usage-error">{error}</p>}
      {items && items.length === 0 && <p className="usage-muted">No Enterprise scans yet.</p>}
      {!items && !error && <p className="usage-muted">Reading the scans…</p>}
      <ul className="usage-history">
        {(items ?? []).map((s) => {
          const host = siteHost(s.website_url) || s.website_url;
          const chip = STATUS_CHIP[s.status] ?? "running";
          return (
            <li key={s.scan_id} className={`${current === s.scan_id ? "current " : ""}clickable`} data-testid="earlier-scan" tabIndex={0}
                onClick={(e) => { if (!(e.target as HTMLElement).closest("button")) onOpen(s.scan_id); }}
                onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); onOpen(s.scan_id); } }}>
              <div className="usage-history-top"><b title={s.website_url}>{host}</b><span className={`usage-chip usage-chip-${chip}`}>{s.status}</span></div>
              <div className="usage-muted small usage-url-wrap">{s.website_url}</div>
              <div className="usage-muted small">{dateTime(s.created_at)}{s.completed_at ? ` → ${dateTime(s.completed_at)}` : ""}</div>
              <div className="usage-muted small usage-mono" title="Scan ID">{s.scan_id}</div>
              <div className="usage-history-nums">
                {s.score != null && <span data-testid="earlier-score">score {s.score} / 100</span>}
              </div>
              <button className="usage-link" onClick={() => onOpen(s.scan_id)} aria-label={`Open the Enterprise scan of ${host}`}>Open scan</button>
            </li>
          );
        })}
      </ul>
      {items && total > items.length && <button type="button" className="usage-more" onClick={() => setLimit((n) => n + 10)}>Show older scans</button>}
    </aside>
  );
}
