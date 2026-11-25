import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { enterpriseErrorMessage, enterpriseScans, type ScanSummary } from "../api/enterprise";
import { canResume, completionTone, isLive, text } from "./format";
import { DocumentsTab, EvidenceTab, UrlsTab } from "./ListTabs";
import { ChangesTab, FindingsTab, ReportTab } from "./MoreTabs";
import { OverviewTab } from "./OverviewTab";
import { ParametersTab } from "./ParametersTab";
import { ErrorNote, Pill, Skeleton } from "./ui";

export const POLL_MS = 4000;

const TABS = [
  ["overview", "Overview"], ["urls", "URLs"], ["documents", "Documents"], ["evidence", "Evidence"], ["consent", "Consent and trackers"],
  ["parameters", "Parameters"], ["findings", "Findings"], ["changes", "Changes"], ["report", "Report"],
] as const;
type TabId = (typeof TABS)[number][0];

export function ScanPage({ scanId, canReview, onBack }: { scanId: string; canReview: boolean; onBack: () => void }) {
  const [s, setS] = useState<ScanSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId>("overview");
  const [evidenceFocus, setEvidenceFocus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [kick, setKick] = useState(0); // bump to read now (and restart polling after an action)
  const timer = useRef<number | null>(null);

  useEffect(() => {
    setS(null);
    setError(null);
    setTab("overview");
    setEvidenceFocus(null);
  }, [scanId]);

  // Poll only while the scan can still change. One timer at a time; it is cleared on unmount, on a new scan, and the moment the scan is final.
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
        if (alive) setError(enterpriseErrorMessage(e)); // polling stops on an error; "Try again" restarts it
      }
    };
    void tick();
    return () => {
      alive = false;
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [scanId, kick]);

  const refresh = useCallback(() => setKick((k) => k + 1), []);
  const refreshToken = s ? [s.completion, s.crawl_status, s.analysis_status, s.declaration?.urls.crawled, s.declaration?.urls.analysed, s.declaration?.evidence.records, s.score?.score ?? "-", s.report_available].join("|") : "";

  async function act(kind: "cancel" | "resume" | "analyze") {
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

  function openEvidence(id: string) {
    setEvidenceFocus(id);
    setTab("evidence");
  }

  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    const i = TABS.findIndex(([id]) => id === tab);
    const next = e.key === "ArrowRight" ? (i + 1) % TABS.length : e.key === "ArrowLeft" ? (i - 1 + TABS.length) % TABS.length : e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : -1;
    if (next < 0) return;
    e.preventDefault();
    setTab(TABS[next][0]);
    window.requestAnimationFrame(() => document.getElementById(`tab-${TABS[next][0]}`)?.focus());
  }

  const live = s ? isLive(s.completion, s.crawl_status, s.analysis_status) : false;
  const cancelWhy = s && !live ? "The scan is not running." : null;
  const resumeWhy = s && !canResume(s.crawl_status, s.stop_reason, s.analysis_status) ? "Only a scan that is waiting or running, was blocked by the website, or was stopped by a time or memory limit can be resumed." : null;
  const analyzeWhy = s && (s.crawl_status !== "completed" ? "The crawl has to finish before analysis can run." : s.analysis_status === "running" ? "Analysis is already running." : null);
  const ct = s ? completionTone(s.completion) : null;

  return (
    <div data-testid="scan-page">
      <button type="button" className="secondary small" onClick={onBack}>Back to all scans</button>
      {error && <ErrorNote message={error} onRetry={refresh} />}
      {!s && !error && <Skeleton rows={5} label="Loading scan" />}
      {s && (
        <>
          <h3 style={{ margin: "10px 0 4px" }}>
            <span className="mono" style={{ wordBreak: "break-all" }}>{s.website_url}</span> {ct && <Pill tone={ct.tone}>{ct.label}</Pill>}
          </h3>
          {live && <p className="muted small" data-testid="live-note" aria-live="polite">Updating every {POLL_MS / 1000} seconds while the scan is running.</p>}
          <div className="inline-row" data-testid="scan-actions">
            <button type="button" className="secondary small" disabled={busy || !!cancelWhy} title={cancelWhy ?? "Stop this scan"} onClick={() => act("cancel")}>Cancel</button>
            <button type="button" className="secondary small" disabled={busy || !!resumeWhy} title={resumeWhy ?? "Carry on a scan whose worker stopped"} onClick={() => act("resume")}>Resume</button>
            <button type="button" className="secondary small" disabled={busy || !!analyzeWhy} title={analyzeWhy ?? "Run the analysis again"} onClick={() => act("analyze")}>Re-run analysis</button>
          </div>
          {[cancelWhy && `Cancel: ${cancelWhy}`, resumeWhy && `Resume: ${resumeWhy}`, analyzeWhy && `Re-run analysis: ${analyzeWhy}`].filter(Boolean).map((r) => <p key={r as string} className="muted small" style={{ margin: "2px 0" }}>{r}</p>)}
          {note && <div className={`banner ${note.ok ? "banner-ok" : "banner-error"}`} role="status" data-testid="action-note">{note.text}</div>}

          <div role="tablist" aria-label="Scan sections" onKeyDown={onKey} style={{ display: "flex", flexWrap: "wrap", gap: 4, borderBottom: "1px solid var(--border)", margin: "12px 0" }}>
            {TABS.map(([id, label]) => (
              <button
                key={id} id={`tab-${id}`} role="tab" type="button" aria-selected={tab === id} aria-controls={`panel-${id}`} tabIndex={tab === id ? 0 : -1}
                onClick={() => setTab(id)}
                style={{ background: "none", border: "none", borderBottom: `2px solid ${tab === id ? "var(--accent)" : "transparent"}`, color: tab === id ? "var(--text)" : "var(--muted)", padding: "8px 12px", cursor: "pointer", font: "inherit" }}
              >{label}</button>
            ))}
          </div>
          <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
            {tab === "overview" && <OverviewTab s={s} />}
            {tab === "urls" && <UrlsTab scanId={scanId} refresh={refreshToken} />}
            {tab === "documents" && <DocumentsTab scanId={scanId} refresh={refreshToken} />}
            {tab === "evidence" && <EvidenceTab scanId={scanId} refresh={refreshToken} focusId={evidenceFocus} onFocus={setEvidenceFocus} />}
            {tab === "consent" && <ParametersTab scanId={scanId} refresh={refreshToken} canReview={canReview} onEvidence={openEvidence} onChanged={refresh} onlyGroup="consent" heading="What the scan observed about consent, cookies and trackers, as the consent parameters. Open the linked evidence for the cookies and requests behind each one." />}
            {tab === "parameters" && <ParametersTab scanId={scanId} refresh={refreshToken} canReview={canReview} onEvidence={openEvidence} onChanged={refresh} />}
            {tab === "findings" && <FindingsTab scanId={scanId} refresh={refreshToken} onEvidence={openEvidence} />}
            {tab === "changes" && <ChangesTab scanId={scanId} websiteUrl={s.website_url} createdAt={s.created_at} />}
            {tab === "report" && <ReportTab scanId={scanId} refresh={refreshToken} />}
          </div>
        </>
      )}
    </div>
  );
}
