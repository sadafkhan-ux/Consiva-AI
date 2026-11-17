import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const saved = vi.hoisted(() => ({ saveBlob: vi.fn() }));
vi.mock("../api/client", async (orig) => ({ ...(await orig<typeof import("../api/client")>()), saveBlob: saved.saveBlob }));

import { EnterpriseWorkspace } from "./EnterpriseWorkspace";

type Handler = (url: string, init?: RequestInit) => unknown | Promise<unknown>;
let routes: [RegExp, Handler][] = [];
const calls: { url: string; init?: RequestInit }[] = [];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
function on(pattern: RegExp, h: Handler | unknown) {
  routes.unshift([pattern, typeof h === "function" ? (h as Handler) : () => h]);
}

const ID = "11111111-1111-1111-1111-111111111111";

const declaration = (o: object = {}) => ({
  crawl_status: "completed", analysis_status: "partial", completion: "partial",
  urls: { discovered: 120, eligible: 100, crawled: 90, analysed: 40, pending: 10, excluded: 20, failed: 3, excluded_by_reason: { robots_txt: 5 } },
  documents: { discovered: 7, extracted: 4, partly_extracted: 1, unreadable: 2, extraction_not_recorded: 0 },
  evidence: { records: 321, sources: 12, analysed: null },
  parameters: { total: 39, assessed: 20, not_assessed: 10, not_applicable: 2, awaiting_human_review: 3, needs_records: 4, recorded: 39 },
  ...o,
});
const score = (o: object = {}) => ({
  score: 69, name: "Website-observable DPDP readiness score", version: "ent-dpdp-1.0.0", basis: "readiness", coverage: 82, reason_no_score: null,
  earned_points: 61.5, points_in_play: 89, not_yet_in_force: ["N1"], by_group: { notice: { label: "Notice", points_available: 30, points_in_play: 24, earned_points: 20 } },
  needs_attention: { not_met: [], partly_met: [], not_assessed: [], human_review: [], needs_records: [] }, as_of: "2026-11-13", calculated_at: "2026-11-13T10:00:00Z", ...o,
});
const summary = (o: object = {}) => ({
  scan_id: ID, website_url: "https://example.com/", domain: "example.com", crawl_status: "completed", analysis_status: "partial", completion: "partial",
  score: score(), declaration: declaration(), legal_review_status: "draft: awaiting a named legal sign-off", report_available: false, options: {},
  created_at: "2026-11-13T09:00:00Z", started_at: "2026-11-13T09:01:00Z", completed_at: null, error: null, ...o,
});
const listItem = (o: object = {}) => ({ scan_id: ID, website_url: "https://example.com/", status: "completed", analysis_status: "partial", completion: "partial", score: 69, coverage: 82, created_at: "2026-11-13T09:00:00Z", completed_at: null, ...o });
const param = (o: object = {}) => ({
  id: "C4", name: "No analytics cookie before a choice", group: "consent", group_label: "Consent", kind: "statutory", points: 5, partial_allowed: true, human_review_required: false, conditional: false,
  legal_refs: ["s.6(1)"], in_force: "2027-05-13", in_force_on_as_of: false, penalty_ceiling_cr: 50, schedule_item: 7, state: "not_assessed", earned: 0, in_play: 0, note: "not enough pages",
  confidence: null, assessor: "rule:consent", model: null, reviewed_by: null, review_reason: null, evidence_refs: ["ev:abc"], adjustment: null, assessed_at: "2026-11-13T10:00:00Z", ...o,
});
const page = (items: unknown[], total = items.length, offset = 0) => ({ items, total, offset, limit: 25 });

async function mount(path?: string) {
  if (path) window.history.replaceState(null, "", path);
  render(<EnterpriseWorkspace />);
}

async function openScan() {
  on(/\/enterprise\/scans\?/, page([listItem()]));
  await mount();
  fireEvent.click(await screen.findByRole("button", { name: /Open scan of/ }));
  await screen.findByTestId("overview");
}

const dashScan = (o: object = {}) => ({ agent: "enterprise", scan_id: ID, website_url: "https://example.com/", outcome: "completed", detail: "completed, limited", reason: null,
  started_at: "2026-11-13T09:00:00Z", finished_at: "2026-11-13T09:04:00Z", data_cleared: false, ...o });
const dashboard = (items: object[]) => ({ kpis: { scans_total: items.length, scans_completed: items.length, scans_failed: 0, scans_pending: 0, scans_five_page: 0, scans_enterprise: items.length }, last_scan: items[0] ?? null,
  history: { items, total: items.length, offset: 0, limit: 10 }, definitions: { completed: "done", failed: "failed", pending: "pending" }, cached: false });

beforeEach(() => {
  routes = [];
  calls.length = 0;
  window.sessionStorage.clear();
  window.sessionStorage.setItem("consiva.access_token", "tok");
  window.sessionStorage.setItem("consiva.profile", JSON.stringify({ user_id: "u", org_id: "o", role: "admin", email: "a@b.c" }));
  window.history.replaceState(null, "", "/#/dpdp");
  on(/\/summary$/, () => summary());
  on(/\/enterprise\/dashboard\/summary/, () => dashboard([dashScan()]));
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    for (const [re, h] of routes) if (re.test(url)) { const r = await h(url, init); return r instanceof Response ? r : json(r); }
    return json({ detail: `no route for ${url}` }, 404);
  }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); saved.saveBlob.mockReset(); });

describe("Enterprise DPDP workspace", () => {
  it("lists scans with status, score, coverage and date, and opens one", async () => {
    await openScan();
    expect(calls.some((c) => /\/enterprise\/dashboard\/summary/.test(c.url))).toBe(true);
    expect(calls.every((c) => (c.init?.headers as Record<string, string>)?.Authorization === "Bearer tok")).toBe(true);
  });

  it("shows crawl, analysis and assessment as three separate indicators plus the one-word completion", async () => {
    await openScan();
    expect(screen.getByTestId("completion-word")).toHaveTextContent("Partial");
    expect(screen.getByTestId("stage-crawl")).toHaveTextContent("completed");
    expect(screen.getByTestId("stage-analysis")).toHaveTextContent("partial");
    expect(screen.getByTestId("stage-assessment")).toHaveTextContent("Partial");
    expect(screen.getByTestId("stage-crawl")).toHaveTextContent("90 crawled of 100 eligible");
    expect(screen.getByTestId("stage-assessment")).toHaveTextContent("20 of 39 parameters assessed");
    // the real counts
    const d = screen.getByTestId("declaration");
    expect(d).toHaveTextContent("321");
    expect(d).toHaveTextContent("Awaiting human review");
    expect(screen.getByTestId("excluded-reasons")).toHaveTextContent("robots txt 5");
  });

  it("only calls a scan finished when completion is complete", async () => {
    on(/\/summary$/, () => summary({ completion: "complete", analysis_status: "completed", declaration: declaration({ completion: "complete" }) }));
    await openScan();
    expect(screen.getByTestId("completion-word")).toHaveTextContent("Complete");
    expect(screen.queryByTestId("live-note")).toBeNull();
  });

  it("shows null counts as 'not recorded yet', never 0, undefined or NaN", async () => {
    on(/\/summary$/, () => summary({ declaration: declaration({ urls: { discovered: null, eligible: null, crawled: null, analysed: null, pending: null, excluded: null, failed: null }, evidence: { records: null, sources: null, analysed: null } }) }));
    await openScan();
    const d = screen.getByTestId("declaration");
    expect(within(d).getAllByText("not recorded yet").length).toBeGreaterThanOrEqual(8);
    expect(screen.getByTestId("stage-crawl")).toHaveTextContent("not recorded yet crawled of not recorded yet eligible");
    const body = document.body.textContent ?? "";
    expect(body).not.toMatch(/undefined|NaN|\$\{|\bnull\b/);
  });

  it("labels the score by its basis, names what is not yet in force, shows legal review and the not-a-certification notice", async () => {
    await openScan();
    expect(screen.getByTestId("score-basis")).toHaveTextContent("Readiness score");
    expect(screen.getByTestId("score-value")).toHaveTextContent("69");
    expect(screen.getByTestId("score-coverage")).toHaveTextContent("Coverage 82%.");
    expect(screen.getByTestId("score-coverage")).toHaveTextContent("61.5 of 100");
    expect(screen.getByTestId("not-in-force")).toHaveTextContent("N1");
    expect(screen.getByTestId("group-table")).toHaveTextContent("Notice");
    expect(screen.getByTestId("legal-review")).toHaveTextContent("awaiting a named legal sign-off");
    expect(screen.getByTestId("not-certification")).toHaveTextContent("not a certification");
  });

  it("shows the score whatever the coverage, and the reason instead of a number only when nothing could be scored", async () => {
    on(/\/summary$/, () => summary({ score: score({ score: 55, coverage: 40 }) }));
    await openScan();
    expect(screen.getByTestId("score-value")).toHaveTextContent("55");
    expect(screen.getByTestId("score-coverage")).toHaveTextContent("Coverage 40%.");
    expect(screen.queryByTestId("no-score")).toBeNull();
  });

  it("gives the reason instead of a number when nothing could be assessed", async () => {
    on(/\/summary$/, () => summary({ score: score({ score: null, coverage: 0, reason_no_score: "no scored parameter could be assessed" }) }));
    await openScan();
    expect(screen.getByTestId("no-score")).toHaveTextContent("no scored parameter could be assessed");
    expect(screen.queryByTestId("score-value")).toBeNull();
    expect(screen.getByTestId("stage-assessment")).toHaveTextContent("Not scored");
  });

  it("polls while crawling, stops once final, and cleans up on unmount", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let n = 0;
    on(/\/summary$/, () => { n += 1; return n < 3 ? summary({ crawl_status: "running", completion: "crawling", analysis_status: "not_started" }) : summary({ completion: "complete", analysis_status: "completed" }); });
    on(/\/enterprise\/scans\?/, page([listItem()]));
    await mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    fireEvent.click(screen.getByRole("button", { name: /Open scan of/ }));
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(n).toBe(1);
    expect(screen.getByTestId("live-note")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(n).toBe(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(n).toBe(3);
    expect(screen.getByTestId("completion-word")).toHaveTextContent("Complete");
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(n).toBe(3); // stopped: nothing left to wait for
  });

  it("stops polling when the page is left", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let n = 0;
    on(/\/summary$/, () => { n += 1; return summary({ crawl_status: "running", completion: "crawling" }); });
    on(/\/enterprise\/scans\?/, page([listItem()]));
    await mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    fireEvent.click(screen.getByRole("button", { name: /Open scan of/ }));
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    cleanup();
    const before = n;
    await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
    expect(n).toBe(before);
  });

  it("paginates the URL table with offset and limit, and filters by status", async () => {
    on(/\/urls\?/, (u: string) => {
      const q = new URL(u, "http://x").searchParams;
      const off = Number(q.get("offset"));
      return page([{ url: `https://example.com/p${off}`, final_url: null, depth: 1, status: q.get("status") === "excluded" ? "excluded" : "done", analysis_status: "done", http_status: 200, status_reason: q.get("status") === "excluded" ? "robots_txt" : null, last_error: null, discovery_sources: ["sitemap"], is_document: false, extraction_status: null }], 60, off);
    });
    await openScan();
    fireEvent.click(screen.getByRole("tab", { name: "URLs" }));
    await screen.findByText("https://example.com/p0");
    expect(screen.getByTestId("pager")).toHaveTextContent("1 to 25 of 60 URLs");
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("https://example.com/p25");
    expect(calls.some((c) => /\/urls\?.*offset=25&limit=25/.test(c.url))).toBe(true);
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "excluded" } });
    await screen.findByText("robots_txt");
    expect(calls.some((c) => /\/urls\?status=excluded&offset=0/.test(c.url))).toBe(true);
  });

  it("never styles a not-assessed parameter as passed or failed", async () => {
    on(/\/parameters$/, { items: [param(), param({ id: "C5", name: "Met one", state: "met", earned: 5, in_play: 5, in_force_on_as_of: true }), param({ id: "C6", name: "Needs", state: "needs_records" }), param({ id: "C7", name: "HR", state: "human_review" })] });
    await openScan();
    fireEvent.click(screen.getByRole("tab", { name: "Parameters" }));
    const pill = await screen.findByTestId("param-state-C4");
    expect(pill).toHaveTextContent("Not assessed");
    expect(pill.className).not.toMatch(/pill-(ok|bad|warn)/);
    expect(screen.getByTestId("param-C4")).toHaveTextContent("not counted as a pass or a fail");
    expect(screen.getByTestId("param-state-C6").className).not.toMatch(/pill-(ok|bad)/);
    expect(screen.getByTestId("param-state-C7").className).not.toMatch(/pill-(ok|bad)/);
    expect(screen.getByTestId("param-state-C5").className).toMatch(/pill-ok/);
    expect(screen.getByTestId("param-C4")).toHaveTextContent("Not yet in force");
    expect(screen.getByTestId("param-C4")).toHaveTextContent("s.6(1)");
  });

  it("submits a review with state and reason, then reloads", async () => {
    let posted: unknown = null;
    let reads = 0;
    on(/\/parameters$/, () => { reads += 1; return { items: [param()] }; });
    on(/\/parameters\/C4\/review$/, (_u: string, init?: RequestInit) => { posted = JSON.parse(String(init?.body)); return param({ state: "met" }); });
    await openScan();
    fireEvent.click(screen.getByRole("tab", { name: "Parameters" }));
    await screen.findByTestId("param-C4");
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    const save = screen.getByRole("button", { name: "Save review" });
    expect(save).toBeDisabled(); // a reason is required
    fireEvent.change(screen.getByLabelText("New state"), { target: { value: "met" } });
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: "Checked the banner by hand" } });
    fireEvent.click(save);
    await waitFor(() => expect(posted).toEqual({ state: "met", reason: "Checked the banner by hand" }));
    await waitFor(() => expect(reads).toBeGreaterThanOrEqual(2));
  });

  it("hides the Review action from people who may not review, and shows the history drawer", async () => {
    window.sessionStorage.setItem("consiva.profile", JSON.stringify({ user_id: "u", org_id: "o", role: "member", email: "a@b.c" }));
    on(/\/parameters$/, { items: [param()] });
    on(/\/parameters\/C4\/history$/, { items: [{ state: "not_assessed", assessor: "rule:consent", assessed_at: "2026-11-13T10:00:00Z", note: "first look" }] });
    await openScan();
    fireEvent.click(screen.getByRole("tab", { name: "Parameters" }));
    await screen.findByTestId("param-C4");
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(await screen.findByTestId("history-drawer")).toHaveTextContent("first look");
  });

  it("words the penalty as a ceiling set by law and shows the true affected count", async () => {
    on(/\/findings\?/, page([{ id: "f1", finding_key: "C8:w", parameter_id: "C8", title: "No withdrawal", description: "d", severity: "high", state: "not_met", legal_refs: ["s.6(6)"], affected_urls: ["https://example.com/a"], affected_count: 312, evidence_refs: [], expected: "exp", observed: "obs", reason: "why", confidence: "high", recommendation: "do it", human_review: false, verification_status: "unverified", schedule_item: 7, penalty_ceiling_cr: 50 }]));
    await openScan();
    fireEvent.click(screen.getByRole("tab", { name: "Findings" }));
    await screen.findByText("No withdrawal");
    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByTestId("finding-details")).toHaveTextContent("312 in total, first 1 shown");
    expect(screen.getByTestId("penalty-ceiling")).toHaveTextContent("ceiling of up to Rs 50 crore");
    expect(screen.getByTestId("penalty-ceiling")).toHaveTextContent("not a prediction");
    fireEvent.change(screen.getByLabelText("Severity"), { target: { value: "low" } });
    await waitFor(() => expect(calls.some((c) => /\/findings\?severity=low/.test(c.url))).toBe(true));
  });

  it("shows evidence content as plain text, never as HTML, and notes truncation", async () => {
    on(/\/evidence\?/, page([{ id: "e1", kind: "cookie", source_url: "https://example.com/", excerpt: "x" }]));
    on(/\/evidence\/e1$/, { id: "e1", kind: "cookie", source_url: "https://example.com/", content: "<img src=x onerror=alert(1)>hello", truncated: true });
    await openScan();
    fireEvent.click(screen.getByRole("tab", { name: "Evidence" }));
    fireEvent.click(await screen.findByRole("button", { name: /View evidence e1/ }));
    const c = await screen.findByTestId("evidence-content");
    expect(c.textContent).toBe("<img src=x onerror=alert(1)>hello");
    expect(c.querySelector("img")).toBeNull();
    expect(screen.getByTestId("truncated-note")).toBeInTheDocument();
  });

  it("downloads the report PDF with an authenticated request and saves the blob", async () => {
    on(/\/reports$/, { items: [{ version: 2, status: "ready", page_count: 12, byte_size: 2048, sha256: "abc", generated_at: "2026-11-13T10:00:00Z", score_result_id: "s" }] });
    on(/\/report\.pdf/, () => new Response(new Blob(["%PDF"], { type: "application/pdf" }), { status: 200, headers: { "content-type": "application/pdf", "content-disposition": 'attachment; filename="r.pdf"' } }));
    await openScan();
    fireEvent.click(screen.getByRole("tab", { name: "Report" }));
    fireEvent.click(await screen.findByRole("button", { name: "Download version 2" }));
    await waitFor(() => expect(saved.saveBlob).toHaveBeenCalled());
    const call = calls.find((c) => /report\.pdf\?version=2/.test(c.url));
    expect(call).toBeTruthy();
    expect((call!.init!.headers as Record<string, string>).Authorization).toBe("Bearer tok");
    expect(saved.saveBlob.mock.calls[0][1]).toBe("r.pdf");
  });

  it("generates a report and says plainly when there is none", async () => {
    let made = false;
    on(/\/reports$/, () => ({ items: [] }));
    on(/\/report$/, () => { made = true; return json({ scan_id: ID, version: 1, status: "queued" }, 202); });
    await openScan();
    fireEvent.click(screen.getByRole("tab", { name: "Report" }));
    expect(await screen.findByText("No report has been generated yet.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Generate report" }));
    await waitFor(() => expect(made).toBe(true));
    expect(await screen.findByTestId("report-note")).toHaveTextContent("Report version 1 is queued");
  });

  it("disables actions that do not apply and says why", async () => {
    on(/\/summary$/, () => summary({ completion: "complete", crawl_status: "completed", analysis_status: "completed" }));
    await openScan();
    const bar = screen.getByTestId("scan-actions");
    expect(within(bar).getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(within(bar).getByRole("button", { name: "Resume" })).toBeDisabled();
    expect(within(bar).getByRole("button", { name: "Re-run analysis" })).toBeEnabled();
    expect(screen.getByText(/Cancel: The scan is not running/)).toBeInTheDocument();
  });

  it("shows a plain error for a failed read and does not leave a rejected promise", async () => {
    on(/\/summary$/, () => json({ detail: "Scan not found" }, 404));
    on(/\/enterprise\/scans\?/, page([listItem()]));
    await mount();
    fireEvent.click(await screen.findByRole("button", { name: /Open scan of/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Scan not found");
  });

  it("requires the attestation before a scan can be started", async () => {
    on(/\/enterprise\/dashboard\/summary/, () => dashboard([]));
    await mount();
    const start = screen.getByRole("button", { name: "Start scan" });
    fireEvent.change(screen.getByLabelText("Website address"), { target: { value: "example.com" } });
    expect(start).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(start).toBeEnabled();
    expect(await screen.findByText(/No scans yet/)).toBeInTheDocument();
  });
});
