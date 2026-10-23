import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ScanUsage, UsageHistoryItem, UsageStage } from "../api/types";

const getScanUsage = vi.fn();
const getUsageHistory = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...(await orig<typeof import("../api/client")>()), api: { getScanUsage: (id: string) => getScanUsage(id), getUsageHistory: (n: number) => getUsageHistory(n) } }));

import { fmtCost, fmtTokens, TokenUsagePanel } from "./TokenUsagePanel";

const SCAN = "11111111-1111-1111-1111-111111111111";
const OTHER = "22222222-2222-2222-2222-222222222222";

const stage = (over: Partial<UsageStage>): UsageStage => ({
  stage: "x", label: "X", kind: "non_llm", status: "completed", runs: 1, duration_ms: 1000, models: [], note: null, calls: 0, failed_calls: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0,
  cache_write_tokens: 0, total_tokens: 0, reasoning_tokens: 0, priced_calls: 0, unpriced_calls: 0, cost: 0, embedding_requests: null, embedding_tokens: null, ...over,
});
const usage = (over: Partial<ScanUsage> = {}): ScanUsage => ({
  scan_id: SCAN, url: "https://shop.example/", domain: "shop.example", scan_status: "completed", state: "completed", current_stage: null, created_at: "2026-10-23T06:43:00Z", completed_at: "2026-10-23T06:44:00Z",
  pages_scanned: 3, source: "measured", analysis_run: true,
  totals: { calls: 2, failed_calls: 0, input_tokens: 18154, output_tokens: 5353, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 23507, reasoning_tokens: 4229, priced_calls: 2, unpriced_calls: 0, cost: 0.00449, embedding_tokens: 16 },
  cost: { currency: "USD", total: 0.00449, input: 0.0018154, output: 0.0026765, cache_read: 0, cache_write: 0, priced_calls: 2, unpriced_calls: 0, complete: true, price_source: "platform.claude.com/docs/en/about-claude/pricing, fetched 2026-10-23" },
  stages: [
    stage({ stage: "website_scan", label: "Website scan (browser)" }),
    stage({ stage: "policy_analysis", label: "Privacy policy analysis", kind: "llm", calls: 1, input_tokens: 12106, output_tokens: 4989, total_tokens: 17095, reasoning_tokens: 3875, models: ["claude/claude-haiku-5-5"], cost: 0.0037, priced_calls: 1 }),
    stage({ stage: "rag_retrieval", label: "Regulatory provision retrieval", kind: "embedding", embedding_requests: 1, embedding_tokens: 16, cost: null }),
    stage({ stage: "llm_analysis", label: "Compliance reasoning, findings and recommendations", kind: "llm", calls: 1, input_tokens: 6048, output_tokens: 364, total_tokens: 6412, reasoning_tokens: 354, models: ["claude/claude-haiku-5-5"], cost: 0.00079, priced_calls: 1 }),
  ],
  pages: {
    attributed: [{ url: "https://shop.example/privacy", calls: 1, input_tokens: 12106, output_tokens: 4989, total_tokens: 17095, cost: 0.0037 }],
    shared: [{ label: "Compliance reasoning, findings and recommendations: read 3 pages together", reason: "One model call read all of these pages, so its tokens cannot be split between them.", stage: "llm_analysis",
      page_urls: ["https://shop.example/", "https://shop.example/contact", "https://shop.example/privacy"], calls: 1, input_tokens: 6048, output_tokens: 364, total_tokens: 6412, cost: 0.00079 }],
    unattributed: null,
  },
  notes: ["Report and PDF generation use no LLM: the page is drawn from stored data and printed by a browser."], ...over,
});
const hist = (over: Partial<UsageHistoryItem> = {}): UsageHistoryItem => ({
  scan_id: OTHER, url: "https://old.example/", domain: "old.example", status: "completed", created_at: "2026-10-20T10:00:00Z", completed_at: "2026-10-20T10:01:00Z", pages_scanned: 5, calls: 2,
  failed_calls: 0, input_tokens: 9000, output_tokens: 1000, total_tokens: 10000, cost: 0.0014, cost_complete: true, currency: "USD", source: "measured", analysis_run: true, ...over,
});

beforeEach(() => {
  getScanUsage.mockReset(); getUsageHistory.mockReset();
  getUsageHistory.mockResolvedValue([]);
  getScanUsage.mockResolvedValue(usage());
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

const panel = (props: Partial<Parameters<typeof TokenUsagePanel>[0]> = {}) => render(<TokenUsagePanel scanId={SCAN} live={false} status="completed" {...props} />);

describe("the numbers", () => {
  it("formats nothing it was not given: unavailable shows a dash, never zero", () => {
    expect(fmtTokens(null)).toBe("—"); expect(fmtTokens(undefined)).toBe("—"); expect(fmtTokens(0)).toBe("0"); expect(fmtTokens(18154)).toBe("18,154");
    expect(fmtCost(null)).toBe("—"); expect(fmtCost(0.00449)).toBe("$0.00449"); expect(fmtCost(0.5)).toBe("$0.5000"); expect(fmtCost(1.2, "EUR")).toBe("EUR 1.2000");
  });

  it("shows the totals the server sent: tokens, calls, cached, reasoning and cost, marked measured", async () => {
    panel();
    expect(await screen.findByTestId("usage-total")).toHaveTextContent("23,507");
    expect(screen.getByTestId("usage-calls")).toHaveTextContent("2");
    expect(screen.getByTestId("usage-calls")).toHaveTextContent("none failed");
    expect(screen.getByTestId("usage-input")).toHaveTextContent("18,154");
    expect(screen.getByTestId("usage-output")).toHaveTextContent("5,353");
    expect(screen.getByTestId("usage-cached")).toHaveTextContent("none used");
    expect(screen.getByTestId("usage-reasoning")).toHaveTextContent("4,229");
    expect(screen.getByTestId("usage-reasoning")).toHaveTextContent("inside output");
    expect(screen.getByTestId("usage-source")).toHaveTextContent("Measured");
    expect(screen.getByTestId("usage-source")).toHaveTextContent("embedding 16 tokens (not LLM)");
  });

  it("shows reasoning as not reported, not zero, when no call reported it", async () => {
    getScanUsage.mockResolvedValue(usage({ totals: { ...usage().totals, reasoning_tokens: null } }));
    panel();
    expect(await screen.findByTestId("usage-reasoning")).toHaveTextContent("not reported");
    expect(screen.getByTestId("usage-reasoning")).not.toHaveTextContent("0");
  });

  it("shows cache reads and writes when they were used", async () => {
    getScanUsage.mockResolvedValue(usage({ totals: { ...usage().totals, cache_read_tokens: 10_000, cache_write_tokens: 2_000 } }));
    panel();
    expect(await screen.findByTestId("usage-cached")).toHaveTextContent("10,000 read");
    expect(screen.getByTestId("usage-cached")).toHaveTextContent("2,000 written");
  });

  it("states cost in its currency with input, cached, output and total, says it is calculated, and names the price source", async () => {
    panel();
    const cost = await screen.findByTestId("usage-cost");
    expect(cost).toHaveTextContent("Input$0.00182");
    expect(cost).toHaveTextContent("Output$0.00268");
    expect(screen.getByTestId("usage-cost-total")).toHaveTextContent("$0.00449");
    expect(cost.textContent).toContain("Total AI cost (USD)");
    expect(screen.getByText(/platform\.claude\.com/)).toBeInTheDocument();
    expect(screen.getAllByTestId("usage-mark-calculated").length).toBeGreaterThan(0);
  });

  it("says a call with no verified price has tokens but no cost", async () => {
    getScanUsage.mockResolvedValue(usage({ cost: { ...usage().cost, total: null, input: null, output: null, cache_read: null, unpriced_calls: 1, complete: false, price_source: null } }));
    panel();
    expect(await screen.findByTestId("usage-cost-total")).toHaveTextContent("—");
    expect(screen.getByTestId("usage-unpriced")).toHaveTextContent("no verified price");
    expect(screen.getAllByTestId("usage-mark-unavailable").length).toBeGreaterThan(0);
  });

  it("marks usage unavailable, not zero, for a scan that never recorded any", async () => {
    getScanUsage.mockResolvedValue(usage({ source: "none", totals: { ...usage().totals, calls: 0, total_tokens: 0, input_tokens: 0, output_tokens: 0, reasoning_tokens: null, embedding_tokens: null }, stages: [stage({ stage: "llm_analysis", label: "Compliance reasoning", kind: "llm", total_tokens: null, input_tokens: null, output_tokens: null, cost: null })] }));
    panel();
    expect(await screen.findByTestId("usage-total")).toHaveTextContent("—");
    expect(screen.getByTestId("usage-calls")).toHaveTextContent("—");
    expect(screen.getByTestId("usage-source")).toHaveTextContent("Unavailable");
  });

  it("says when the numbers come from the stage records of a scan made before per-call tracking", async () => {
    getScanUsage.mockResolvedValue(usage({ source: "stage_records" }));
    panel();
    expect(await screen.findByTestId("usage-source")).toHaveTextContent("From stage records");
    expect(screen.getAllByText("from the stage record").length).toBeGreaterThan(0);
  });
});

describe("by stage", () => {
  it("lists each stage that ran with its own calls and tokens", async () => {
    panel();
    const policy = await screen.findByTestId("usage-stage-policy_analysis");
    expect(policy).toHaveTextContent("Privacy policy analysis");
    expect(policy).toHaveTextContent("claude/claude-haiku-5-5");
    expect(policy).toHaveTextContent("12,106"); expect(policy).toHaveTextContent("4,989"); expect(policy).toHaveTextContent("17,095"); expect(policy).toHaveTextContent("$0.00370");
    const analysis = screen.getByTestId("usage-stage-llm_analysis");
    expect(analysis).toHaveTextContent("6,048"); expect(analysis).toHaveTextContent("364"); expect(analysis).toHaveTextContent("6,412");
  });

  it("shows zero LLM tokens for a confirmed non-LLM stage and says so", async () => {
    panel();
    const scanStage = await screen.findByTestId("usage-stage-website_scan");
    expect(scanStage).toHaveTextContent("Not an LLM stage");
    expect(within(scanStage).getAllByRole("cell").slice(1, 5).map((c) => c.textContent)).toEqual(["0", "0", "0", "0"]);
  });

  it("shows the embedding stage as not an LLM, with its own measured tokens", async () => {
    panel();
    const emb = await screen.findByTestId("usage-stage-rag_retrieval");
    expect(emb).toHaveTextContent("Embedding model, not an LLM");
    expect(screen.getByTestId("usage-embedding")).toHaveTextContent("Embedding tokens: 16 (measured)");
  });

  it("shows a model stage with no recorded usage as unavailable, and one that is waiting as running", async () => {
    getScanUsage.mockResolvedValue(usage({ stages: [stage({ stage: "llm_analysis", label: "Compliance reasoning", kind: "llm", status: "running", calls: 0, input_tokens: null, output_tokens: null, total_tokens: null, cost: null }), stage({ stage: "policy_analysis", label: "Privacy policy analysis", kind: "llm", total_tokens: null, input_tokens: null, output_tokens: null, cost: null })] }));
    panel({ live: true, status: "running" });
    const waiting = await screen.findByTestId("usage-stage-llm_analysis");
    expect(waiting).toHaveTextContent("waiting for the model");
    expect(waiting.className).toContain("running");
    expect(within(waiting).getAllByRole("cell").slice(1, 5).map((c) => c.textContent)).toEqual(["—", "—", "—", "—"]);
    expect(within(screen.getByTestId("usage-stage-policy_analysis")).getAllByRole("cell")[4].textContent).toBe("—");
  });

  it("counts failed calls on their stage", async () => {
    getScanUsage.mockResolvedValue(usage({ stages: [stage({ stage: "llm_analysis", label: "Compliance reasoning", kind: "llm", calls: 3, failed_calls: 1, input_tokens: 10, output_tokens: 1, total_tokens: 11, cost: 0 })] }));
    panel();
    expect(await screen.findByTestId("usage-stage-llm_analysis")).toHaveTextContent("1 failed");
  });
});

describe("by page", () => {
  it("shows a page's own usage only where one call read just that page, and a shared call as shared", async () => {
    panel();
    const pages = await screen.findByTestId("usage-pages");
    fireEvent.click(within(pages).getByText(/By page/));
    const row = within(pages).getByTestId("usage-page-row");
    expect(row).toHaveTextContent("/privacy"); expect(row).toHaveTextContent("17,095");
    const shared = within(pages).getByTestId("usage-shared-row");
    expect(shared).toHaveTextContent("Shared:"); expect(shared).toHaveTextContent("cannot be split between them"); expect(shared).toHaveTextContent("/contact");
    expect(pages).toHaveTextContent("3 scanned");
  });

  it("labels calls whose pages were not recorded as unattributed and invents no page rows", async () => {
    getScanUsage.mockResolvedValue(usage({ pages: { attributed: [], shared: [], unattributed: { label: "Not attributed to a page", reason: "The pages these calls read were not recorded.", stage: null, page_urls: [], calls: 2, input_tokens: 5, output_tokens: 1, total_tokens: 6, cost: null } } }));
    panel();
    const pages = await screen.findByTestId("usage-pages");
    expect(within(pages).getByTestId("usage-unattributed-row")).toHaveTextContent("not recorded");
    expect(within(pages).queryByTestId("usage-page-row")).toBeNull();
  });
});

describe("live updates", () => {
  it("polls while the scan runs, shows each new total, and stops after one last read when it ends", async () => {
    vi.useFakeTimers();
    let n = 0;
    getScanUsage.mockImplementation(async () => { n += 1; return usage({ state: n < 3 ? "running" : "completed", current_stage: n < 3 ? "Privacy policy analysis" : null, totals: { ...usage().totals, calls: n, total_tokens: 1000 * n, input_tokens: 800 * n } }); });
    const { rerender } = render(<TokenUsagePanel scanId={SCAN} live status="running" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(50); });
    expect(screen.getByTestId("usage-total")).toHaveTextContent("1,000");
    expect(screen.getByTestId("usage-live")).toBeInTheDocument();
    expect(screen.getByTestId("usage-current-stage")).toHaveTextContent("Privacy policy analysis");
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByTestId("usage-total")).toHaveTextContent("2,000");
    expect(screen.getByTestId("usage-calls")).toHaveTextContent("2");
    rerender(<TokenUsagePanel scanId={SCAN} live={false} status="completed" />);            // the scan ended: one last read
    await act(async () => { await vi.advanceTimersByTimeAsync(50); });
    expect(screen.getByTestId("usage-total")).toHaveTextContent("3,000");
    expect(screen.queryByTestId("usage-live")).toBeNull();
    const calls = getScanUsage.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(getScanUsage.mock.calls.length).toBe(calls);                                       // and no more reads
    expect(screen.getByTestId("usage-status")).toHaveTextContent("Scan finished");
  });

  it("does not ask again while the last read is still on its way, so a slow server is never piled up", async () => {
    vi.useFakeTimers();
    getScanUsage.mockImplementation(() => new Promise(() => {}));
    render(<TokenUsagePanel scanId={SCAN} live status="running" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(getScanUsage).toHaveBeenCalledTimes(1);
  });

  it("keeps the numbers already shown when a later read fails, and says so", async () => {
    vi.useFakeTimers();
    getScanUsage.mockResolvedValueOnce(usage()).mockRejectedValue(new Error("Network error"));
    render(<TokenUsagePanel scanId={SCAN} live status="running" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(50); });
    expect(screen.getByTestId("usage-total")).toHaveTextContent("23,507");
    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(screen.getByTestId("usage-total")).toHaveTextContent("23,507");
    expect(screen.getByTestId("usage-error")).toHaveTextContent("Network error");
  });

  it("shows a failed scan's usage as it stood, with the failed status", async () => {
    getScanUsage.mockResolvedValue(usage({ state: "failed", scan_status: "failed" }));
    panel({ status: "failed" });
    expect(await screen.findByTestId("usage-total")).toHaveTextContent("23,507");
    expect(screen.getByTestId("usage-status")).toHaveTextContent("Scan failed");
  });
});

describe("separate scans", () => {
  it("never shows one scan's numbers under another scan's id", async () => {
    const { rerender } = panel();
    expect(await screen.findByTestId("usage-total")).toHaveTextContent("23,507");
    let release: (u: ScanUsage) => void = () => {};
    getScanUsage.mockReturnValue(new Promise<ScanUsage>((r) => { release = r; }));
    rerender(<TokenUsagePanel scanId={OTHER} live={false} status="completed" />);
    expect(screen.queryByTestId("usage-total")).toBeNull();                                  // gone at once, not left on screen
    expect(await screen.findByTestId("usage-loading")).toBeInTheDocument();
    await act(async () => { release(usage({ scan_id: OTHER, domain: "other.example", totals: { ...usage().totals, total_tokens: 55 } })); });
    expect(await screen.findByTestId("usage-total")).toHaveTextContent("55");
  });

  it("ignores a reply that belongs to a different scan", async () => {
    getScanUsage.mockResolvedValue(usage({ scan_id: OTHER }));
    panel();
    await waitFor(() => expect(getScanUsage).toHaveBeenCalled());
    expect(screen.queryByTestId("usage-total")).toBeNull();
  });

  it("asks for the scan it was given, and for nothing when there is no scan", async () => {
    panel();
    await waitFor(() => expect(getScanUsage).toHaveBeenCalledWith(SCAN));
    cleanup(); getScanUsage.mockReset();
    panel({ scanId: null, status: "idle" });
    expect(await screen.findByTestId("usage-empty")).toHaveTextContent("Run a scan");
    expect(getScanUsage).not.toHaveBeenCalled();
    expect(screen.getByTestId("usage-status")).toHaveTextContent("No scan");
  });
});

describe("history", () => {
  it("lists earlier scans with their domain, id, date, pages, calls, tokens, cost and status", async () => {
    getUsageHistory.mockResolvedValue([hist()]);
    panel();
    const row = await screen.findByTestId("usage-history-row");
    expect(row).toHaveTextContent("old.example"); expect(row).toHaveTextContent("22222222"); expect(row).toHaveTextContent("5 pages"); expect(row).toHaveTextContent("20 Oct");
    expect(row).toHaveTextContent("2 calls"); expect(row).toHaveTextContent("9,000 in"); expect(row).toHaveTextContent("1,000 out"); expect(row).toHaveTextContent("10,000 total"); expect(row).toHaveTextContent("$0.00140");
    expect(row).toHaveTextContent("completed");
  });

  it("says usage was not recorded for a scan that never recorded it, and where an older scan's numbers come from", async () => {
    getUsageHistory.mockResolvedValue([hist({ source: "none", calls: 0, total_tokens: 0, cost: null }), hist({ scan_id: "33333333-3333-3333-3333-333333333333", source: "stage_records" })]);
    panel();
    const rows = await screen.findAllByTestId("usage-history-row");
    expect(rows[0]).toHaveTextContent("usage not recorded");
    expect(rows[0]).not.toHaveTextContent("0 calls");
    expect(rows[1]).toHaveTextContent("from stage records");
  });

  it("says a scan that was only crawled ran no AI analysis, which is not the same as usage that went missing", async () => {
    getUsageHistory.mockResolvedValue([hist({ source: "none", analysis_run: false, calls: 0, total_tokens: 0, cost: null })]);
    panel();
    const row = await screen.findByTestId("usage-history-row");
    expect(row).toHaveTextContent("no AI analysis run");
    expect(row).not.toHaveTextContent("usage not recorded");
  });

  it("writes one call, not 1 calls", async () => {
    getUsageHistory.mockResolvedValue([hist({ calls: 1 }), hist({ scan_id: "33333333-3333-3333-3333-333333333333", calls: 2 })]);
    panel();
    const rows = await screen.findAllByTestId("usage-history-row");
    expect(rows[0]).toHaveTextContent("1 call");
    expect(rows[0]).not.toHaveTextContent("1 calls");
    expect(rows[1]).toHaveTextContent("2 calls");
  });

  it("marks a partly priced total with a plus and an unpriced one as cost unavailable", async () => {
    getUsageHistory.mockResolvedValue([hist({ cost_complete: false }), hist({ scan_id: "33333333-3333-3333-3333-333333333333", cost: null })]);
    panel();
    const rows = await screen.findAllByTestId("usage-history-row");
    expect(rows[0]).toHaveTextContent("$0.00140+");
    expect(rows[1]).toHaveTextContent("cost unavailable");
  });

  it("opens an earlier scan's usage in the panel without touching the scan on screen, and goes back", async () => {
    getUsageHistory.mockResolvedValue([hist()]);
    getScanUsage.mockImplementation(async (id: string) => (id === OTHER ? usage({ scan_id: OTHER, domain: "old.example", totals: { ...usage().totals, total_tokens: 10_000 } }) : usage()));
    panel({ live: false });
    fireEvent.click(await screen.findByTestId("usage-history-open"));
    expect(await screen.findByTestId("usage-viewing")).toHaveTextContent("Viewing an earlier scan: old.example");
    await waitFor(() => expect(screen.getByTestId("usage-total")).toHaveTextContent("10,000"));
    expect(getScanUsage).toHaveBeenCalledWith(OTHER);
    fireEvent.click(within(screen.getByTestId("usage-viewing")).getByText("Back to the current scan"));
    await waitFor(() => expect(screen.getByTestId("usage-total")).toHaveTextContent("23,507"));
    expect(screen.queryByTestId("usage-viewing")).toBeNull();
  });

  it("does not poll an earlier scan that is opened while another scan is running", async () => {
    vi.useFakeTimers();
    getUsageHistory.mockResolvedValue([hist()]);
    getScanUsage.mockImplementation(async (id: string) => usage({ scan_id: id }));
    render(<TokenUsagePanel scanId={SCAN} live status="running" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(50); });
    fireEvent.click(screen.getByTestId("usage-history-open"));
    await act(async () => { await vi.advanceTimersByTimeAsync(50); });
    getScanUsage.mockClear();
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(getScanUsage).not.toHaveBeenCalled();                                              // the finished scan viewed is read once, not polled
  });

  it("shows a history error without breaking the rest of the panel", async () => {
    getUsageHistory.mockRejectedValue(new Error("history down"));
    panel();
    expect(await screen.findByText("history down")).toBeInTheDocument();
    expect(await screen.findByTestId("usage-total")).toBeInTheDocument();
  });
});

describe("the panel itself", () => {
  it("can be hidden and shown again", async () => {
    panel();
    await screen.findByTestId("usage-total");
    fireEvent.click(screen.getByLabelText("Hide the usage panel"));
    expect(screen.queryByTestId("usage-total")).toBeNull();
    fireEvent.click(screen.getByTestId("usage-open"));
    expect(await screen.findByTestId("usage-total")).toBeInTheDocument();
  });

  it("explains measured, calculated and unavailable, and says token counts are never estimated", async () => {
    panel();
    expect(await screen.findByTestId("usage-legend")).toHaveTextContent("Token counts are never estimated");
  });

  it("states what is not an LLM call: the notes the server sent, including that reports use none", async () => {
    panel();
    expect(await screen.findByText(/Report and PDF generation use no LLM/)).toBeInTheDocument();
  });
});
