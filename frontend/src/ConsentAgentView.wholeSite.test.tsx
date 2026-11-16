import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ConsentScanState } from "./hooks/useConsentScan";

let current: Partial<ConsentScanState> = {};
const run = vi.fn();
vi.mock("./hooks/useConsentScan", async (orig) => {
  const real = await orig<typeof import("./hooks/useConsentScan")>();
  const base = { phase: "idle", scanId: null, scan: null, stages: [], websiteScanMeta: null, findings: [], audit: [], evidence: null, dpdpScore: null,
    error: null, failedStage: null, startedAt: null, completedAt: null, durationMs: null };
  return { ...real, useConsentScan: () => ({ state: { ...base, ...current }, run, open: vi.fn(), afterReviewDecision: vi.fn() }) };
});
const pdf = vi.hoisted(() => ({ fetchReportPdf: vi.fn(), saveBlob: vi.fn() }));
vi.mock("./api/client", async (orig) => ({ ...(await orig<typeof import("./api/client")>()), fetchReportPdf: pdf.fetchReportPdf, saveBlob: pdf.saveBlob }));

import { ConsentAgentView } from "./ConsentAgentView";

describe("the Enterprise screen: the same agent, over the whole website", () => {
  beforeEach(() => { current = {}; run.mockReset(); pdf.fetchReportPdf.mockReset(); pdf.saveBlob.mockReset(); });

  it("starts a whole-site scan (an empty crawl request: the server's own limits) instead of the five-page one", () => {
    render(<ConsentAgentView wholeSite />);
    expect(screen.getByText("Enterprise Scanner")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("https://example.com"), { target: { value: "https://swaransoft.com" } });
    fireEvent.click(screen.getByTestId("scan-whole-website"));
    expect(run).toHaveBeenCalledWith("https://swaransoft.com", true, {});
    expect(screen.queryByRole("button", { name: "Scan Website" })).toBeNull();
    expect(screen.queryByTestId("crawl-settings")).toBeNull();                  // no second whole-site form inside the whole-site screen
  });

  it("will not scan until ownership is attested", () => {
    render(<ConsentAgentView wholeSite />);
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByTestId("scan-whole-website")).toBeDisabled();
  });

  it("offers the DETAILED report and fetches the long variant", async () => {
    current = { phase: "completed", scanId: "s1", findings: [], startedAt: 1, completedAt: 2 };
    pdf.fetchReportPdf.mockResolvedValue({ blob: new Blob(["x"]), filename: "r.pdf" });
    render(<ConsentAgentView wholeSite />);
    fireEvent.click(screen.getByRole("button", { name: "Download Detailed Report (PDF)" }));
    await vi.waitFor(() => expect(pdf.fetchReportPdf).toHaveBeenCalledWith("s1", "long"));
    expect(pdf.saveBlob).toHaveBeenCalled();
  });

  it("leaves the standard agent exactly as it was: five-page scan, summary report", async () => {
    current = { phase: "completed", scanId: "s1", findings: [], startedAt: 1, completedAt: 2 };
    pdf.fetchReportPdf.mockResolvedValue({ blob: new Blob(["x"]), filename: "r.pdf" });
    render(<ConsentAgentView />);
    expect(screen.getByText("Privacy Policy Agent")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Scan Website" })).toBeInTheDocument();
    expect(screen.getByTestId("crawl-settings")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Download Report (PDF)" }));
    await vi.waitFor(() => expect(pdf.fetchReportPdf).toHaveBeenCalledWith("s1", "summary"));
  });
});
