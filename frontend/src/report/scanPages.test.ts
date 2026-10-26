import { beforeEach, describe, expect, it, vi } from "vitest";

// A recording stand-in for jsPDF, as in twoPageReport.test.ts.
const rec: { pages: string[][]; current: number; links: string[] } = { pages: [[]], current: 0, links: [] };
vi.mock("jspdf", () => {
  class FakeDoc {
    constructor() { rec.pages = [[]]; rec.current = 0; rec.links = []; }
    setFont() { return this; }
    setFontSize() { return this; }
    setTextColor() { return this; }
    setDrawColor() { return this; }
    setFillColor() { return this; }
    setLineWidth() { return this; }
    setLineCap() { return this; }
    line() { return this; }
    circle() { return this; }
    roundedRect() { return this; }
    rect() { return this; }
    link(_x: number, _y: number, _w: number, _h: number, o: { url: string }) { rec.links.push(o.url); return this; }
    getTextWidth(t: string) { return t.length * 4.5; }
    addImage() { return this; }
    splitTextToSize(text: string, width: number): string[] {
      const per = Math.max(10, Math.floor(width / 4.6));
      const out: string[] = [];
      for (const part of text.split("\n")) for (let i = 0; i < Math.max(part.length, 1); i += per) out.push(part.slice(i, i + per));
      return out;
    }
    text(t: string | string[]) { rec.pages[rec.current].push(...(Array.isArray(t) ? t : [t])); return this; }
    addPage() { rec.pages.push([]); rec.current = rec.pages.length - 1; return this; }
    getNumberOfPages() { return rec.pages.length; }
    setPage(n: number) { rec.current = n - 1; return this; }
    save() { /* not written */ }
  }
  return { jsPDF: FakeDoc };
});

import type { DpdpScoreResponse, EvidencePageItem, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildScoreView } from "./dpdpScore";
import { buildReportModel } from "./buildReport";
import { downloadReport } from "./downloadReport";
import { buildScanPages, SELECTION_TEXT } from "./scanPages";

const SITE = "https://shop.example/";
const page = (n: number, url: string, via: string | null, status: number | null = 200): EvidencePageItem => ({
  id: `p${n}`, url, title: null, http_status: via && ["failed", "timeout"].includes(via) ? null : status, discovered_via: via,
});
const FIVE: EvidencePageItem[] = [
  page(0, "https://shop.example/privacy", "link"),
  page(1, "https://shop.example/terms", "link"),
  page(2, "https://shop.example/", "seed"),
  page(3, "https://shop.example/contact", "link"),
  page(4, "https://shop.example/about", "sitemap"),
];
const META = (over: Partial<WebsiteScanMetadata> = {}) => ({
  page_limit: 5, pages_attempted: 5, pages_scanned: 5, pages_failed: 0, pages_timeout: 0, pages_discovered: 9,
  pages_not_attempted_budget_exhausted: 4, stop_reason: "page_limit", skipped_by_reason: {}, ...over,
}) as WebsiteScanMetadata;

const reviewOf = (v: NonNullable<ReturnType<typeof buildScanPages>>) => Object.fromEntries(v.review.map((r) => [r.label, r.text]));

describe("the page list is the pages the scan recorded, nothing assumed", () => {
  it("puts the homepage first and lists the other four exactly as recorded", () => {
    const v = buildScanPages(FIVE, META(), SITE)!;
    expect(v.rows).toHaveLength(5);
    expect(v.rows[0]).toMatchObject({ no: 1, pageType: "Homepage", url: "https://shop.example/", scanned: true });
    expect(v.rows.slice(1).map((r) => r.url)).toEqual([
      "https://shop.example/privacy", "https://shop.example/terms", "https://shop.example/contact", "https://shop.example/about"]);
    expect(v.rows[4].pageType).toBe("Scanned Page (found by the sitemap)");
    expect(v.rows[1].pageType).toBe("Scanned Page (found by a link)");
    expect(v.rows.every((r) => r.status === "Scanned (HTTP 200)")).toBe(true);
  });

  it("says all five were included when all five loaded", () => {
    const text = reviewOf(buildScanPages(FIVE, META(), SITE)!);
    expect(text["Pages attempted"]).toContain("5 (scans are limited to 5 pages)");
    expect(text["Pages scanned successfully"]).toBe("5 of 5.");
    expect(text["Pages that failed or could not be scanned"]).toContain("None");
    expect(text["Included in the compliance assessment"]).toContain("All 5 scanned pages were included");
    expect(text["How the pages were selected"]).toBe(SELECTION_TEXT);
    expect(text["Found but not scanned"]).toContain("4 more pages found after the limit was reached");
  });

  it("reports five attempted and four scanned accurately, and does not count the failed page as scanned", () => {
    const pages = [...FIVE.slice(0, 4), page(4, "https://shop.example/about", "timeout")];
    const v = buildScanPages(pages, META({ pages_scanned: 4, pages_timeout: 1 }), SITE)!;
    const failed = v.rows.find((r) => r.url.endsWith("/about"))!;
    expect(failed).toMatchObject({ scanned: false, pageType: "Attempted Page", status: "Not scanned: timed out" });
    expect(v.rows.indexOf(failed)).toBe(4);   // a page that did not load is listed after those that did
    const text = reviewOf(v);
    expect(text["Pages attempted"]).toContain("5");
    expect(text["Pages scanned successfully"]).toBe("4 of 5.");
    expect(text["Pages that failed or could not be scanned"]).toContain("https://shop.example/about (timed out)");
    expect(text["Included in the compliance assessment"]).toContain("4 scanned pages, fewer than the limit of 5");
    expect(text["Included in the compliance assessment"]).toContain("Pages that failed contributed no evidence");
    expect(text["Limitations"]).toContain("was not assessed");
  });

  it("does not call an attempt that ended on an already-scanned page a failure or a scanned page", () => {
    // Seen on a stored scan: 3 attempted, 2 recorded (the third redirected to a page already scanned)
    const v = buildScanPages(FIVE.slice(0, 2), META({ page_limit: 3, pages_attempted: 3, pages_scanned: 2, stop_reason: "page_limit", skipped_by_reason: { redirected_to_already_scanned_page: 1 }, pages_not_attempted_budget_exhausted: 0 }), SITE)!;
    const text = reviewOf(v);
    expect(text["Pages attempted"]).toContain("3");
    expect(text["Pages scanned successfully"]).toBe("2 of 3.");
    expect(text["Pages that failed or could not be scanned"]).toContain("None failed to load.");
    expect(text["Pages that failed or could not be scanned"]).toContain("1 attempted page ended on a page already scanned");
    expect(text["Found but not scanned"]).toContain("1 page redirected to a page already scanned");
    expect(v.rows).toHaveLength(2);
  });

  it("never prints a count above the limit as 'N of 5'; an older, larger scan is said to predate the limit", () => {
    const many = Array.from({ length: 8 }, (_, i) => page(i, i === 0 ? SITE : `https://shop.example/p${i}`, i === 0 ? "seed" : "sitemap"));
    const v = buildScanPages(many, META({ page_limit: 25, pages_attempted: 8, pages_scanned: 8, stop_reason: "queue_exhausted" }), SITE, 5)!;
    expect(v.pageLimit).toBe(5);
    const text = reviewOf(v);
    expect(text["Pages attempted"]).toContain("scans are limited to 5 pages");
    expect(text["Included in the compliance assessment"]).toContain("ran before the page limit was set to 5");
  });

  it("uses the server's limit (5) over the limit the scan recorded", () => {
    expect(buildScanPages(FIVE, META({ page_limit: 25 }), SITE, 5)!.pageLimit).toBe(5);
    expect(buildScanPages(FIVE, META({ page_limit: 25 }), SITE)!.pageLimit).toBe(25);   // only a fallback when the server's was not loaded
  });

  it("lists fewer rows, and says why, when the site had fewer pages than the limit", () => {
    const v = buildScanPages(
      FIVE.slice(0, 3), META({ pages_attempted: 3, pages_scanned: 3, stop_reason: "queue_exhausted", pages_not_attempted_budget_exhausted: 0 }), SITE,
    )!;
    expect(v.rows).toHaveLength(3);
    const text = reviewOf(v);
    expect(text["Included in the compliance assessment"]).toContain("3 scanned pages, fewer than the limit of 5");
    expect(text["Included in the compliance assessment"]).toContain("no more pages of the entered domain were found");
    expect(text["Found but not scanned"]).toBeUndefined();
  });

  it("does not claim a page loaded when the scan did not store the result", () => {
    const old = FIVE.map((p) => ({ id: p.id, url: p.url, title: null }));   // a scan made before the status was returned
    const v = buildScanPages(old as EvidencePageItem[], META(), SITE)!;
    expect(v.rows[0].url).toBe("https://shop.example/");   // the root page still leads
    expect(v.rows.every((r) => !r.scanned && r.status.startsWith("Recorded;"))).toBe(true);
    expect(v.scannedCount).toBe(5);   // the count is the scan's own, not derived from the rows
  });

  it("names the reasons pages were found but not scanned", () => {
    const v = buildScanPages(FIVE, META({ skipped_by_reason: { robots_disallowed: 2, duplicate_url: 7 }, pages_not_attempted_budget_exhausted: 0 }), SITE)!;
    const t = reviewOf(v)["Found but not scanned"];
    expect(t).toContain("2 pages blocked by the site's robots.txt");
    expect(t).not.toContain("same address");
  });

  it("says pages left in the queue at the limit once, not twice, and in the crawler's own words", () => {
    const v = buildScanPages(FIVE, META({ skipped_by_reason: { page_limit: 10, time_limit: 2 }, pages_not_attempted_budget_exhausted: 10 }), SITE)!;
    const t = reviewOf(v)["Found but not scanned"];
    expect(t).toBe("10 pages found after the page limit was reached; 2 pages found but not reached before the time limit.");
  });

  it("explains the reasons older scans recorded, and never prints a raw code for an unknown one", () => {
    const t = reviewOf(buildScanPages(FIVE, META({ skipped_by_reason: { subdomain: 2, something_new: 1 }, pages_not_attempted_budget_exhausted: 0 }), SITE)!)["Found but not scanned"];
    expect(t).toContain("2 pages on a subdomain or another host, outside the entered domain");
    expect(t).toContain("1 page skipped (something new)");
  });

  it("flags a recorded page that is not on the entered domain, and a summary that disagrees with the records", () => {
    const pages = [...FIVE.slice(0, 4), page(4, "https://other.example/x", "link")];
    const limitations = reviewOf(buildScanPages(pages, META({ pages_scanned: 4 }), SITE)!)["Limitations"];
    expect(limitations).toContain("is not on shop.example");
    expect(limitations).toContain("reports 4 scanned pages but 5 stored page records loaded");
  });

  it("is null when neither the page records nor the scan summary were loaded", () => {
    expect(buildScanPages(null, null, SITE)).toBeNull();
  });
});

describe("the PDF page", () => {
  beforeEach(() => { rec.pages = [[]]; rec.current = 0; rec.links = []; });
  const SCAN = { id: "8a1f03bc-aaaa-bbbb-cccc-000000000000", url: SITE, evidence_counts: { pages: 5, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 } } as never;
  const EVIDENCE = { pages: FIVE, forms: [], cookies: [], trackers: [], third_party_services: [], policies: [], consent_signals: [] } as unknown as ScanEvidenceResponse;

  async function pdf(pages: EvidencePageItem[], meta = META(), serverLimit: number | null = null) {
    // The server's limit reaches the report through the score's assessment.
    const sc: DpdpScoreResponse | null = serverLimit === null ? null : JSON.parse(JSON.stringify(fixtures.mixed));
    if (sc) sc.assessment.page_limit = serverLimit as number;
    const score = sc ? buildScoreView(sc) : null;
    await downloadReport(buildReportModel(SCAN, [], meta, [], [], score, { ...EVIDENCE, pages }));
    return rec.pages.map((p) => p.join(""));
  }

  it("shows the section, every URL, the status and the review, with each URL a link", async () => {
    const t = (await pdf(FIVE)).join("");
    expect(t).toContain("WEBSITE PAGES SCANNED");
    expect(t).toContain("SCAN REVIEW");
    for (const p of FIVE) expect(t, p.url).toContain(p.url);
    expect(t).toContain("Scan run 8a1f03bc");
    expect(rec.links.filter((u) => u.startsWith("https://shop.example"))).toHaveLength(5);
    expect(rec.links).toEqual(expect.arrayContaining(FIVE.map((p) => p.url)));
    expect(t.indexOf("https://shop.example/privacy")).toBeGreaterThan(t.indexOf("Homepage"));   // the homepage is row 1
  });

  it("comes after the first summary page, and every page is numbered against the real total", async () => {
    const pages = await pdf(FIVE);
    expect(pages.length).toBeGreaterThanOrEqual(2);
    expect(pages.findIndex((p) => p.includes("WEBSITE PAGES SCANNED"))).toBeGreaterThanOrEqual(1);
    expect(pages[0]).not.toContain("WEBSITE PAGES SCANNED");
    pages.forEach((p, i) => expect(p).toContain(`Page ${i + 1} of ${pages.length}`));
  });

  it("states the one page limit, 5, whatever limit an older scan happened to run with", async () => {
    await pdf(FIVE.slice(0, 3), META({ page_limit: 25, pages_attempted: 3, pages_scanned: 3 }), 5);
    const t = rec.pages.map((p) => p.join("")).join("");
    expect(t).toContain("scans are limited to 5 pages");
    expect(t).not.toMatch(/limit (of )?25|up to 25|limited to 25/);
  });

  it("shows how many pages were scanned out of how many were found", async () => {
    const t = (await pdf(FIVE, META({ pages_discovered: 12 }))).join("");
    expect(t).toContain("5 pages scanned of 12 found");
    expect(t).toMatch(/Pages found:\s*12: every distinct page/);
  });

  it("says only how many were scanned when the scan did not record how many it found", async () => {
    const meta = META();
    delete (meta as { pages_discovered?: number }).pages_discovered;
    expect((await pdf(FIVE, meta)).join("")).toContain("5 pages scanned");
  });

  it("shows a failed attempt as not scanned", async () => {
    const t = (await pdf([...FIVE.slice(0, 4), page(4, "https://shop.example/about", "failed")], META({ pages_scanned: 4, pages_failed: 1 }))).join("");
    expect(t).toContain("Not scanned: failed to load");
    expect(t).toContain("4 of 5.");
  });

  it("says the list could not be loaded, and invents no pages, when nothing was loaded", async () => {
    await downloadReport(buildReportModel(SCAN, [], null, [], [], null, null));
    const t = rec.pages.map((p) => p.join("")).join("");
    expect(t).toContain("WEBSITE PAGES SCANNED");
    expect(t).toContain("The list of pages could not be loaded for this report");
    expect(t).not.toContain("https://shop.example/privacy");
    expect(t).not.toContain("SCAN REVIEW");
  });
});
