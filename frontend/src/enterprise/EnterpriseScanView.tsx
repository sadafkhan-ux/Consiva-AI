import { useCallback, useEffect, useRef, useState } from "react";
import { saveBlob } from "../api/client";
import { enterpriseErrorMessage, enterpriseScans, fetchEnterpriseReportPdf, siteHost, type Method3Line, type Method3Result, type ScanSummary } from "../api/enterprise";
import { canResume, categoriesInOrder, dateTime, isLive, text } from "./format";

export const POLL_MS = 4000;

/**
 * One Enterprise scan on one page, laid out like the Privacy Policy screen: the status, the circle (progress while it runs, the score when it is done),
 * the report button, and the report details. The score is the method 3 (96-parameter) score, the same number the PDF report prints.
 */
export function EnterpriseScanView({ scanId }: { scanId: string }) {
  const [s, setS] = useState<ScanSummary | null>(null);
  const [m3, setM3] = useState<Method3Result | null>(null);
  const [m3Missing, setM3Missing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [kick, setKick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => { setS(null); setM3(null); setM3Missing(false); setError(null); setNote(null); }, [scanId]);

  // Read the scan; keep reading while it can still change, and stop the moment it is final.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const next = await enterpriseScans.summary(scanId);
        if (!alive) return;
        setS(next);
        setError(null);
        if (isLive(next.completion, next.crawl_status, next.analysis_status)) timer.current = window.setTimeout(tick, POLL_MS);
      } catch (e) {
        if (alive) setError(enterpriseErrorMessage(e));
      }
    };
    void tick();
    return () => { alive = false; if (timer.current !== null) window.clearTimeout(timer.current); };
  }, [scanId, kick]);

  const live = s ? isLive(s.completion, s.crawl_status, s.analysis_status) : true;
  const finishedKey = s && !live ? `${s.completion}|${s.analysis_status}|${s.official_score?.score ?? "-"}` : "";

  // The 96-parameter result is read once the scan has stopped changing.
  useEffect(() => {
    if (!finishedKey) return;
    let alive = true;
    enterpriseScans.method3(scanId).then((r) => { if (alive) { setM3(r); setM3Missing(false); } })
      .catch(() => { if (alive) { setM3(null); setM3Missing(true); } });
    return () => { alive = false; };
  }, [scanId, finishedKey]);

  const refresh = useCallback(() => setKick((k) => k + 1), []);

  async function act(kind: "cancel" | "resume") {
    setBusy(true);
    setNote(null);
    try {
      const r = await enterpriseScans[kind](scanId);
      setNote({ ok: true, text: text(r.message, "Done.") });
      refresh();
    } catch (e) {
      setNote({ ok: false, text: enterpriseErrorMessage(e) });
    } finally {
      setBusy(false);
    }
  }

  if (error) return <div className="banner banner-error" role="alert" data-testid="scan-error">{error} <button type="button" className="secondary small" onClick={refresh}>Try again</button></div>;
  if (!s) return <p className="muted" data-testid="scan-loading">Reading the scan…</p>;

  const failed = s.crawl_status === "failed" || s.completion === "failed";
  const cancelled = s.crawl_status === "cancelled" || s.completion === "cancelled";
  const done = !live && !failed && !cancelled;

  return (
    <div data-testid="enterprise-scan">
      <StatusBanner s={s} live={live} failed={failed} cancelled={cancelled} busy={busy} onStop={() => act("cancel")} onResume={() => act("resume")} />
      {note && <div className={`banner ${note.ok ? "banner-ok" : "banner-error"}`} role="status" data-testid="action-note">{note.text}</div>}

      <ReportBar scanId={scanId} ready={done && !!m3} pending={live} />

      <Hero s={s} m3={m3} live={live} />

      {done && !m3 && m3Missing && (
        <div className="status-banner warn" role="status" data-testid="m3-missing" style={{ marginBottom: 14 }}>
          The score is being calculated from the analysis. It appears here in a moment; reload the scan if it does not.
        </div>
      )}
      {m3 && <ReportDetails m3={m3} />}
    </div>
  );
}

function StatusBanner({ s, live, failed, cancelled, busy, onStop, onResume }: { s: ScanSummary; live: boolean; failed: boolean; cancelled: boolean; busy: boolean; onStop: () => void; onResume: () => void }) {
  const u = s.declaration?.urls;
  const crawling = s.crawl_status === "queued" || s.crawl_status === "running";
  const message = failed ? "Scan failed" : cancelled ? "Scan stopped" : crawling ? (s.crawl_status === "queued" ? "Waiting to start" : "Scanning the website")
    : live ? "Checking the 96 DPDP requirements" : "Scan complete";
  const tone = failed ? "bad" : cancelled ? "warn" : live ? "progress" : "ok";
  return (
    <section className="section" data-testid="scan-status">
      <div className={`status-banner ${tone}`} style={{ marginBottom: 10 }} role="status" aria-live="polite">
        <span className="dot" />
        <span data-testid="status-text">{message}</span>
        {live && <button type="button" className="stop-btn" onClick={onStop} disabled={busy} style={{ marginLeft: "auto" }}>Stop scan</button>}
        {!live && canResume(s.crawl_status, s.stop_reason, s.analysis_status) && <button type="button" className="secondary small" onClick={onResume} disabled={busy} style={{ marginLeft: "auto" }}>Resume</button>}
      </div>
      {s.error && <p className="muted small" data-testid="scan-error-reason">{s.error}</p>}
      <p className="muted small" style={{ margin: 0 }}>
        <span className="mono" style={{ wordBreak: "break-all" }}>{s.website_url}</span>
        {u && typeof u.crawled === "number" ? ` · ${u.crawled} page${u.crawled === 1 ? "" : "s"} read` : ""}
        {` · started ${dateTime(s.started_at ?? s.created_at)}`}{s.completed_at ? ` · finished ${dateTime(s.completed_at)}` : ""}
      </p>
    </section>
  );
}

function progressOf(s: ScanSummary): number {
  const u = s.declaration?.urls;
  const eligible = typeof u?.eligible === "number" && u.eligible > 0 ? u.eligible : null;
  if (!eligible) return s.crawl_status === "queued" ? 0 : 5;
  const crawled = typeof u?.crawled === "number" ? u.crawled : 0;
  const analysed = typeof u?.analysed === "number" ? u.analysed : 0;
  // Reading the pages is the first half of the work, checking them the second.
  const crawling = s.crawl_status === "queued" || s.crawl_status === "running";
  const pct = crawling ? (crawled / eligible) * 50 : 50 + (analysed / eligible) * 50;
  return Math.max(1, Math.min(99, Math.round(pct)));
}

/** The circle: progress while the scan runs, the score out of 100 when it is done. */
export function Ring({ value, mode }: { value: number | null; mode: "score" | "progress" }) {
  const R = 44;
  const C = 2 * Math.PI * R;
  const filled = value === null ? 0 : (Math.min(100, Math.max(0, value)) / 100) * C;
  const shown = value === null ? "–" : mode === "progress" ? `${value}%` : String(Math.round(value * 10) / 10);
  const size = shown.length >= 5 ? 24 : 30;
  return (
    <svg viewBox="0 0 110 110" width="124" height="124" role="img" data-testid="enterprise-ring"
      aria-label={value === null ? "No score yet" : mode === "progress" ? `Scan ${value}% done` : `Enterprise DPDP score ${shown} out of 100`}>
      <circle cx="55" cy="55" r="52" fill="none" stroke="var(--ring-edge, #05070d)" strokeWidth="4" />
      <circle cx="55" cy="55" r={R} fill="none" stroke="var(--ring-track, #26324a)" strokeWidth="9" />
      {filled > 0 && <circle cx="55" cy="55" r={R} fill="none" stroke="var(--ring-arc, #7a4dff)" strokeWidth="9" strokeLinecap="round" strokeDasharray={`${filled} ${C}`} transform="rotate(-90 55 55)" />}
      <text x="55" y={mode === "score" && value !== null ? 58 : 55 + size * 0.36} textAnchor="middle" fontSize={size} fontWeight="700" fill="currentColor" data-testid="ring-value">{shown}</text>
      {mode === "score" && value !== null && <text x="55" y="76" textAnchor="middle" fontSize="11" fill="currentColor" opacity="0.7">/100</text>}
    </svg>
  );
}

function band(score: number): { label: string; cls: string } {
  if (score >= 80) return { label: "Largely ready", cls: "ok" };
  if (score >= 50) return { label: "Partly ready", cls: "warn" };
  return { label: "Major gaps", cls: "bad" };
}

function Hero({ s, m3, live }: { s: ScanSummary; m3: Method3Result | null; live: boolean }) {
  const host = siteHost(s.website_url) || s.website_url;
  const c = m3?.counts ?? {};
  const b = m3 && m3.score !== null ? band(m3.score) : null;
  return (
    <section className="section scan-hero" data-testid="enterprise-hero" style={{ gridTemplateColumns: "150px minmax(0, 1fr)" }}>
      <div className="hero-score">
        {live ? <Ring value={progressOf(s)} mode="progress" /> : <Ring value={m3?.score ?? null} mode="score" />}
        <div className="hero-score-label">{live ? "Scan progress" : "Enterprise DPDP score"}</div>
      </div>
      <div className="hero-text">
        {live ? (
          <>
            <span className="badge neutral">In progress</span>
            <p className="hero-headline">Scanning every reachable page of {host}.</p>
            <p className="small muted">The score and the report appear here when the scan and the checks finish. You can leave this page; the scan keeps running.</p>
          </>
        ) : m3 && b ? (
          <>
            <span className={`badge ${b.cls}`} data-testid="hero-band">{b.label}</span>
            <p className="hero-headline" data-testid="hero-headline">
              {host}: {c.pass ?? 0} requirements met, {c.partial ?? 0} partly met and {c.fail ?? 0} not met, out of {m3.parameters} DPDP requirements.
            </p>
            <p className="small muted" data-testid="hero-basis">
              {m3.marks_earned} of {m3.applicable_max} applicable marks. Requirements that do not apply to this website ({c.not_applicable ?? 0}) are left out of the maximum. Method {m3.version}.
            </p>
          </>
        ) : (
          <>
            <span className="badge neutral">No score</span>
            <p className="hero-headline">{host}</p>
          </>
        )}
      </div>
    </section>
  );
}

const REPORT_WAIT_MS = 3000;
const REPORT_TRIES = 40;

function ReportBar({ scanId, ready, pending }: { scanId: string; ready: boolean; pending: boolean }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!ready && !pending) return null;

  async function download() {
    setBusy(true);
    setErr(null);
    try {
      // The newest report is made from the latest result, then saved. A report already made is still downloaded if making a new one is refused.
      try {
        const g = await enterpriseScans.generateReport(scanId);
        for (let i = 0; i < REPORT_TRIES; i += 1) {
          const v = (await enterpriseScans.reports(scanId)).items.find((r) => r.version === g.version);
          if (!v || v.status === "ready" || v.status === "failed") break;
          await new Promise((r) => window.setTimeout(r, REPORT_WAIT_MS));
        }
      } catch { /* fall through to the latest ready report */ }
      const f = await fetchEnterpriseReportPdf(scanId);
      saveBlob(f.blob, f.filename);
    } catch (e) {
      setErr(enterpriseErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="report-bar" data-testid="enterprise-report-bar">
      <div className="report-bar-text">
        <strong>Enterprise DPDP report</strong>
        <span>{pending ? "The PDF report becomes available when the scan finishes." : "The score, every one of the 96 DPDP requirements with its marks, the gaps to fix and the evidence, as a PDF."}</span>
        {err && <span className="report-error" role="alert">{err}</span>}
      </div>
      <button className="report-btn" onClick={download} disabled={!ready || busy} data-testid="download-report">{busy ? "Preparing…" : "Download Report (PDF)"}</button>
    </div>
  );
}

const STATUS_BADGE: Record<string, string> = { pass: "ok", partial: "warn", fail: "bad", not_applicable: "neutral", information: "info" };
const SEVERITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };

function StatusBadge({ l }: { l: Method3Line }) {
  return <span className={`badge ${STATUS_BADGE[l.status] ?? "neutral"}`}>{l.status_label}</span>;
}

function marks(l: Method3Line): string {
  return l.status === "not_applicable" || l.earned === null ? "—" : `${l.earned} / ${l.max}`;
}

function ReportDetails({ m3 }: { m3: Method3Result }) {
  const cats = categoriesInOrder(m3.categories);
  const gaps = m3.lines.filter((l) => l.status === "fail" && l.max > 0)
    .sort((a, b) => (SEVERITY_RANK[a.severity ?? ""] ?? 3) - (SEVERITY_RANK[b.severity ?? ""] ?? 3) || b.max - a.max).slice(0, 10);
  return (
    <>
      <section className="section" data-testid="category-scores">
        <h2>1. Score by area</h2>
        <div className="check-table-wrap">
          <table className="ropa-table">
            <thead><tr><th>Area</th><th>Marks</th><th>Score</th><th>Met</th><th>Partly</th><th>Not met</th></tr></thead>
            <tbody>
              {cats.map(([k, c]) => (
                <tr key={k}>
                  <td>{c.label}</td>
                  <td>{c.earned} / {c.max}</td>
                  <td>{c.pct === null ? "—" : `${c.pct}%`}</td>
                  <td>{c.counts.pass ?? 0}</td>
                  <td>{c.counts.partial ?? 0}</td>
                  <td>{c.counts.fail ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="section" data-testid="major-gaps">
        <h2>2. Major gaps</h2>
        {gaps.length === 0 ? <p className="muted">No requirement was found not met.</p> : (
          <ul style={{ paddingLeft: 18, margin: 0 }}>
            {gaps.map((l) => (
              <li key={l.parameter_id} style={{ marginBottom: 8 }}>
                <strong>{l.parameter_id} · {l.name}</strong>{l.severity ? <span className={`badge ${l.severity === "high" ? "bad" : l.severity === "medium" ? "warn" : "neutral"}`} style={{ marginLeft: 6 }}>{l.severity}</span> : null}
                <div className="small muted">{l.explanation}{l.rule ? ` (${l.rule})` : ""}</div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="section" data-testid="all-requirements">
        <h2>3. All {m3.parameters} requirements</h2>
        {cats.map(([k, c]) => {
          const rows = m3.lines.filter((l) => l.category === k);
          return (
            <details key={k} style={{ marginBottom: 8 }}>
              <summary style={{ cursor: "pointer" }}><strong>{c.label}</strong> <span className="muted small">({rows.length} requirements, {c.earned} / {c.max} marks)</span></summary>
              <div className="check-table-wrap">
                <table className="ropa-table">
                  <thead><tr><th>ID</th><th>Requirement</th><th>Result</th><th>Marks</th><th>Why</th></tr></thead>
                  <tbody>
                    {rows.map((l) => (
                      <tr key={l.parameter_id} data-testid={`req-${l.parameter_id}`}>
                        <td className="mono">{l.parameter_id}</td>
                        <td>{l.name}</td>
                        <td><StatusBadge l={l} /></td>
                        <td>{marks(l)}</td>
                        <td className="small">{l.explanation}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          );
        })}
      </section>

      <p className="muted small" data-testid="not-certification">
        A website-observable readiness assessment under the DPDP Act 2023 and Rules 2025. It is not a certification, an audit opinion or legal advice.
        {m3.legal_review_status ? ` ${m3.legal_review_status}.` : ""}
      </p>
    </>
  );
}
