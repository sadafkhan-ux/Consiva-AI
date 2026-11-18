import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  createScan: vi.fn(), createApplicationScan: vi.fn(), getScan: vi.fn(), getStages: vi.fn(), getEvidence: vi.fn(), getRequirements: vi.fn(), analyze: vi.fn(), getFindings: vi.fn(),
  getAudit: vi.fn(), getPolicyAnalysis: vi.fn(), getScreenshot: vi.fn(), getAssessment: vi.fn(), cancelScan: vi.fn(),
}));
vi.mock("../api/client", () => ({ api }));

import { ApiError } from "../api/types";
import { useConsentScan } from "./useConsentScan";

const SCAN = (status: string, pages = 2) => ({ id: "s1", url: "https://example.com/", status, error: null, evidence_counts: { pages, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 } });

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
  api.createScan.mockResolvedValue({ id: "s1", url: "https://example.com/", status: "queued" });
  api.getScan.mockResolvedValue(SCAN("running"));
  api.getStages.mockResolvedValue([]);
  api.analyze.mockResolvedValue({ id: "r1", status: "queued" });
  api.getFindings.mockResolvedValue([]);
  api.getAudit.mockResolvedValue([]);
  api.getEvidence.mockResolvedValue(null);
  api.getRequirements.mockResolvedValue(null);
  api.getPolicyAnalysis.mockResolvedValue(null);
  api.getAssessment.mockResolvedValue(null);
  api.getScreenshot.mockResolvedValue(null);
});

// The scan page polls in the background; once the test is over the scan is made terminal so that loop ends.
afterEach(() => {
  api.getScan.mockResolvedValue(SCAN("cancelled"));
});

async function runningScan() {
  const hook = renderHook(() => useConsentScan());
  act(() => { void hook.result.current.run("https://example.com/", true); });
  await waitFor(() => expect(hook.result.current.state.scanId).toBe("s1"));
  await waitFor(() => expect(hook.result.current.state.scan?.status).toBe("running"));
  return hook;
}

describe("stopping a scan", () => {
  it("calls cancelScan once and ends in the cancelled phase, with no error", async () => {
    api.cancelScan.mockResolvedValue({ scan_id: "s1", status: "cancelled", message: "stopped" });
    const { result } = await runningScan();
    api.getScan.mockResolvedValue(SCAN("cancelled", 3));
    await act(async () => { await result.current.stop(); });
    expect(api.cancelScan).toHaveBeenCalledTimes(1);
    expect(api.cancelScan).toHaveBeenCalledWith("s1");
    expect(result.current.state.phase).toBe("cancelled");
    expect(result.current.state.stopping).toBe(false);
    expect(result.current.state.error).toBeNull();
    expect(result.current.state.scan?.evidence_counts.pages).toBe(3);
  });

  it("sends one request when the button is clicked twice", async () => {
    let finish: (v: unknown) => void = () => undefined;
    api.cancelScan.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const { result } = await runningScan();
    api.getScan.mockResolvedValue(SCAN("cancelled"));
    await act(async () => { void result.current.stop(); void result.current.stop(); });
    expect(api.cancelScan).toHaveBeenCalledTimes(1);
    await act(async () => { finish({ scan_id: "s1", status: "cancelled", message: "" }); });
    await waitFor(() => expect(result.current.state.phase).toBe("cancelled"));
    expect(api.cancelScan).toHaveBeenCalledTimes(1);
  });

  it("is in the stopping state until the server has answered", async () => {
    let finish: (v: unknown) => void = () => undefined;
    api.cancelScan.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const { result } = await runningScan();
    api.getScan.mockResolvedValue(SCAN("cancelled"));
    act(() => { void result.current.stop(); });
    await waitFor(() => expect(result.current.state.stopping).toBe(true));
    expect(result.current.state.phase).toBe("scanning");
    await act(async () => { finish({ scan_id: "s1", status: "cancelled", message: "" }); });
    await waitFor(() => expect(result.current.state.phase).toBe("cancelled"));
    expect(result.current.state.stopping).toBe(false);
  });

  it("on 409 for a scan that already finished, shows its real state and no error", async () => {
    api.cancelScan.mockRejectedValue(new ApiError(409, "Scan is already completed."));
    const { result } = await runningScan();
    api.getScan.mockResolvedValue(SCAN("completed"));
    await act(async () => { await result.current.stop(); });
    expect(result.current.state.scan?.status).toBe("completed");
    expect(result.current.state.stopNote).toContain("already finished");
    expect(result.current.state.stopping).toBe(false);
    expect(result.current.state.phase).not.toBe("failed");
    expect(result.current.state.phase).not.toBe("cancelled");
    expect(result.current.state.error).toBeNull();
  });

  it("on 409 for a scan that was already cancelled, shows it as stopped", async () => {
    api.cancelScan.mockRejectedValue(new ApiError(409, "Scan is already cancelled."));
    const { result } = await runningScan();
    api.getScan.mockResolvedValue(SCAN("cancelled"));
    await act(async () => { await result.current.stop(); });
    await waitFor(() => expect(result.current.state.phase).toBe("cancelled"));
    expect(result.current.state.stopNote).toBeNull();
  });

  it("on any other error keeps the scan running and says so briefly", async () => {
    api.cancelScan.mockRejectedValue(new ApiError(500, "boom"));
    const { result } = await runningScan();
    await act(async () => { await result.current.stop(); });
    expect(result.current.state.phase).toBe("scanning");
    expect(result.current.state.stopping).toBe(false);
    expect(result.current.state.stopNote).toContain("could not be stopped");
    expect(result.current.state.stopNote).toContain("still running");
    // The button works again: a second attempt is sent.
    api.cancelScan.mockResolvedValue({ scan_id: "s1", status: "cancelled", message: "" });
    api.getScan.mockResolvedValue(SCAN("cancelled"));
    await act(async () => { await result.current.stop(); });
    expect(api.cancelScan).toHaveBeenCalledTimes(2);
    expect(result.current.state.phase).toBe("cancelled");
  });

  it("starts the same scan again after a stop", async () => {
    api.cancelScan.mockResolvedValue({ scan_id: "s1", status: "cancelled", message: "" });
    const { result } = await runningScan();
    api.getScan.mockResolvedValue(SCAN("cancelled"));
    await act(async () => { await result.current.stop(); });
    expect(result.current.state.phase).toBe("cancelled");
    api.createScan.mockClear();
    await act(async () => { void result.current.scanAgain(); });
    expect(api.createScan).toHaveBeenCalledWith("https://example.com/", true);
  });
});
