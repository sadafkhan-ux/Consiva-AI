import { act, renderHook, waitFor } from "@testing-library/react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listScans: vi.fn(), createScan: vi.fn(), getScan: vi.fn(), getStages: vi.fn(), getEvidence: vi.fn(),
  getDpdpScore: vi.fn(), analyze: vi.fn(), getFindings: vi.fn(), getAudit: vi.fn(), getPolicyAnalysis: vi.fn(), getScreenshot: vi.fn(),
}));
vi.mock("../api/client", () => ({ api }));

import { RecentScans } from "../components/RecentScans";
import { useConsentScan } from "./useConsentScan";

const stage = (name: string, status = "completed") => ({ stage: name, status, error: null, metadata: {} });
const SCAN = (status: string, extra = {}) => ({ id: "s1", url: "https://x.test/", domain: "x.test", status, error: null, evidence_counts: { pages: 5, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 }, ...extra });
const ANALYSED = [stage("website_scan"), stage("rag_retrieval"), stage("llm_analysis"), stage("findings_generated"), stage("human_review_gate")];

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
  api.getFindings.mockResolvedValue([]);
  api.getAudit.mockResolvedValue([]);
  api.getEvidence.mockResolvedValue(null);
  api.getDpdpScore.mockResolvedValue(null);
  api.getPolicyAnalysis.mockResolvedValue(null);
  api.getScreenshot.mockResolvedValue(null);
});

describe("reopening an earlier scan", () => {
  it("shows a finished, reviewed-pending scan without asking for the analysis again", async () => {
    api.getScan.mockResolvedValue(SCAN("completed"));
    api.getStages.mockResolvedValue(ANALYSED);
    const { result } = renderHook(() => useConsentScan());
    await act(async () => { await result.current.open("s1"); });
    expect(result.current.state.scanId).toBe("s1");
    expect(result.current.state.phase).toBe("awaiting_review");   // audit_saved is not done: the run waits at the review gate
    expect(api.analyze).not.toHaveBeenCalled();
    expect(api.getFindings).toHaveBeenCalledWith("s1");
  });

  it("does not start an analysis for a scan whose analysis never ran, and says so", async () => {
    api.getScan.mockResolvedValue(SCAN("completed"));
    api.getStages.mockResolvedValue([stage("website_scan")]);
    const { result } = renderHook(() => useConsentScan());
    await act(async () => { await result.current.open("s1"); });
    expect(api.analyze).not.toHaveBeenCalled();
    expect(result.current.state.phase).toBe("degraded");
    expect(result.current.state.error).toContain("analysis was not run");
  });

  it("shows a failed scan as failed with its reason, and still loads what it collected", async () => {
    api.getScan.mockResolvedValue(SCAN("failed", { error: "navigation timeout" }));
    api.getStages.mockResolvedValue([stage("website_scan", "failed")]);
    const { result } = renderHook(() => useConsentScan());
    await act(async () => { await result.current.open("s1"); });
    expect(result.current.state.phase).toBe("failed");
    expect(result.current.state.error).toBe("navigation timeout");
    expect(api.getEvidence).toHaveBeenCalledWith("s1");
    expect(api.analyze).not.toHaveBeenCalled();
  });

  it("reports a request failure as a failed phase with a readable reason", async () => {
    api.getScan.mockRejectedValue(new Error("boom"));
    api.getStages.mockResolvedValue([]);
    const { result } = renderHook(() => useConsentScan());
    await act(async () => { await result.current.open("s1"); });
    expect(result.current.state.phase).toBe("failed");
    expect(result.current.state.error).toBe("boom");
  });

  it("loads the scan's homepage preview with the rest, and keeps going when the preview cannot be loaded", async () => {
    const SHOT = { scan_id: "s1", domain: "x.test", status: "ok", available: true, message: null, page_url: "https://x.test/", mime: "image/jpeg", width: 1280, height: 720, captured_at: "2026-10-22T16:43:00Z", data: "QUJD" };
    api.getScan.mockResolvedValue(SCAN("completed"));
    api.getStages.mockResolvedValue(ANALYSED);
    api.getScreenshot.mockResolvedValue(SHOT);
    const a = renderHook(() => useConsentScan());
    await act(async () => { await a.result.current.open("s1"); });
    expect(api.getScreenshot).toHaveBeenCalledWith("s1");
    expect(a.result.current.state.screenshot).toEqual(SHOT);
    api.getScreenshot.mockRejectedValue(new Error("down"));
    const b = renderHook(() => useConsentScan());
    await act(async () => { await b.result.current.open("s1"); });
    expect(b.result.current.state.screenshot).toBeNull();
    expect(b.result.current.state.phase).toBe("awaiting_review");          // the scan is shown regardless
  });

  it("still starts the analysis for a scan the user has just started", async () => {
    api.createScan.mockResolvedValue({ id: "s2", url: "https://y.test/", status: "queued" });
    api.getScan.mockResolvedValue(SCAN("completed", { id: "s2" }));
    api.getStages.mockResolvedValueOnce([stage("website_scan")]).mockResolvedValue(ANALYSED);
    api.analyze.mockResolvedValue({ id: "r1", status: "running" });
    const { result } = renderHook(() => useConsentScan());
    await act(async () => { await result.current.run("https://y.test/", true); });
    await waitFor(() => expect(result.current.state.phase).toBe("awaiting_review"));
    expect(api.analyze).toHaveBeenCalledWith("s2");
  });
});

describe("the recent scans list", () => {
  const ROWS = [
    { id: "a", url: "https://a.test/", status: "completed", created_at: "2026-10-22T10:00:00Z", completed_at: "2026-10-22T10:01:00Z" },
    { id: "b", url: "https://b.test/", status: "failed", created_at: "2026-10-22T09:00:00Z", completed_at: null },
  ];

  it("lists the scans and opens the one chosen", async () => {
    api.listScans.mockResolvedValue(ROWS);
    const onOpen = vi.fn();
    render(<RecentScans busy={false} refreshKey="x" currentScanId={null} onOpen={onOpen} />);
    expect(await screen.findAllByTestId("recent-scan")).toHaveLength(2);
    await userEvent.click(screen.getAllByRole("button", { name: "Open" })[1]);
    expect(onOpen).toHaveBeenCalledWith(ROWS[1]);
  });

  it("disables opening while a scan is running, and marks the one already shown", async () => {
    api.listScans.mockResolvedValue(ROWS);
    const { rerender } = render(<RecentScans busy={true} refreshKey="x" currentScanId={null} onOpen={() => {}} />);
    await screen.findAllByTestId("recent-scan");
    screen.getAllByRole("button").forEach((b) => expect(b).toBeDisabled());
    rerender(<RecentScans busy={false} refreshKey="x" currentScanId="a" onOpen={() => {}} />);
    expect(screen.getByRole("button", { name: "Shown" })).toBeDisabled();
  });

  it("is absent when there are no scans or the list cannot be loaded", async () => {
    api.listScans.mockResolvedValue([]);
    const { container, rerender } = render(<RecentScans busy={false} refreshKey="1" currentScanId={null} onOpen={() => {}} />);
    await waitFor(() => expect(api.listScans).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    api.listScans.mockRejectedValue(new Error("down"));
    rerender(<RecentScans busy={false} refreshKey="2" currentScanId={null} onOpen={() => {}} />);
    await waitFor(() => expect(api.listScans).toHaveBeenCalledTimes(2));
    expect(container).toBeEmptyDOMElement();
  });
});
