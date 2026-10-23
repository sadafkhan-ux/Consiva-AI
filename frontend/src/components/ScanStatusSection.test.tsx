import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConsentScanState } from "../hooks/useConsentScan";
import { pollUntil, readableError } from "../hooks/useConsentScan";
import { ApiError } from "../api/types";
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

describe("polling survives a dropped connection", () => {
  const down = () => new ApiError(0, "Network error");

  it("retries after a network error and carries on to the result", async () => {
    let calls = 0;
    const fn = vi.fn(async () => {
      calls += 1;
      if (calls <= 2) throw down();
      return calls;
    });

    await expect(pollUntil(fn, (n) => n >= 4, Number.POSITIVE_INFINITY, 5)).resolves.toBe(4);
    expect(fn).toHaveBeenCalledTimes(4);
  });

  it("gives up once the outage outlasts the grace period", async () => {
    const fn = vi.fn(async () => { throw down(); });

    await expect(pollUntil(fn, () => false, Number.POSITIVE_INFINITY, 5, 30)).rejects.toBeInstanceOf(ApiError);
  });

  it.each([502, 503, 504])("treats a %s from the gateway during a restart as an outage, not as the scan failing", async (status) => {
    let calls = 0;
    const fn = vi.fn(async () => {
      calls += 1;
      if (calls <= 2) throw new ApiError(status, "<html><head><title>Bad Gateway</title></head></html>");
      return calls;
    });
    await expect(pollUntil(fn, (n) => n >= 4, Number.POSITIVE_INFINITY, 5)).resolves.toBe(4);
  });

  it("shows a sentence, not an HTML error page, as the reason", () => {
    const text = readableError(new ApiError(502, "<html> <head><title>502 Bad Gateway</title></head> <body>nginx</body> </html>"));
    expect(text).not.toContain("<");
    expect(text).toContain("did not answer (502)");
    expect(readableError(new ApiError(422, "URL is not allowed"))).toBe("URL is not allowed");
  });

  it("does not retry an error that is not a lost connection", async () => {
    const fn = vi.fn(async () => { throw new ApiError(500, "boom"); });

    await expect(pollUntil(fn, () => false, Number.POSITIVE_INFINITY, 5)).rejects.toThrow("boom");
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("a sleeping tab is not counted as time spent waiting", async () => {
    // The clock jumps an hour between two rounds, as when a laptop wakes. The 600 s ceiling must not fire.
    const real = Date.now();
    let offset = 0;
    vi.spyOn(Date, "now").mockImplementation(() => real + offset);
    let calls = 0;
    const fn = vi.fn(async () => {
      calls += 1;
      if (calls === 2) offset = 3_600_000;
      return calls;
    });

    await expect(pollUntil(fn, (n) => n >= 4, 600_000, 5)).resolves.toBe(4);
    vi.restoreAllMocks();
  });
});

describe("the wait-between-steps note", () => {
  const row = (stage: string, status: string, duration_ms: number) => ({ stage, status, duration_ms, error: null, metadata: {} });
  const state = (phase: string, stages: unknown[]) =>
    ({
      phase, scanId: "d3b6f2f4", scan: null, stages, startedAt: 0, completedAt: 8_413_000, durationMs: 8_413_000,
      websiteScanMeta: null, error: null, failedStage: null,
    }) as never;

  it("is not shown for a failed run whose stage never finished", () => {
    render(<ScanStatusSection state={state("failed", [row("website_scan", "completed", 20_770), row("rag_retrieval", "running", 0)])} />);
    expect(screen.queryByTestId("stage-gap")).not.toBeInTheDocument();
  });

  it("is still shown for a run that finished", () => {
    render(<ScanStatusSection state={state("completed", [row("website_scan", "completed", 20_770)])} />);
    expect(screen.getByTestId("stage-gap")).toBeInTheDocument();
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

describe("duration comes from the server's clock", () => {
  const row = (stage: string, status: string, started_at: string | null, completed_at: string | null, duration_ms: number | null = null) =>
    ({ stage, status, duration_ms, error: null, metadata: {}, started_at, completed_at });

  // Scan d3b6f2f4, as reported: a 21 s pipeline whose RAG stage was still "running", seen by a browser that was
  // away for 2 h 20 m. 17:59:14 -> 20:19:27 is 8,413 s of the page's clock.
  const REAL_STAGES = [
    row("url_validation", "completed", "2026-10-20T12:29:14.000Z", "2026-10-20T12:29:14.213Z", 213),
    row("website_scan", "completed", "2026-10-20T12:29:15.000Z", "2026-10-20T12:29:35.770Z", 20_770),
    row("rules_check", "completed", "2026-10-20T12:29:36.000Z", "2026-10-20T12:29:36.000Z", 0),
    row("rag_retrieval", "running", "2026-10-20T12:29:36.100Z", null),
  ];

  const state = (phase: string, stages: unknown[], browserMs: number | null) =>
    ({
      phase, scanId: "d3b6f2f4", scan: null, stages, startedAt: 0, completedAt: browserMs, durationMs: browserMs,
      websiteScanMeta: null, error: null, failedStage: null,
    }) as never;

  it("serverSpanMs is first start to last end, and runs to 'now' while a stage is running", async () => {
    const { serverSpanMs } = await import("./stageTiming");
    const done = REAL_STAGES.slice(0, 3);

    expect(serverSpanMs(done as never, 0)).toBe(22_000);                          // 12:29:14.000 -> 12:29:36.000
    expect(serverSpanMs(REAL_STAGES as never, Date.parse("2026-10-20T12:29:46.100Z"))).toBe(32_100);
    expect(serverSpanMs([row("a", "completed", null, null)] as never, 0)).toBeNull();   // an older response: no timestamps
  });

  it("shows the server's duration, not the browser's, and says the page waited longer", () => {
    const stages = REAL_STAGES.map((s) => (s.status === "running" ? { ...s, status: "completed", completed_at: "2026-10-20T12:29:40.000Z" } : s));
    render(<ScanStatusSection state={state("completed", stages, 8_413_060)} />);

    expect(screen.getByText("Duration").previousSibling).toHaveTextContent("26.00 s");
    expect(screen.queryByText(/8413/)).not.toBeInTheDocument();
    expect(screen.getByTestId("paused-time")).toHaveTextContent("waited 2 h 20 min longer than the server worked");
  });

  it("falls back to the browser's time when the server sent no timestamps", () => {
    const stages = REAL_STAGES.slice(0, 3).map((s) => ({ ...s, started_at: null, completed_at: null }));
    render(<ScanStatusSection state={state("completed", stages, 42_000)} />);

    expect(screen.getByText("Duration").previousSibling).toHaveTextContent("42.00 s");
    expect(screen.queryByTestId("paused-time")).not.toBeInTheDocument();
  });

  it("warns when a stage has been running for far longer than a healthy run", async () => {
    const { stuckStages } = await import("./stageTiming");
    const now = Date.parse("2026-10-20T14:49:27.000Z");                            // 2 h 20 m later

    expect(stuckStages(REAL_STAGES as never, now)).toEqual([{ stage: "rag_retrieval", runningMs: now - Date.parse("2026-10-20T12:29:36.100Z") }]);
    expect(stuckStages(REAL_STAGES as never, Date.parse("2026-10-20T12:30:00.000Z"))).toEqual([]);   // 24 s in: normal
    expect(stuckStages(REAL_STAGES.slice(0, 3) as never, now)).toEqual([]);                           // nothing running
  });
});
