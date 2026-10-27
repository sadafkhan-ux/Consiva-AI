import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { fetchReportPdf, saveBlob } from "../../api/client";
import { AUTH_BYPASSED, autoLogin, getToken } from "../../api/auth";
import { LoginScreen } from "../../components/LoginScreen";
import { useConsentScan } from "../../hooks/useConsentScan";
import { pagesCoverageLine } from "../scanPages";
import { reportModelFromState } from "../modelFromState";
import type { ReportModel } from "../buildReport";
import { FreeReport, ReportCover } from "./FreeReport";
import { FullReport } from "./FullReport";
import { SummaryReport } from "./SummaryReport";
import { ReportDocument } from "./ReportDocument";

declare global {
  interface Window {
    /** Set when the report is fully drawn, so the server that prints it knows when to start. */
    __REPORT_READY__?: boolean;
    /** Why the report could not be drawn (the printing server reads this instead of waiting out its timeout). */
    __REPORT_ERROR__?: string;
    /** The words for the printed page header and the file's name, worked out here so the PDF header matches the report. */
    __REPORT_META__?: { domain: string; scanned: string; filename: string };
  }
}

const SETTLE_MS = 8_000;

/** The header line of the printed page: "Scanned 21 Oct 2026, 06:41 UTC · 5 of 520 pages scanned". The same words as the PDF drawn in the browser. */
export function reportHeaderMeta(model: ReportModel): { domain: string; scanned: string; filename: string } {
  const v = model.dpdpScore;
  const vis = model.visual;
  const domain = model.siteUrl.replace(/^https?:\/\//, "").replace(/\/$/, "");
  const pages = model.scanPages ? model.scanPages.coverageLine : vis ? pagesCoverageLine(vis.pagesScanned, vis.pagesFound) : "";
  const host = model.siteUrl.replace(/^https?:\/\//, "").replace(/[^a-zA-Z0-9.-]/g, "-").replace(/-+$/, "");
  const stamp = new Date().toISOString().slice(0, 10);
  return { domain, scanned: `Scanned ${v ? v.scanTime : model.generatedAt}${pages ? ` · ${pages}` : ""}`, filename: `consiva-privacy-report-${host || "report"}-${stamp}.pdf` };
}

/**
 * The web report for one scan (/?report=<scanId>), and the page the server prints to make the PDF (/?report=<scanId>&print=1). It loads the scan
 * exactly as the console does when a scan is opened, builds the report model with the same function, and draws the one report document.
 */
export type ReportVariantParam = "free" | "full" | "summary";

/**
 * `variant` picks the report: "free" is the two-page Free Scan report, "full" the eight-page Full Scan report followed by its evidence appendix, and none the
 * long single-flow report. `part` is only for the server's printing: "pages" draws the fixed pages alone and "appendix" the evidence alone, because the
 * fixed pages carry their own header and footer and the appendix takes the print engine's.
 */
export function ReportPage({ scanId, print, variant, part }: { scanId: string; print: boolean; variant?: ReportVariantParam; part?: "pages" | "appendix" | "cover" | "body" }) {
  const { state, open } = useConsentScan();
  const [settled, setSettled] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [signedIn, setSignedIn] = useState(!!getToken());
  const opened = useRef(false);

  // The console around this app is dark (its colour scheme, page background and heading font are set for the whole document). The report is a light
  // document, on screen and on paper, so while it is shown the document is put in report mode (see report.css) and put back after.
  // "Open web report" opens a new tab, and a new tab starts with no session (the token lives in the tab's own storage). Under the development / open-access
  // bypass the tab signs itself in; otherwise it shows the sign-in form. The printing server always arrives with a session.
  useEffect(() => {
    if (signedIn || print || !AUTH_BYPASSED) return;
    autoLogin().then(() => setSignedIn(true)).catch(() => undefined);
  }, [signedIn, print]);

  useLayoutEffect(() => {
    document.documentElement.classList.add("report-mode");
    return () => document.documentElement.classList.remove("report-mode");
  }, []);

  useEffect(() => {
    if (!signedIn || opened.current) return;
    opened.current = true;
    void open(scanId);
  }, [signedIn, scanId, open]);

  const terminal = ["completed", "degraded", "failed", "awaiting_review"].includes(state.phase) && state.scan?.id === scanId;
  const complete = terminal && state.scan?.status === "completed";
  const loaded = !!state.evidence && !!state.dpdpScore && state.policyAnalysis !== null && state.screenshot !== null;

  // What the page still waits for after the scan itself: the evidence, the score, the policy analysis and the picture load alongside it. A part that
  // cannot be loaded is shown as unavailable, so waiting stops after a short while rather than holding the report back for ever.
  useEffect(() => {
    if (!terminal) return;
    if (loaded) {
      setSettled(true);
      return;
    }
    const t = setTimeout(() => setSettled(true), SETTLE_MS);
    return () => clearTimeout(t);
  }, [terminal, loaded]);

  const model = useMemo(() => (complete && settled ? reportModelFromState(state) : null), [complete, settled, state]);

  useEffect(() => {
    if (!model) return;
    window.__REPORT_META__ = reportHeaderMeta(model);
    document.title = `Privacy Readiness Report - ${window.__REPORT_META__.domain}`;
    window.__REPORT_READY__ = true;
  }, [model]);

  const failure = !signedIn
    ? "Sign in to see this report."
    : terminal && !complete
      ? `This scan did not complete (${state.scan?.status ?? state.phase}), so it has no report.`
      : state.phase === "failed" && !state.scan
        ? state.error ?? "The scan could not be loaded."
        : null;
  useEffect(() => {
    if (failure) window.__REPORT_ERROR__ = failure;
  }, [failure]);

  async function download() {
    setDownloadError(null);
    setDownloading(true);
    try {
      const { blob, filename } = await fetchReportPdf(scanId, variant ?? "long");
      saveBlob(blob, filename);
    } catch (e) {
      setDownloadError(e instanceof Error ? e.message : "Could not make the PDF.");
    } finally {
      setDownloading(false);
    }
  }

  if (!signedIn && !print && !AUTH_BYPASSED) return <LoginScreen onSignedIn={() => setSignedIn(true)} signInHint={null} />;

  return (
    <div className="r-page" data-testid="report-page">
      {!print && (
        <div className="r-toolbar">
          <a className="r-btn" href="/" style={{ background: "#16163a" }}>← Back to the console</a>
          <nav className="r-variants" aria-label="Report">
            <a href={`/?report=${scanId}&variant=summary`} className={variant === "summary" ? "on" : undefined} data-testid="variant-summary">Summary (3 pages)</a>
            <a href={`/?report=${scanId}&variant=free`} className={variant === "free" ? "on" : undefined} data-testid="variant-free">Free Scan (2 pages)</a>
            <a href={`/?report=${scanId}&variant=full`} className={variant === "full" ? "on" : undefined} data-testid="variant-full">Full Scan (8 pages + appendix)</a>
            <a href={`/?report=${scanId}`} className={variant === undefined ? "on" : undefined} data-testid="variant-long">Evidence report</a>
          </nav>
          <span className="r-status" data-testid="report-status">
            {failure ? <span className="r-error">{failure}</span> : model ? "Report ready" : "Loading the report…"}
            {downloadError && <span className="r-error"> {downloadError}</span>}
          </span>
          <button onClick={download} disabled={!model || downloading} data-testid="report-download">{downloading ? "Preparing the PDF…" : "Download PDF"}</button>
        </div>
      )}
      {model ? (variant === "summary" ? <SummaryReport model={model} /> : variant === "free" ? <FreeReport model={model} /> : variant === "full" ? <>{part !== "appendix" && <FullReport model={model} />}{part !== "pages" && <ReportDocument model={model} appendixOnly />}</> : part === "cover" ? <ReportCover model={model} /> : part === "body" ? <ReportDocument model={model} noFront /> : <><ReportCover model={model} /><ReportDocument model={model} noFront /></>) : <div className="r-loading" data-testid="report-loading">{failure ?? "Loading the report…"}</div>}
    </div>
  );
}
