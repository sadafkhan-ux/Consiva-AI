import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/types";
import type { ConsentScanState } from "../hooks/useConsentScan";

let current: Partial<ConsentScanState> = {};
vi.mock("../hooks/useConsentScan", async (orig) => {
  const real = await orig<typeof import("../hooks/useConsentScan")>();
  const base = { phase: "idle", scanId: null, scan: null, stages: [], websiteScanMeta: null, findings: [], audit: [], evidence: null, dpdpScore: null, policyAnalysis: null, screenshot: null, error: null, failedStage: null, startedAt: null, completedAt: null, durationMs: null };
  return { ...real, useConsentScan: () => ({ state: { ...base, ...current }, run: vi.fn(), open: vi.fn(), afterReviewDecision: vi.fn() }) };
});
const fetchReportPdf = vi.fn();
const saveBlob = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...(await orig<typeof import("../api/client")>()), fetchReportPdf: (id: string) => fetchReportPdf(id), saveBlob: (b: Blob, n: string) => saveBlob(b, n) }));
const downloadReport = vi.fn();
vi.mock("../report/downloadReport", async (orig) => ({ ...(await orig<typeof import("../report/downloadReport")>()), downloadReport: (m: unknown) => downloadReport(m) }));

import { ConsentAgentView } from "../ConsentAgentView";

const BUTTON = /Download Report \(PDF\)/;
const SCAN_ID = "11111111-1111-1111-1111-111111111111";
const done = () => { current = { phase: "completed", scanId: SCAN_ID, findings: [], startedAt: 1, completedAt: 2 }; };

describe("the Download Report button uses the server's PDF API", () => {
  beforeEach(() => { current = {}; fetchReportPdf.mockReset(); saveBlob.mockReset(); downloadReport.mockReset(); });

  it("fetches the scan's PDF from the server and saves it, without drawing a second copy in the browser", async () => {
    done();
    const blob = new Blob(["%PDF-1.4"], { type: "application/pdf" });
    fetchReportPdf.mockResolvedValue({ blob, filename: "consiva-privacy-report-x.test-2026-10-23.pdf" });
    render(<ConsentAgentView />);
    fireEvent.click(screen.getByRole("button", { name: BUTTON }));
    await waitFor(() => expect(saveBlob).toHaveBeenCalledWith(blob, "consiva-privacy-report-x.test-2026-10-23.pdf"));
    expect(fetchReportPdf).toHaveBeenCalledWith(SCAN_ID);
    expect(downloadReport).not.toHaveBeenCalled();
  });

  it.each([[0, "no network"], [500, "server error"], [503, "the printer is unavailable"]])("falls back to the in-browser PDF, from the same report model, on %s (%s)", async (status) => {
    done();
    fetchReportPdf.mockRejectedValue(new ApiError(status, "The PDF report could not be prepared."));
    render(<ConsentAgentView />);
    fireEvent.click(screen.getByRole("button", { name: BUTTON }));
    await waitFor(() => expect(downloadReport).toHaveBeenCalledTimes(1));
    expect(downloadReport.mock.calls[0][0]).toMatchObject({ scanId: null, siteUrl: expect.any(String) });
    expect(saveBlob).not.toHaveBeenCalled();
  });

  it.each([[404, "Scan not found"], [409, "Scan is not completed yet"], [403, "Not authorized"]])("shows the server's answer, and does not fall back, on %s", async (status, detail) => {
    done();
    fetchReportPdf.mockRejectedValue(new ApiError(status, detail));
    render(<ConsentAgentView />);
    fireEvent.click(screen.getByRole("button", { name: BUTTON }));
    await waitFor(() => expect(screen.getByText(detail)).toBeInTheDocument());
    expect(downloadReport).not.toHaveBeenCalled();
  });

  it("links to the web report of the open scan, in a new tab", () => {
    done();
    render(<ConsentAgentView />);
    const link = screen.getByTestId("open-web-report");
    expect(link).toHaveAttribute("href", `/?report=${SCAN_ID}`);
    expect(link).toHaveAttribute("target", "_blank");
  });
});
