import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ScanUsage, UsageCall, UsageHistoryItem, UsageStage } from "../api/types";

const getScanUsage = vi.fn();
const getUsageHistory = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...(await orig<typeof import("../api/client")>()), api: { getScanUsage: (id: string) => getScanUsage(id), getUsageHistory: (n: number, o?: number) => getUsageHistory(n, o) } }));

import { TokenUsagePanel } from "./TokenUsagePanel";

const SCAN = "0024ef1e-0000-4000-8000-000000000001";
const OLD = "4a0f13c8-0000-4000-8000-000000000002";

const stage = (over: Partial<UsageStage>): UsageStage => ({
  stage: "x", label: "X", kind: "non_llm", status: "completed", runs: 1, duration_ms: 1000, models: [], note: null, calls: 0, failed_calls: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0,
  cache_write_tokens: 0, total_tokens: 0, reasoning_tokens: 0, priced_calls: 0, unpriced_calls: 0, cost: 0, embedding_requests: null, embedding_tokens: null, ...over,
});
const call = (over: Partial<UsageCall> = {}): UsageCall => ({
  stage: "llm_analysis", label: "Compliance reasoning, findings and recommendations", agent: "consent", provider: "claude", model: "claude-haiku-5-5", status: "success", attempts: 1, input_tokens: 6807,
  output_tokens: 226, cache_read_tokens: 0, cache_write_tokens: 0, reasoning_tokens: 0, total_tokens: 7033, duration_ms: 2500, cost: 0.0008, page_urls: [], error_type: null, at: null, ...over,
});
// swaransoft.com, scan 0024ef1e: the policy analysis was reused (no model call), so exactly one call and 7,033 tokens.
const usage = (over: Partial<ScanUsage> = {}): ScanUsage => ({
  scan_id: SCAN, url: "https://swaransoft.com/", domain: "swaransoft.com", scan_status: "completed", state: "completed", current_stage: null, created_at: "2026-10-28T06:28:53Z", completed_at: "2026-10-28T06:30:00Z",
  pages_scanned: 5, source: "measured", analysis_run: true,
  totals: { calls: 1, failed_calls: 0, input_tokens: 6807, output_tokens: 226, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 7033, reasoning_tokens: 0, priced_calls: 1, unpriced_calls: 0, cost: 0.0008, embedding_tokens: 35 },
  cost: { currency: "USD", total: 0.0008, input: 0.0007, output: 0.0001, cache_read: 0, cache_write: 0, priced_calls: 1, unpriced_calls: 0, complete: true, price_source: "pricing page" },
  stages: [
    stage({ stage: "policy_analysis", label: "Privacy policy analysis", kind: "llm", reused: true, reused_from_scan: "f9f1c31d-037c-4e32-9c0f-6b4aea67193d", note: "Reused the saved analysis of an identical policy text from scan f9f1c31d: no model call, zero tokens." }),
    stage({ stage: "llm_analysis", label: "Compliance reasoning, findings and recommendations", kind: "llm", calls: 1, input_tokens: 6807, output_tokens: 226, total_tokens: 7033, models: ["claude/claude-haiku-5-5"], cost: 0.0008 }),
  ],
  pages: { attributed: [], shared: [], unattributed: null }, notes: [], calls: [call()], errors: [], results: { evidence: true, analysis: true, findings: true, audit: true }, ...over,
});
const hist = (over: Partial<UsageHistoryItem> = {}): UsageHistoryItem => ({
  scan_id: OLD, url: "https://swaransoft.com/", domain: "swaransoft.com", status: "completed", created_at: "2026-10-27T13:12:21Z", completed_at: "2026-10-27T13:13:00Z", pages_scanned: 5, calls: 2, failed_calls: 0,
  input_tokens: 52000, output_tokens: 8294, total_tokens: 60294, cost: 0.01, cost_complete: true, currency: "USD", source: "measured", analysis_run: true, errors: [],
  results: { evidence: true, analysis: true, findings: true, audit: false }, reused_analysis: false, ...over,
});

beforeEach(() => {
  getScanUsage.mockReset(); getUsageHistory.mockReset();
  getUsageHistory.mockResolvedValue([]);
  getScanUsage.mockResolvedValue(usage());
});
afterEach(() => cleanup());

const panel = (props: Partial<Parameters<typeof TokenUsagePanel>[0]> = {}) => render(<TokenUsagePanel scanId={SCAN} live={false} status="completed" {...props} />);

describe("every metric opens its breakdown", () => {
  it.each([["usage-total", "Total tokens"], ["usage-calls", "AI calls"], ["usage-input", "Input tokens"], ["usage-output", "Output tokens"], ["usage-cached", "Cached input"], ["usage-reasoning", "Reasoning tokens"]])(
    "%s shows the %s detail and closes again", async (testid, title) => {
      panel();
      fireEvent.click(await screen.findByTestId(testid));
      expect(within(screen.getByTestId("usage-detail")).getByText(title, { selector: "b" })).toBeInTheDocument();
      fireEvent.click(screen.getByTestId("usage-detail-close"));
      expect(screen.queryByTestId("usage-detail")).toBeNull();
    });

  it("lists the recorded calls with model, attempts and duration", async () => {
    panel();
    fireEvent.click(await screen.findByTestId("usage-calls"));
    const t = screen.getByTestId("usage-detail-calls");
    expect(t).toHaveTextContent("claude/claude-haiku-5-5"); expect(t).toHaveTextContent("1 attempt"); expect(t).toHaveTextContent("2.5 s");
  });

  it("the by-stage breakdown of the total adds up to the total shown", async () => {
    panel();
    fireEvent.click(await screen.findByTestId("usage-total"));
    expect(screen.getByTestId("usage-detail-stages")).toHaveTextContent("7,033");
  });

  it("clicking the same metric again closes its breakdown", async () => {
    panel();
    fireEvent.click(await screen.findByTestId("usage-input"));
    fireEvent.click(screen.getByTestId("usage-input"));
    expect(screen.queryByTestId("usage-detail")).toBeNull();
  });
});

describe("every stage opens its breakdown", () => {
  it("a reused policy analysis says it was reused from which scan, with zero tokens, and is not shown as missing", async () => {
    panel();
    const row = await screen.findByTestId("usage-stage-policy_analysis");
    expect(row).toHaveTextContent("Reused saved analysis"); expect(row).not.toHaveTextContent("model not recorded");
    fireEvent.click(row);
    const d = screen.getByTestId("usage-detail");
    expect(d).toHaveTextContent("f9f1c31d-037c-4e32-9c0f-6b4aea67193d"); expect(screen.getByTestId("usage-detail-note")).toHaveTextContent("no model call, zero tokens");
  });

  it("an LLM stage lists its own calls, and a stage error is shown", async () => {
    getScanUsage.mockResolvedValue(usage({ stages: [stage({ stage: "llm_analysis", label: "Analysis", kind: "llm", status: "failed", error: "Schema validation failed", calls: 1, total_tokens: 7033 })] }));
    panel();
    fireEvent.click(await screen.findByTestId("usage-stage-llm_analysis"));
    expect(screen.getByTestId("usage-detail-error")).toHaveTextContent("Schema validation failed");
    expect(screen.getByTestId("usage-detail-calls")).toHaveTextContent("claude-haiku-5-5");
  });
});

describe("errors", () => {
  it("shows the errors recorded for the scan, and nothing when there are none", async () => {
    getScanUsage.mockResolvedValue(usage({ errors: [{ where: "Scan", stage: null, message: "The analysis did not finish" }] }));
    panel();
    expect(await screen.findByTestId("usage-errors")).toHaveTextContent("The analysis did not finish");
    cleanup();
    getScanUsage.mockResolvedValue(usage());
    panel();
    await screen.findByTestId("usage-total");
    expect(screen.queryByTestId("usage-errors")).toBeNull();
  });
});

describe("the scan history", () => {
  it("shows the URL, date, status, full scan id, saved results and errors of each scan", async () => {
    getUsageHistory.mockResolvedValue([hist({ errors: [{ where: "Scan", stage: null, message: "timed out" }] })]);
    panel();
    const row = await screen.findByTestId("usage-history-row");
    expect(row).toHaveTextContent("https://swaransoft.com/"); expect(row).toHaveTextContent("27 Oct"); expect(row).toHaveTextContent("completed");
    expect(within(row).getByTestId("usage-history-id")).toHaveTextContent(OLD);
    expect(within(row).getByTestId("usage-history-saved")).toHaveTextContent("findings"); expect(within(row).getByTestId("usage-history-error")).toHaveTextContent("timed out");
    expect(row).toHaveTextContent("60,294 total");
  });

  it("clicking a scan opens it in the console when the console can open it", async () => {
    getUsageHistory.mockResolvedValue([hist()]);
    const onOpenScan = vi.fn();
    panel({ onOpenScan });
    fireEvent.click(await screen.findByTestId("usage-history-row"));
    expect(onOpenScan).toHaveBeenCalledWith(OLD);
  });

  it("without the console (or while a scan runs) it shows that scan's usage instead, as before", async () => {
    getUsageHistory.mockResolvedValue([hist()]);
    getScanUsage.mockImplementation(async (id: string) => usage({ scan_id: id }));
    panel();
    fireEvent.click(await screen.findByTestId("usage-history-open"));
    expect(await screen.findByTestId("usage-viewing")).toBeInTheDocument();
    expect(getScanUsage).toHaveBeenCalledWith(OLD);
  });

  it("pages back through older scans until there are no more", async () => {
    const page = (n: number, from: number) => Array.from({ length: n }, (_, i) => hist({ scan_id: `scan-${from + i}`, url: `https://site${from + i}.test/` }));
    getUsageHistory.mockImplementation(async (_n: number, offset?: number) => (offset ? page(3, 20) : page(20, 0)));
    panel();
    fireEvent.click(await screen.findByTestId("usage-history-more"));
    await waitFor(() => expect(screen.getAllByTestId("usage-history-row")).toHaveLength(23));
    expect(getUsageHistory).toHaveBeenLastCalledWith(20, 20);
    expect(screen.queryByTestId("usage-history-more")).toBeNull();
  });
});
