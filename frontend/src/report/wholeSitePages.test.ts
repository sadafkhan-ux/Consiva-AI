import { describe, expect, it } from "vitest";
import type { EvidencePageItem, WebsiteScanMetadata } from "../api/types";
import { buildScanPages as build } from "./scanPages";

/** The page view, which is null only when there is neither pages nor metadata (never in these tests). */
const buildScanPages = (...a: Parameters<typeof build>) => {
  const v = build(...a);
  if (!v) throw new Error("expected a page view");
  return v;
};

const page = (url: string, via: string | null = "link", http: number | null = 200): EvidencePageItem =>
  ({ url, title: null, http_status: http, discovered_via: via } as unknown as EvidencePageItem);

const pages = (n: number) => [page("https://x.test/", "seed"), ...Array.from({ length: n - 1 }, (_, i) => page(`https://x.test/p${i}`))];
const meta = (extra: Partial<WebsiteScanMetadata> = {}) =>
  ({ pages_scanned: 142, pages_attempted: 142, pages_discovered: 152, page_limit: null, stop_reason: "queue_exhausted", skipped_by_reason: {}, ...extra }) as unknown as WebsiteScanMetadata;

describe("a whole-site scan (no page cap) is described as what it is", () => {
  it("never says the scan is limited, and says all pages were included", () => {
    const sp = buildScanPages(pages(142), meta(), "https://x.test/", null);
    expect(sp.pageLimit).toBeNull();
    const text = sp.review.map((r) => r.text).join(" ");
    expect(text).not.toMatch(/limited to|limit of|page limit was set|fewer than the limit/);
    expect(sp.review.find((r) => r.label === "Included in the compliance assessment")?.text).toMatch(/^All 142 scanned pages were included/);
  });

  it("keeps every page row (the report lists them all when there is no cap)", () => {
    expect(buildScanPages(pages(142), meta(), "https://x.test/", null).rows).toHaveLength(142);
  });

  it("says a skipped file is a file, not a page that failed", () => {
    const sp = buildScanPages(pages(3), meta({ pages_scanned: 3, pages_attempted: 3, skipped_by_reason: { document_download: 8 } }), "https://x.test/", null);
    expect(sp.review.find((r) => r.label === "Found but not scanned")?.text).toContain("8 pages files (PDF or similar), not web pages");
  });
});

describe("a standard (five-page) scan keeps its limit wording", () => {
  it("still names the limit of 5", () => {
    const sp = buildScanPages(pages(5), meta({ pages_scanned: 5, pages_attempted: 5, page_limit: 5 }), "https://x.test/", 5);
    expect(sp.pageLimit).toBe(5);
    expect(sp.review.find((r) => r.label === "Pages attempted")?.text).toContain("limited to 5 pages");
  });
});
