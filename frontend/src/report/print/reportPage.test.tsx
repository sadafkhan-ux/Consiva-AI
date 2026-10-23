import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DpdpScoreResponse, PolicyAnalysis, ScanEvidenceResponse, ScanScreenshot } from "../../api/types";
import type { ConsentScanState } from "../../hooks/useConsentScan";
import fixtures from "../../test/dpdpScoreFixtures.json";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
const SCAN_ID = "11111111-1111-1111-1111-111111111111";

let current: Partial<ConsentScanState> = {};
const open = vi.fn();
vi.mock("../../hooks/useConsentScan", async (orig) => {
  const real = await orig<typeof import("../../hooks/useConsentScan")>();
  const base = { phase: "idle", scanId: null, scan: null, stages: [], websiteScanMeta: null, findings: [], audit: [], evidence: null, dpdpScore: null, policyAnalysis: null, screenshot: null, error: null, failedStage: null, startedAt: null, completedAt: null, durationMs: null };
  return { ...real, useConsentScan: () => ({ state: { ...base, ...current }, run: vi.fn(), open, afterReviewDecision: vi.fn() }) };
});
let token: string | null = "tok";
vi.mock("../../api/auth", async (orig) => ({ ...(await orig<typeof import("../../api/auth")>()), getToken: () => token }));
const fetchReportPdf = vi.fn();
const saveBlob = vi.fn();
vi.mock("../../api/client", async (orig) => ({ ...(await orig<typeof import("../../api/client")>()), fetchReportPdf: (id: string) => fetchReportPdf(id), saveBlob: (b: Blob, n: string) => saveBlob(b, n) }));

import { ReportPage } from "./ReportPage";

const EVIDENCE = {
  pages: [{ id: "p1", url: "https://x.test/", title: "Home", http_status: 200, discovered_via: "seed" }], forms: [], cookies: [], trackers: [], third_party_services: [],
  policies: [{ id: "pol", url: "https://x.test/privacy", policy_type: "privacy_policy" }], consent_signals: [],
} as unknown as ScanEvidenceResponse;
const POLICY = { status: "not_run", reason: null, sector: null, documents: [], notes: [], limitations: [], model: null, summary: null, requirements: [] } as unknown as PolicyAnalysis;
const SHOT = { scan_id: SCAN_ID, domain: "x.test", status: "not_captured", available: false, message: "No preview.", page_url: null, mime: null, width: null, height: null, captured_at: null, data: null } as ScanScreenshot;
const COMPLETE = { phase: "completed", scanId: SCAN_ID, scan: { id: SCAN_ID, url: "https://x.test/", status: "completed", evidence_counts: { pages: 1, cookies: 0, trackers: 0, third_party_services: 0, policies: 1, forms: 0 } }, evidence: EVIDENCE, dpdpScore: F.mixed, policyAnalysis: POLICY, screenshot: SHOT } as unknown as Partial<ConsentScanState>;

beforeEach(() => {
  current = {};
  token = "tok";
  open.mockReset(); fetchReportPdf.mockReset(); saveBlob.mockReset();
  delete window.__REPORT_READY__; delete window.__REPORT_ERROR__; delete window.__REPORT_META__;
});
afterEach(() => vi.useRealTimers());

describe("the report page the server prints", () => {
  it("opens the scan once, draws the report, and tells the printer it is ready with the header words and the file name", async () => {
    current = COMPLETE;
    render(<ReportPage scanId={SCAN_ID} print />);
    expect(open).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith(SCAN_ID);
    expect(await screen.findByTestId("rpt")).toBeInTheDocument();
    await waitFor(() => expect(window.__REPORT_READY__).toBe(true));
    expect(window.__REPORT_META__!.domain).toBe("x.test");
    expect(window.__REPORT_META__!.scanned).toMatch(/^Scanned .+ UTC/);
    expect(window.__REPORT_META__!.filename).toMatch(/^consiva-privacy-report-x\.test-\d{4}-\d{2}-\d{2}\.pdf$/);
    expect(document.title).toBe("Privacy Compliance Report - x.test");
    expect(window.__REPORT_ERROR__).toBeUndefined();
  });

  it("draws no toolbar in the printed version", () => {
    current = COMPLETE;
    render(<ReportPage scanId={SCAN_ID} print />);
    expect(screen.queryByTestId("report-download")).not.toBeInTheDocument();
  });

  it("is not ready until the scan has finished loading, then waits a bounded time for the rest and draws what it has", async () => {
    vi.useFakeTimers();
    current = { ...COMPLETE, evidence: null, dpdpScore: null, policyAnalysis: null, screenshot: null };
    render(<ReportPage scanId={SCAN_ID} print />);
    expect(window.__REPORT_READY__).toBeUndefined();
    expect(screen.getByTestId("report-loading")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(8_100); });
    expect(window.__REPORT_READY__).toBe(true);
    expect(screen.getByTestId("pg-percent-free").textContent).toBe("No score");                    // an unavailable part is shown as unavailable, never invented
  });

  it("says a scan that did not complete has no report, and signals the printer at once instead of leaving it to time out", async () => {
    current = { phase: "failed", scanId: SCAN_ID, scan: { id: SCAN_ID, url: "https://x.test/", status: "failed" } as never, error: "boom" };
    render(<ReportPage scanId={SCAN_ID} print />);
    expect(screen.getByTestId("report-loading")).toHaveTextContent("did not complete");
    await waitFor(() => expect(window.__REPORT_ERROR__).toMatch(/did not complete/));
    expect(window.__REPORT_READY__).toBeUndefined();
  });

  it("asks to sign in, and never loads anything, when there is no session", async () => {
    token = null;
    render(<ReportPage scanId={SCAN_ID} print />);
    expect(screen.getByTestId("report-loading")).toHaveTextContent("Sign in");
    expect(open).not.toHaveBeenCalled();
    await waitFor(() => expect(window.__REPORT_ERROR__).toMatch(/Sign in/));
  });
});

describe("the web report page", () => {
  it("shows the report and a Download PDF button that fetches the server's PDF and saves it under the server's file name", async () => {
    current = COMPLETE;
    const blob = new Blob(["%PDF-1.4"], { type: "application/pdf" });
    fetchReportPdf.mockResolvedValue({ blob, filename: "consiva-privacy-report-x.test-2026-10-23.pdf" });
    render(<ReportPage scanId={SCAN_ID} print={false} />);
    const button = await screen.findByTestId("report-download");
    await waitFor(() => expect(button).toBeEnabled());
    expect(screen.getByTestId("report-status")).toHaveTextContent("Report ready");
    expect(screen.getByTestId("rpt")).toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() => expect(saveBlob).toHaveBeenCalledWith(blob, "consiva-privacy-report-x.test-2026-10-23.pdf"));
    expect(fetchReportPdf).toHaveBeenCalledWith(SCAN_ID);
  });

  it("shows the server's plain error when the PDF cannot be made, and leaves the report on screen", async () => {
    current = COMPLETE;
    fetchReportPdf.mockRejectedValue(new Error("The PDF report could not be prepared. Please try again in a moment."));
    render(<ReportPage scanId={SCAN_ID} print={false} />);
    const button = await screen.findByTestId("report-download");
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(screen.getByTestId("report-status")).toHaveTextContent("could not be prepared"));
    expect(screen.getByTestId("rpt")).toBeInTheDocument();
    expect(saveBlob).not.toHaveBeenCalled();
  });

  it("keeps the download disabled while the report loads", () => {
    current = { phase: "scanning", scanId: SCAN_ID };
    render(<ReportPage scanId={SCAN_ID} print={false} />);
    expect(screen.getByTestId("report-download")).toBeDisabled();
    expect(screen.getByTestId("report-status")).toHaveTextContent("Loading the report");
  });
});
