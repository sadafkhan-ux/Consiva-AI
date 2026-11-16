import { useState } from "react";
import { enterpriseApi, enterpriseErrorMessage, enterpriseScans, siteHost, type ScanListItem } from "../api/enterprise";
import { getProfile } from "../api/auth";
import { writeHash } from "../components/dsrRoute";
import { completionTone, dateOnly, percent, text } from "./format";
import { usePaged } from "./hooks";
import { PagedView, Pill } from "./ui";
import { ScanPage } from "./ScanPage";

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
      const made = await enterpriseApi.createScan(url.trim(), authorized);
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
    <form className="scan-form" onSubmit={start} data-testid="new-scan">
      <div className="field grow">
        <label htmlFor="dpdp-url">Website address</label>
        <input id="dpdp-url" type="text" placeholder="https://example.com" value={url} onChange={(e) => setUrl(e.target.value)} autoComplete="off" />
      </div>
      <label className="checkbox-row">
        <input type="checkbox" checked={authorized} onChange={(e) => setAuthorized(e.target.checked)} />
        <span>I own this website or am permitted to scan it.</span>
      </label>
      <button type="submit" disabled={busy || !authorized || url.trim().length === 0}>{busy ? "Starting…" : "Start scan"}</button>
      <p className="hint">Only an organisation administrator can start a full-site scan. Where the server requires it, the domain must also pass DNS ownership verification.</p>
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

/** The Enterprise DPDP workspace: the list of scans, a form to start one, and the scan page. Separate from the 5-page consent scanner. */
export function EnterpriseWorkspace() {
  const [scanId, setScanId] = useState<string | null>(() => scanFromHash());
  const [nonce, setNonce] = useState(0);
  const canReview = getProfile()?.role === "admin";
  const p = usePaged<ScanListItem>((offset, limit) => enterpriseScans.list({ offset, limit }), "list", 25, String(nonce));

  function open(id: string | null) {
    setScanId(id);
    writeHash(id ? `#/dpdp/${id}` : "#/dpdp");
    if (!id) setNonce((n) => n + 1);
  }

  if (scanId) return <div className="section" data-testid="enterprise-workspace"><ScanPage scanId={scanId} canReview={canReview} onBack={() => open(null)} /></div>;

  return (
    <div className="section" data-testid="enterprise-workspace">
      <h2>Enterprise DPDP</h2>
      <p className="muted">
        Full-website DPDP readiness assessment. It reads the whole site, not five pages, and reports what it observed. It is a readiness assessment, not a certification.
      </p>
      <NewScan onStarted={open} />
      <h3 style={{ marginTop: 20 }}>Your scans</h3>
      <PagedView p={p} noun="scans" empty="No Enterprise scans yet. Start one above.">
        {(items) => (
          <table className="ropa-table" data-testid="scan-list">
            <thead><tr><th>Website</th><th>Status</th><th>Score</th><th>Coverage</th><th>Started</th><th>Open</th></tr></thead>
            <tbody>
              {items.map((s) => {
                const ct = completionTone(s.completion ?? s.status);
                return (
                  <tr key={s.scan_id}>
                    <td className="mono" style={{ wordBreak: "break-all" }}>{s.website_url}</td>
                    <td><Pill tone={ct.tone}>{ct.label}</Pill></td>
                    <td>{s.score === null || s.score === undefined ? <span className="muted">no score</span> : s.score}</td>
                    <td>{percent(s.coverage)}</td>
                    <td>{dateOnly(s.created_at)}</td>
                    <td><button type="button" className="secondary small" onClick={() => open(s.scan_id)} aria-label={`Open scan of ${text(s.website_url)}`}>Open</button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </PagedView>
    </div>
  );
}
