import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { enterpriseDashboard, type DashboardScan, type DashboardSummary } from "../api/enterprise";
import { ScanDashboard } from "./ScanDashboard";

const scan = (o: Partial<DashboardScan> = {}): DashboardScan => ({
  agent: "enterprise", scan_id: "s1", website_url: "https://a.test/", outcome: "completed", detail: "completed", reason: null,
  started_at: "2026-11-16T10:00:00Z", finished_at: "2026-11-16T10:03:30Z", data_cleared: false, ...o,
});
const summary = (items: DashboardScan[], o: Partial<DashboardSummary["kpis"]> = {}): DashboardSummary => ({
  kpis: { scans_total: 4, scans_completed: 2, scans_failed: 1, scans_pending: 1, scans_blocked: 0, scans_five_page: 2, scans_enterprise: 2, ...o },
  last_scan: items[0] ?? null, history: { items, total: items.length, offset: 0, limit: 10 },
  definitions: { completed: "the scan completed", failed: "the scan failed or was cancelled", pending: "queued or still running", blocked: "the website refused the scanner" }, cached: false,
});
const MIXED = [
  scan(),
  scan({ agent: "five_page", scan_id: "s2", website_url: "https://b.test/", outcome: "failed", detail: "failed", reason: "the site refused the connection", data_cleared: false }),
  scan({ agent: "enterprise", scan_id: "s3", website_url: "https://c.test/", outcome: "pending", detail: "crawling", finished_at: null }),
  scan({ agent: "five_page", scan_id: "s4", website_url: "https://d.test/", outcome: "completed", detail: "completed" }),
  scan({ agent: "enterprise", scan_id: "s5", website_url: "https://e.test/", data_cleared: true }),
];

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("ScanDashboard", () => {
  it("shows a blocked scan as blocked by the website, with its reason, not as a failure", async () => {
    const blocked = scan({ scan_id: "s9", website_url: "https://blocked.test/", outcome: "blocked", detail: "Website blocked our scanner.", reason: "ACCESS_BLOCKED: Website blocked our scanner. HTTP 403 from the first page." });
    vi.spyOn(enterpriseDashboard, "summary").mockResolvedValue(summary([blocked], { scans_total: 1, scans_blocked: 1, scans_failed: 0, scans_completed: 0, scans_pending: 0 }));
    render(<ScanDashboard onOpen={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("scan-dashboard")).toBeInTheDocument());
    expect(screen.getByTestId("kpi-blocked")).toHaveTextContent("1");
    expect(screen.getByTestId("kpi-failed")).toHaveTextContent("0");
    expect(screen.getByTestId("scan-list")).toHaveTextContent("Website blocked our scanner.");
    expect(screen.getByTestId("scan-list")).toHaveTextContent("HTTP 403");
  });

  it("shows the counts of both agents from the server's numbers", async () => {
    vi.spyOn(enterpriseDashboard, "summary").mockResolvedValue(summary(MIXED));
    render(<ScanDashboard onOpen={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("scan-dashboard")).toBeInTheDocument());
    expect(screen.getByTestId("kpi-total")).toHaveTextContent("4");
    expect(screen.getByTestId("kpi-completed")).toHaveTextContent("2");
    expect(screen.getByTestId("kpi-failed")).toHaveTextContent("1");
    expect(screen.getByTestId("kpi-pending")).toHaveTextContent("1");
    expect(screen.getByTestId("kpi-five-page")).toHaveTextContent("2");
    expect(screen.getByTestId("kpi-enterprise")).toHaveTextContent("2");
    expect(screen.getByTestId("kpi-last")).toHaveTextContent("completed");
  });

  it("lists website, agent, status, start and finish for both agents, and the reason a scan failed", async () => {
    vi.spyOn(enterpriseDashboard, "summary").mockResolvedValue(summary(MIXED));
    render(<ScanDashboard onOpen={() => {}} />);
    const rows = within(await screen.findByTestId("scan-list")).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("a.test");
    expect(rows[0]).toHaveTextContent("Enterprise");
    expect(rows[1]).toHaveTextContent("Five-page");
    expect(rows[1]).toHaveTextContent("failed");
    expect(rows[1]).toHaveTextContent("the site refused the connection");
    expect(rows[2]).toHaveTextContent("crawling");
    expect(rows[0]).toHaveTextContent("3m 30s");
  });

  it("says 'No reason recorded' for a failed scan without one, and shows no reason for a completed one", async () => {
    vi.spyOn(enterpriseDashboard, "summary").mockResolvedValue(summary([scan({ outcome: "failed", detail: "failed", reason: null }), scan({ scan_id: "x" })]));
    render(<ScanDashboard onOpen={() => {}} />);
    const rows = within(await screen.findByTestId("scan-list")).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("No reason recorded");
    expect(rows[1]).not.toHaveTextContent("No reason recorded");
  });

  it("opens only an Enterprise scan whose data still exists: never a five-page scan, never a cleared one", async () => {
    vi.spyOn(enterpriseDashboard, "summary").mockResolvedValue(summary(MIXED));
    const open = vi.fn();
    render(<ScanDashboard onOpen={open} />);
    const table = await screen.findByTestId("scan-list");
    expect(within(table).getAllByRole("button", { name: /Open scan of/ })).toHaveLength(2);
    fireEvent.click(within(table).getAllByRole("row")[1]);
    expect(open).toHaveBeenLastCalledWith("s1");
    open.mockClear();
    fireEvent.click(within(table).getAllByRole("row")[2]);          // a five-page scan
    fireEvent.click(within(table).getAllByRole("row")[5]);          // an Enterprise scan whose data was cleared
    expect(open).not.toHaveBeenCalled();
  });

  it("clicking a status card filters the history by it, and clicking it again clears the filter", async () => {
    const spy = vi.spyOn(enterpriseDashboard, "summary").mockResolvedValue(summary(MIXED));
    render(<ScanDashboard onOpen={() => {}} />);
    await screen.findByTestId("scan-list");
    fireEvent.click(screen.getByTestId("kpi-failed"));
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(expect.objectContaining({ outcome: "failed" })));
    expect(screen.getByTestId("kpi-failed")).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByTestId("kpi-failed"));
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(expect.objectContaining({ outcome: "all" })));
  });

  it("filters by agent from the cards and from the agent chips", async () => {
    const spy = vi.spyOn(enterpriseDashboard, "summary").mockResolvedValue(summary(MIXED));
    render(<ScanDashboard onOpen={() => {}} />);
    await screen.findByTestId("scan-list");
    fireEvent.click(screen.getByTestId("kpi-five-page"));
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(expect.objectContaining({ agent: "five_page" })));
    fireEvent.click(screen.getByTestId("agent-enterprise"));
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(expect.objectContaining({ agent: "enterprise" })));
    fireEvent.click(screen.getByTestId("agent-all"));
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(expect.objectContaining({ agent: "all" })));
  });

  it("the status chips carry the counts and filter too", async () => {
    const spy = vi.spyOn(enterpriseDashboard, "summary").mockResolvedValue(summary(MIXED));
    render(<ScanDashboard onOpen={() => {}} />);
    await screen.findByTestId("scan-list");
    expect(screen.getByTestId("chip-all")).toHaveTextContent("4");
    fireEvent.click(screen.getByTestId("chip-pending"));
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(expect.objectContaining({ outcome: "pending" })));
  });

  it("says plainly when there is no scan, and when a filter matches none", async () => {
    const spy = vi.spyOn(enterpriseDashboard, "summary").mockResolvedValue(summary([], { scans_total: 0, scans_completed: 0, scans_failed: 0, scans_pending: 0, scans_five_page: 0, scans_enterprise: 0 }));
    render(<ScanDashboard onOpen={() => {}} />);
    expect(await screen.findByTestId("history-empty")).toHaveTextContent("No scans yet");
    expect(screen.getByTestId("kpi-last")).toHaveTextContent("none yet");
    fireEvent.click(screen.getByTestId("chip-failed"));
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(expect.objectContaining({ outcome: "failed" })));
    expect(await screen.findByText("No scan matches this filter.")).toBeInTheDocument();
  });

  it("shows an error with a retry when the dashboard cannot be loaded", async () => {
    vi.spyOn(enterpriseDashboard, "summary").mockRejectedValueOnce(new Error("down")).mockResolvedValue(summary(MIXED));
    render(<ScanDashboard onOpen={() => {}} />);
    expect(await screen.findByTestId("dashboard-error")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("scan-dashboard")).toBeInTheDocument();
  });

  it("refreshes by itself every 15 seconds while a scan is pending, and not otherwise", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const spy = vi.spyOn(enterpriseDashboard, "summary").mockResolvedValue(summary(MIXED));
    render(<ScanDashboard onOpen={() => {}} />);
    await screen.findByTestId("scan-list");
    const before = spy.mock.calls.length;
    await vi.advanceTimersByTimeAsync(15_100);
    expect(spy.mock.calls.length).toBeGreaterThan(before);
    expect(spy).toHaveBeenLastCalledWith(expect.objectContaining({ refresh: true }));
    cleanup();
    spy.mockResolvedValue(summary([scan()], { scans_pending: 0 }));
    render(<ScanDashboard onOpen={() => {}} />);
    await screen.findByTestId("scan-list");
    const quiet = spy.mock.calls.length;
    await vi.advanceTimersByTimeAsync(31_000);
    expect(spy.mock.calls.length).toBe(quiet);
  });

  it("explains on request what is kept and what is cleared", async () => {
    vi.spyOn(enterpriseDashboard, "summary").mockResolvedValue(summary(MIXED));
    vi.spyOn(enterpriseDashboard, "retention").mockResolvedValue({
      grace_minutes: 15, keeps: "its id, the website, when it started and finished, completed or failed, and why it failed", clears: "everything else a scan produced, including the stored reports", when: "15 minutes after a scan has finished",
    });
    render(<ScanDashboard onOpen={() => {}} />);
    await screen.findByTestId("scan-list");
    fireEvent.click(screen.getByTestId("retention-toggle"));
    const panel = await screen.findByTestId("retention-panel");
    expect(panel).toHaveTextContent("Kept for every scan");
    expect(panel).toHaveTextContent("why it failed");
    expect(panel).toHaveTextContent("15 minutes after a scan has finished");
  });
});
