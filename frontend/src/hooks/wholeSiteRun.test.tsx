import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  createScan: vi.fn(), createApplicationScan: vi.fn(), getScan: vi.fn(), getStages: vi.fn(), getEvidence: vi.fn(), getRequirements: vi.fn(),
  analyze: vi.fn(), getFindings: vi.fn(), getAudit: vi.fn(), getPolicyAnalysis: vi.fn(), getScreenshot: vi.fn(), getAssessment: vi.fn(),
}));
vi.mock("../api/client", () => ({ api }));

import { ApiError } from "../api/types";
import { useConsentScan } from "./useConsentScan";

const stage = (name: string, status = "completed") => ({ stage: name, status, error: null, metadata: {} });
const SCAN = { id: "s1", url: "https://x.test/", domain: "x.test", status: "completed", error: null, evidence_counts: { pages: 151, cookies: 3, trackers: 2, third_party_services: 1, policies: 1, forms: 1 } };
const ANALYSED = [stage("website_scan"), stage("rag_retrieval"), stage("llm_analysis"), stage("findings_generated"), stage("human_review_gate")];

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
  api.getFindings.mockResolvedValue([]);
  api.getAudit.mockResolvedValue([]);
  api.getEvidence.mockResolvedValue({ pages: [], cookies: [], trackers: [], forms: [], policies: [], third_party_services: [] });
  api.getRequirements.mockResolvedValue(null);
  api.getPolicyAnalysis.mockResolvedValue(null);
  api.getAssessment.mockResolvedValue(null);
  api.getScreenshot.mockResolvedValue(null);
  api.createScan.mockResolvedValue({ id: "s1", url: "https://x.test/", status: "queued" });
  api.createApplicationScan.mockResolvedValue({ id: "s1", url: "https://x.test/", status: "queued" });
  api.getScan.mockResolvedValue(SCAN);
  api.getStages.mockResolvedValue(ANALYSED);
  api.analyze.mockResolvedValue({ id: "run", status: "queued" });
});

describe("a whole-site scan: the server crawls AND analyses it in one job", () => {
  it("does not ask for the analysis again (the server refuses with 409), and loads the results", async () => {
    // What the server really answers if it is asked: the analysis is already running, so this must never be reached.
    api.analyze.mockRejectedValue(new ApiError(409, "Conflicts with the scan's current state"));
    const { result } = renderHook(() => useConsentScan());
    await act(async () => { await result.current.run("https://x.test", true, {}); });
    expect(api.createApplicationScan).toHaveBeenCalledWith("https://x.test", true, {});
    expect(api.analyze).not.toHaveBeenCalled();
    expect(result.current.state.phase).toBe("awaiting_review");         // reached the review gate with its results, not "failed"
    expect(result.current.state.error).toBeNull();
    expect(api.getEvidence).toHaveBeenCalledWith("s1");                // the results were actually loaded
    expect(result.current.state.evidence).not.toBeNull();
  });

  it("waits for analysis stages that the server has not written yet when the crawl is reported finished", async () => {
    let calls = 0;
    api.getStages.mockImplementation(async () => (++calls < 4 ? [stage("website_scan")] : ANALYSED));      // crawl done, analysis not started, then it runs
    const { result } = renderHook(() => useConsentScan());
    await act(async () => { await result.current.run("https://x.test", true, {}); });
    expect(result.current.state.phase).toBe("awaiting_review");
    expect(api.analyze).not.toHaveBeenCalled();
  });
});

describe("the standard (five-page) scan is unchanged", () => {
  it("still asks for the analysis once the crawl is done", async () => {
    const { result } = renderHook(() => useConsentScan());
    await act(async () => { await result.current.run("https://x.test", true); });
    expect(api.createScan).toHaveBeenCalledWith("https://x.test", true);
    expect(api.analyze).toHaveBeenCalledWith("s1");
  });
});
