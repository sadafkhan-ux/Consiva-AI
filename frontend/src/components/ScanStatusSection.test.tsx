import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConsentScanState } from "../hooks/useConsentScan";
import { pollUntil } from "../hooks/useConsentScan";
import type { WebsiteScanMetadata } from "../api/types";
import { ScanStatusSection } from "./ScanStatusSection";

function makeState(overrides: Partial<ConsentScanState> = {}): ConsentScanState {
  return {
    phase: "scanning",
    scanId: "b4569e0b-f1ec-4fa4-980f-269961d29676",
    scan: null,
    stages: [],
    websiteScanMeta: null,
    findings: [],
    audit: [],
    evidence: null,
    error: null,
    failedStage: null,
    startedAt: Date.now(),
    completedAt: null,
    durationMs: null,
    ...overrides,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("the 5-page limit is stated, not silent", () => {
  const limited: WebsiteScanMetadata = {
    page_limit: 5, stop_reason: "page_limit", pages_scanned: 5, pages_discovered: 9,
    pages_not_attempted_budget_exhausted: 4,
  };

  it("says so when the limit cut the scan short, with how many pages were left", () => {
    render(<ScanStatusSection state={makeState({ phase: "completed", websiteScanMeta: limited })} />);

    const notice = screen.getByText(/Page limit reached/);
    expect(notice).toHaveTextContent("a scan covers at most 5 pages per scan");
    expect(notice).toHaveTextContent("only the first 5 were scanned");
    expect(notice).toHaveTextContent("4 more pages found on the site were not scanned");
    expect(notice).toHaveAttribute("role", "status");
  });

  it("uses the singular for one skipped page", () => {
    render(
      <ScanStatusSection
        state={makeState({ phase: "completed", websiteScanMeta: { ...limited, pages_not_attempted_budget_exhausted: 1 } })}
      />,
    );
    expect(screen.getByText(/Page limit reached/)).toHaveTextContent("1 more page found on the site was not scanned");
  });

  it.each([
    ["a site that fit inside the limit", { page_limit: 5, stop_reason: "queue_exhausted", pages_scanned: 3 }],
    ["a scan with no metadata yet", null],
    ["a scan stopped for another reason", { page_limit: 5, stop_reason: "cancelled" }],
  ])("says nothing about a limit for %s", (_name, meta) => {
    render(<ScanStatusSection state={makeState({ phase: "completed", websiteScanMeta: meta as WebsiteScanMetadata | null })} />);
    expect(screen.queryByText(/Page limit reached/)).not.toBeInTheDocument();
  });
});

describe("pages that could not be loaded are not counted as scanned", () => {
  it("says a partial scan is partial, and how many pages were lost", () => {
    render(
      <ScanStatusSection
        state={makeState({
          phase: "completed",
          websiteScanMeta: { pages_attempted: 5, pages_scanned: 3, pages_failed: 1, pages_timeout: 1, page_limit: 5, stop_reason: "queue_exhausted" },
        })}
      />,
    );

    const notice = screen.getByText(/Partial scan/);
    expect(notice).toHaveTextContent("2 pages of the 5 attempted could not be loaded and are not part of the results");
    expect(notice).toHaveAttribute("role", "status");
  });

  it("says nothing when every attempted page loaded", () => {
    render(
      <ScanStatusSection
        state={makeState({ phase: "completed", websiteScanMeta: { pages_attempted: 3, pages_scanned: 3, pages_failed: 0, pages_timeout: 0 } })}
      />,
    );
    expect(screen.queryByText(/Partial scan/)).not.toBeInTheDocument();
  });
});

describe("a long scan is not shown as a failure", () => {
  it("after two minutes it says the scan is still running and has not failed", () => {
    render(<ScanStatusSection state={makeState({ startedAt: Date.now() - 5 * 60_000 })} />);

    expect(screen.getByText(/Still scanning/)).toHaveTextContent("has not failed");
    expect(screen.getByText("Scanning…")).toBeInTheDocument();
    expect(screen.queryByText(/Failed/)).not.toBeInTheDocument();
  });

  it("does not say it for a scan that has only just started", () => {
    render(<ScanStatusSection state={makeState({ startedAt: Date.now() - 5_000 })} />);
    expect(screen.queryByText(/Still scanning/)).not.toBeInTheDocument();
  });

  it("a scan that really failed shows the failure and its reason, not a success", () => {
    render(
      <ScanStatusSection
        state={makeState({
          phase: "failed",
          error: "Scan of https://smartians.ai/ exceeded its 1800s budget and was aborted so the worker could continue with other jobs.",
          completedAt: Date.now(),
        })}
      />,
    );
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Yes")).toBeInTheDocument(); // Errors / Warnings
    expect(screen.queryByText(/Still scanning/)).not.toBeInTheDocument();
  });
});

describe("pollUntil has no client-side time limit when none is given", () => {
  it("keeps polling past ten minutes while the backend says the scan is running, then returns the result", async () => {
    vi.useFakeTimers();
    let calls = 0;
    const fn = vi.fn(async () => {
      calls += 1;
      return { status: calls < 700 ? "running" : "completed" };
    });

    const result = pollUntil(fn, (s) => s.status === "completed", Number.POSITIVE_INFINITY, 1_200);
    // 700 polls x 1.2 s = 14 minutes: past the old 600 s ceiling.
    await vi.advanceTimersByTimeAsync(700 * 1_200 + 1_000);

    await expect(result).resolves.toEqual({ status: "completed" });
    expect(calls).toBe(700);
  });

  it("still gives up at a finite ceiling when one is passed (the analysis poll keeps its own)", async () => {
    vi.useFakeTimers();
    const fn = vi.fn(async () => ({ status: "running" }));

    const result = pollUntil(fn, (s) => s.status === "completed", 5_000, 1_000);
    const assertion = expect(result).rejects.toThrow(/Timed out waiting for the backend/);
    await vi.advanceTimersByTimeAsync(7_000);

    await assertion;
  });

  it("ends when a request fails instead of looping on an unreachable backend", async () => {
    const fn = vi.fn(async () => {
      throw new Error("Network error");
    });
    await expect(pollUntil(fn, () => false, Number.POSITIVE_INFINITY, 10)).rejects.toThrow("Network error");
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

describe("stage timing is reconciled with the run's total", () => {
  const row = (stage: string, status: string, duration_ms: number | null) => ({ stage, status, duration_ms, error: null, metadata: {} });

  it("sums repeated stages instead of showing only the last attempt", async () => {
    const { summariseStages } = await import("./stageTiming");
    const out = summariseStages([
      row("llm_analysis", "completed", 14_107), row("output_validation", "completed", 0), row("llm_analysis", "completed", 3_294),
    ] as never);

    expect(out.llm_analysis).toEqual({ status: "completed", durationMs: 17_401, attempts: 2 });
    expect(out.output_validation.attempts).toBe(1);
  });

  it("reports the time no stage accounts for, from the real scan ac537cef numbers", async () => {
    const { summariseStages, unaccountedMs } = await import("./stageTiming");
    const stages = [
      row("url_validation", "completed", 18), row("website_scan", "completed", 26_542), row("classification", "completed", 1),
      row("data_structuring", "completed", 76), row("normalize_evidence", "completed", 36), row("rules_check", "completed", 0),
      row("rag_retrieval", "completed", 216_780), row("llm_analysis", "completed", 14_107), row("output_validation", "completed", 0),
      row("llm_analysis", "completed", 3_294), row("output_validation", "completed", 0), row("findings_generated", "completed", 15),
      row("human_review_gate", "completed", 4),
    ];

    const gap = unaccountedMs(264_890, summariseStages(stages as never));

    expect(gap).toBe(264_890 - 260_873);   // ~4s of queue hand-off; the 14.1s first LLM attempt is no longer in the gap
  });

  it("is unknown, not zero, when the total is unknown", async () => {
    const { unaccountedMs } = await import("./stageTiming");
    expect(unaccountedMs(null, {})).toBeNull();
  });
});

describe("empty knowledge base notice", () => {
  const stateWith = (metadata: Record<string, unknown>) =>
    ({
      phase: "awaiting_review", scanId: "ac537cef", scan: null, stages: [
        { stage: "rag_retrieval", status: "completed", duration_ms: 5, error: null, metadata },
      ],
      startedAt: null, completedAt: null, durationMs: null, websiteScanMeta: null, error: null, failedStage: null,
    }) as never;

  it("tells the reader the findings cannot cite provisions", () => {
    render(<ScanStatusSection state={stateWith({ knowledge_base_empty: true })} />);
    expect(screen.getByTestId("kb-empty")).toHaveTextContent("knowledge base is empty");
  });

  it("shows nothing when retrieval had a populated knowledge base", () => {
    render(<ScanStatusSection state={stateWith({ chunks_retrieved: 3 })} />);
    expect(screen.queryByTestId("kb-empty")).not.toBeInTheDocument();
  });
});
