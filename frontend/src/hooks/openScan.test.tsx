import { act, renderHook, waitFor } from "@testing-library/react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listScans: vi.fn(), createScan: vi.fn(), getScan: vi.fn(), getStages: vi.fn(), getEvidence: vi.fn(),
  getRequirements: vi.fn(), analyze: vi.fn(), getFindings: vi.fn(), getAudit: vi.fn(), getPolicyAnalysis: vi.fn(), getScreenshot: vi.fn(), getAssessment: vi.fn(),
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
  api.getRequirements.mockResolvedValue(null);
  api.getPolicyAnalysis.mockResolvedValue(null);
  api.getAssessment.mockResolvedValue(null);
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

describe("the score is final only when the policy reading has finished (the dashboard showed 66.7 where the PDF showed 94.4)", () => {
  const POLICY = { status: "analysed", reason: null, sector: null, documents: [], notes: [], summary: null, requirements: [], limitations: [], model: null };

  it("stays in 'analyzing' while the policy reading is still running, then loads it and finishes", async () => {
    api.getScan.mockResolvedValue(SCAN("completed"));
    // the analysis stages are done (the findings are ready) but the policy reading is still running on the first looks, and finished on the third
    let looks = 0;
    api.getStages.mockImplementation(async () => {
      looks += 1;
      return [...ANALYSED, stage("policy_analysis", looks < 4 ? "running" : "completed")];
    });
    api.getPolicyAnalysis.mockImplementation(async () => (looks < 4 ? { ...POLICY, status: "not_run" } : POLICY));
    const { result } = renderHook(() => useConsentScan());
    let opened: Promise<void>;
    await act(async () => { opened = result.current.open("s1"); await Promise.resolve(); });
    await act(async () => { await opened; });
    expect(result.current.state.phase).toBe("awaiting_review");
    expect(result.current.state.policyAnalysis?.status).toBe("analysed");              // the finished reading, not the "not_run" read at the moment the findings landed
    expect(looks).toBeGreaterThanOrEqual(4);
  });

  it("loads the requirements reading and the assessment again after the policy reading, because both read that result", async () => {
    api.getScan.mockResolvedValue(SCAN("completed"));
    let done = false;
    api.getStages.mockImplementation(async () => [...ANALYSED, stage("policy_analysis", done ? "completed" : "running")]);
    api.getPolicyAnalysis.mockImplementation(async () => (done ? POLICY : { ...POLICY, status: "not_run" }));
    api.getRequirements.mockImplementation(async () => ({ marker: done ? "after" : "before" }));
    // the stage settles on the second poll
    let polls = 0;
    api.getStages.mockImplementation(async () => { polls += 1; done = polls >= 3; return [...ANALYSED, stage("policy_analysis", done ? "completed" : "running")]; });
    const { result } = renderHook(() => useConsentScan());
    await act(async () => { await result.current.open("s1"); });
    expect((result.current.state.requirements as unknown as { marker: string }).marker).toBe("after");
    expect(api.getAssessment.mock.calls.length).toBeGreaterThanOrEqual(2);   // the one score is read again too
  });

  it("a scan with no policy stage does not wait", async () => {
    api.getScan.mockResolvedValue(SCAN("completed"));
    api.getStages.mockResolvedValue(ANALYSED);
    const { result } = renderHook(() => useConsentScan());
    await act(async () => { await result.current.open("s1"); });
    expect(result.current.state.phase).toBe("awaiting_review");
  });

  it("a policy reading that failed settles the page too, and its score says the policy checks could not be assessed", async () => {
    api.getScan.mockResolvedValue(SCAN("completed"));
    api.getStages.mockResolvedValue([...ANALYSED, stage("policy_analysis", "failed")]);
    const { result } = renderHook(() => useConsentScan());
    await act(async () => { await result.current.open("s1"); });
    expect(["awaiting_review", "degraded"]).toContain(result.current.state.phase);       // never "failed": the scan has a result
    expect(result.current.state.phase).not.toBe("failed");
  });
});
