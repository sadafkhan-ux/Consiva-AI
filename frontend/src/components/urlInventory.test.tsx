import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UrlInventoryItem, UrlInventoryResponse } from "../api/types";
import { CrawlSettings } from "./CrawlSettings";
import { UrlInventoryPanel } from "./UrlInventoryPanel";

const getUrlInventory = vi.fn();
vi.mock("../api/client", () => ({ api: { getUrlInventory: (...a: unknown[]) => getUrlInventory(...a) } }));

const item = (over: Partial<UrlInventoryItem> & { id: string; original_url: string }): UrlInventoryItem => ({
  domain: "x.test", normalized_url: over.original_url, final_url: null, page_title: null, http_status: 200, content_type: "text/html", discovery_source: "link", discovery_sources: ["link"],
  crawl_depth: 1, scan_status: "COMPLETED", status_reason: null, failure_reason: null, discovered_at: "2026-11-03T12:00:00Z", scan_started_at: "2026-11-03T12:00:01Z",
  scan_completed_at: "2026-11-03T12:00:02Z", last_updated_at: "2026-11-03T12:00:02Z", ...over,
});
const URLS: UrlInventoryItem[] = [
  item({ id: "1", original_url: "https://x.test/", crawl_depth: 0, discovery_source: "seed", discovery_sources: ["seed"], page_title: "Home", final_url: "https://x.test/home" }),
  item({ id: "2", original_url: "https://x.test/gone", scan_status: "FAILED", http_status: 404, failure_reason: "HTTP 404", scan_completed_at: null }),
  item({ id: "3", original_url: "https://x.test/private", scan_status: "EXCLUDED", http_status: null, status_reason: "robots_disallowed", scan_started_at: null, scan_completed_at: null }),
  item({ id: "4", original_url: "https://x.test/file.pdf", scan_status: "PARTIALLY_SCANNED", content_type: "application/pdf", status_reason: "not an HTML page (application/pdf); no page text was extracted" }),
  item({ id: "5", original_url: "https://x.test/later", scan_status: "PENDING", http_status: null, status_reason: "page_limit", scan_started_at: null, scan_completed_at: null, discovery_sources: ["sitemap", "link"] }),
];
const SUMMARY = { total: 5, pending: 1, in_progress: 0, completed: 1, failed: 1, partially_scanned: 1, excluded: 1 };
const respond = (urls = URLS, total = urls.length, summary = SUMMARY): UrlInventoryResponse => ({ summary, urls, total, limit: 50, offset: 0 });

beforeEach(() => { getUrlInventory.mockReset(); getUrlInventory.mockResolvedValue(respond()); });
afterEach(cleanup);

describe("the URL inventory dashboard", () => {
  it("shows nothing without a scan", () => {
    render(<UrlInventoryPanel scanId={null} />);
    expect(screen.queryByTestId("url-inventory")).toBeNull();
    expect(getUrlInventory).not.toHaveBeenCalled();
  });

  it("shows the counts of every status and says how many discovered URLs were actually scanned", async () => {
    render(<UrlInventoryPanel scanId="s1" />);
    await screen.findByTestId("inventory-table");
    expect(screen.getByTestId("tile-all")).toHaveTextContent("5");
    for (const [key, n] of [["PENDING", "1"], ["IN_PROGRESS", "0"], ["COMPLETED", "1"], ["FAILED", "1"], ["PARTIALLY_SCANNED", "1"], ["EXCLUDED", "1"]] as const) {
      expect(screen.getByTestId(`tile-${key}`)).toHaveTextContent(n);
    }
    expect(screen.getByTestId("inventory-note")).toHaveTextContent("2 of 5 discovered URLs were scanned");     // completed + partially scanned
    expect(screen.getByTestId("inventory-note")).toHaveTextContent(/not a scanned page/);
  });

  it("a discovered URL is labelled as not scanned, never as scanned", async () => {
    render(<UrlInventoryPanel scanId="s1" />);
    await screen.findByTestId("inventory-table");
    const rows = screen.getAllByTestId("inventory-row");
    const pending = rows.find((r) => r.getAttribute("data-status") === "PENDING")!;
    expect(pending).toHaveTextContent("Discovered, not scanned");
    expect(pending).not.toHaveTextContent(/^Scanned$/);
    expect(pending).toHaveTextContent("page_limit");
  });

  it("shows each URL's depth, HTTP status, title, sources, reason and timestamps", async () => {
    render(<UrlInventoryPanel scanId="s1" />);
    await screen.findByTestId("inventory-table");
    const [home, gone, priv, pdf, later] = screen.getAllByTestId("inventory-row");
    expect(home).toHaveTextContent("Home");
    expect(home).toHaveTextContent("seed");
    expect(screen.getByTestId("inventory-final")).toHaveTextContent("https://x.test/home");      // a redirect target is shown beside the address that was asked for
    expect(gone).toHaveTextContent("404");
    expect(gone).toHaveTextContent("HTTP 404");                                                // a failed page keeps its failure reason
    expect(priv).toHaveTextContent("robots_disallowed");                                       // an excluded page keeps its exclusion reason
    expect(pdf).toHaveTextContent("Partially scanned");
    expect(pdf).toHaveTextContent(/not an HTML page/);                                         // and what was incomplete
    expect(later).toHaveTextContent("sitemap, link");
    expect(home).toHaveTextContent(/2026/);                                                    // discovery and scan timestamps
  });

  it("the original address is a link, and nothing is invented for a field that was not recorded", async () => {
    render(<UrlInventoryPanel scanId="s1" />);
    await screen.findByTestId("inventory-table");
    const link = screen.getAllByRole("link")[0];
    expect(link).toHaveAttribute("href", "https://x.test/");
    const priv = screen.getAllByTestId("inventory-row")[2];
    expect(priv).toHaveTextContent("–");
  });

  it("filters are sent to the server, and clearing them asks again with none", async () => {
    render(<UrlInventoryPanel scanId="s1" />);
    await screen.findByTestId("inventory-table");
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "FAILED" } });
    fireEvent.change(screen.getByLabelText("HTTP status"), { target: { value: "404" } });
    fireEvent.change(screen.getByLabelText("Discovery source"), { target: { value: "sitemap" } });
    fireEvent.change(screen.getByLabelText("Crawl depth"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("URL contains"), { target: { value: "gone" } });
    fireEvent.change(screen.getByLabelText("Reason contains"), { target: { value: "404" } });
    await waitFor(() => expect(getUrlInventory).toHaveBeenLastCalledWith("s1", { status: "FAILED", http_status: 404, source: "sitemap", depth: 2, q: "gone", failure: "404" }, 50, 0));
    fireEvent.click(screen.getByTestId("inventory-clear"));
    await waitFor(() => expect(getUrlInventory).toHaveBeenLastCalledWith("s1", {}, 50, 0));
  });

  it("clicking a status tile filters by that status", async () => {
    render(<UrlInventoryPanel scanId="s1" />);
    await screen.findByTestId("inventory-table");
    fireEvent.click(screen.getByTestId("tile-EXCLUDED"));
    await waitFor(() => expect(getUrlInventory).toHaveBeenLastCalledWith("s1", { status: "EXCLUDED" }, 50, 0));
    expect(screen.getByTestId("tile-EXCLUDED")).toHaveAttribute("aria-pressed", "true");
  });

  it("pages through a long inventory", async () => {
    getUrlInventory.mockResolvedValue({ ...respond(URLS, 120), limit: 50 });
    render(<UrlInventoryPanel scanId="s1" />);
    await screen.findByTestId("inventory-paging");
    expect(screen.getByTestId("inventory-paging")).toHaveTextContent("1–50 of 120");
    fireEvent.click(screen.getByText("Next"));
    await waitFor(() => expect(getUrlInventory).toHaveBeenLastCalledWith("s1", {}, 50, 50));
    expect(screen.getByText("Previous")).not.toBeDisabled();
  });

  it("says so when a scan has no inventory (an older scan), instead of showing an empty table as if nothing was found", async () => {
    getUrlInventory.mockResolvedValue(respond([], 0, { total: 0, pending: 0, in_progress: 0, completed: 0, failed: 0, partially_scanned: 0, excluded: 0 }));
    render(<UrlInventoryPanel scanId="old" />);
    expect(await screen.findByTestId("inventory-empty")).toHaveTextContent(/No URL inventory was recorded/);
    expect(screen.queryByTestId("inventory-table")).toBeNull();
  });

  it("says when filters match nothing, apart from there being no inventory", async () => {
    getUrlInventory.mockResolvedValueOnce(respond()).mockResolvedValue(respond([], 0));
    render(<UrlInventoryPanel scanId="s1" />);
    await screen.findByTestId("inventory-table");
    fireEvent.change(screen.getByLabelText("URL contains"), { target: { value: "zzz" } });
    expect(await screen.findByTestId("inventory-no-match")).toBeInTheDocument();
    expect(screen.queryByTestId("inventory-empty")).toBeNull();
  });

  it("shows a failure to load rather than an empty inventory", async () => {
    getUrlInventory.mockRejectedValue(new Error("boom"));
    render(<UrlInventoryPanel scanId="s1" />);
    expect(await screen.findByTestId("inventory-error")).toHaveTextContent("boom");
  });
});

describe("the whole-site scan settings", () => {
  it("starts nothing until the values are valid, and sends exactly the numbers typed", () => {
    const onStart = vi.fn();
    render(<CrawlSettings disabled={false} onStart={onStart} />);
    fireEvent.change(screen.getByTestId("crawl-max_depth"), { target: { value: "-2" } });
    expect(screen.getByTestId("crawl-error-max_depth")).toHaveTextContent(/at least 0/);
    expect(screen.getByTestId("crawl-start")).toBeDisabled();
    fireEvent.change(screen.getByTestId("crawl-max_depth"), { target: { value: "3" } });
    fireEvent.change(screen.getByTestId("crawl-max_urls"), { target: { value: "250" } });
    fireEvent.change(screen.getByTestId("crawl-requests_per_second"), { target: { value: "2.5" } });
    expect(screen.getByTestId("crawl-start")).not.toBeDisabled();
    fireEvent.click(screen.getByTestId("crawl-start"));
    expect(onStart).toHaveBeenCalledWith({ max_depth: 3, max_urls: 250, requests_per_second: 2.5 });
  });

  it("an untouched form starts a scan that asks for no particular limit", () => {
    const onStart = vi.fn();
    render(<CrawlSettings disabled={false} onStart={onStart} />);
    fireEvent.click(screen.getByTestId("crawl-start"));
    expect(onStart).toHaveBeenCalledWith({});
  });

  it("is disabled while a scan is running", () => {
    render(<CrawlSettings disabled onStart={() => undefined} />);
    expect(screen.getByTestId("crawl-start")).toBeDisabled();
    expect(screen.getByTestId("crawl-max_depth")).toBeDisabled();
  });

  it("says the server decides, so the form is never mistaken for the control", () => {
    render(<CrawlSettings disabled={false} onStart={() => undefined} />);
    expect(screen.getByTestId("crawl-settings")).toHaveTextContent(/server decides what is allowed/);
  });
});
