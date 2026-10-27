import type { EvidencePageItem, WebsiteScanMetadata } from "../api/types";

/**
 * The pages one scan actually visited, for the "Website Pages Scanned" section of the report. Every row is a stored page record of
 * this scan (the same scan run as the score), and every number in the review is the crawler's own count for it. Nothing is assumed:
 * a page the scan tried and could not load is listed as that, never as scanned, and when fewer pages than the limit were found the
 * table has fewer rows.
 *
 * How the pages are chosen (backend scanner/crawler.py, scanner/url_normalize.py): the entered address is scanned first. The other
 * pages are links found on pages already scanned, plus the sitemap when the site has one, on the entered domain only, de-duplicated,
 * and tried in this order: privacy and cookie pages, then terms pages, then contact and legal-notice pages, then sign-up / login / checkout pages,
 * then about / pricing / product pages, then everything else, with blog and news pages last. Within one rank the shallower address comes first, then the alphabetical order. No address is guessed or requested without a link or sitemap entry pointing to it.
 */

export interface ScanPageRow {
  no: number;
  pageType: string;
  url: string;
  status: string;
  /** True only when the page loaded, so the page's evidence is in the assessment. */
  scanned: boolean;
}

export interface ScanPagesView {
  rows: ScanPageRow[];
  pageLimit: number | null;
  host: string | null;
  attempted: number;
  scannedCount: number;
  /** "5 of 12 pages scanned": the pages scanned out of every page of the domain the scan found. */
  coverageLine: string;
  /** Every distinct page of the entered domain the scan saw a link or sitemap entry for, or null when the scan did not record it. */
  discovered: number | null;
  /** Pages that were tried and did not load, with the reason; and the pages found but not tried, by reason, in plain words. */
  failed: { url: string; reason: string }[];
  skipped: { reason: string; count: number }[];
  /** The crawl's own account of why it stopped, in plain words, or null. */
  stopReason: string | null;
  /** "Label: sentence" lines for the Scan Review box, in order. */
  review: { label: string; text: string }[];
}

export const SELECTION_TEXT =
  "The entered address is scanned first. The other pages are links found on pages already scanned, plus the sitemap when the site has one, " +
  "on the entered domain only, and are tried in this order: privacy and cookie pages, then terms pages, then contact and legal-notice pages, " +
  "then sign-up, login and checkout pages, then about, pricing and product pages, then everything else, with blog and news pages last. Within one " +
  "group the shallower address comes first, then the alphabetical order, so the same site gives the same pages every time. No address is guessed: a page is only tried when a link or the sitemap points to it.";

const LOADED_VIA = new Set(["seed", "link", "sitemap"]);

const STOP_REASON: Record<string, string> = {
  queue_exhausted: "The scan stopped because no more pages of the entered domain were found to scan.",
  page_limit: "The scan stopped because it reached the page limit.",
  deadline: "The scan stopped at its time limit, so pages that were still queued were not scanned.",
  slow_site: "The scan stopped early because the pages it tried kept timing out, so the site was too slow to scan more of it.",
  attempt_limit: "The scan stopped because too many of the pages it tried could not be scanned (they failed, timed out, or led away from the entered domain).",
  cancelled: "The scan was cancelled before every queued page was scanned.",
};

// The crawler's own reasons (backend scanner/crawler.py, `_skip`). The last four are pages that were queued when the scan stopped.
const SKIP_REASON: Record<string, string> = {
  robots_disallowed: "blocked by the site's robots.txt",
  slow_site: "not tried: the site's pages kept timing out",
  duplicate_url: "the same address as a page already queued",
  redirected_to_already_scanned_page: "redirected to a page already scanned",
  page_limit: "found after the page limit was reached",
  time_limit: "found but not reached before the time limit",
  scan_cancelled: "not reached because the scan was cancelled",
  not_reached: "found but not reached",
  // Written by earlier versions of the crawler, which recorded links to other subdomains.
  subdomain: "on a subdomain or another host, outside the entered domain",
  subdomain_not_scanned: "on a subdomain or another host, outside the entered domain",
};

const hostOf = (url: string): string | null => {
  try {
    return new URL(url).hostname.toLowerCase().replace(/^www\./, "");
  } catch {
    return null;
  }
};

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function statusOf(p: EvidencePageItem): { text: string; scanned: boolean } {
  const via = p.discovered_via ?? null;
  if (via !== null && LOADED_VIA.has(via)) {
    return { text: p.http_status ? `Scanned (HTTP ${p.http_status})` : "Scanned", scanned: true };
  }
  if (via === "timeout") return { text: "Not scanned: timed out", scanned: false };
  if (via === "failed") return { text: "Not scanned: failed to load", scanned: false };
  if (via === "robots_disallowed") return { text: "Not scanned: blocked by robots.txt", scanned: false };
  // A scan stored before the load result was kept: the page was recorded, but whether it loaded was not.
  return { text: "Recorded; load result not stored for this scan", scanned: false };
}

/** "5 pages scanned of 12 found", or "5 pages scanned" when the scan did not record how many pages it found. Found is not the size of the site: it is the pages the crawler came across. */
export function pagesCoverageLine(scanned: number, found: number | null | undefined): string {
  if (found === null || found === undefined || found < scanned) return `${scanned} ${scanned === 1 ? "page" : "pages"} scanned`;
  return `${scanned} ${scanned === 1 ? "page" : "pages"} scanned of ${found} found`;
}

/** The value of a "Pages scanned" tile: "3 (of 40 found)", or just "3" when the number found was not recorded. */
export function pagesTileValue(scanned: number, found: number | null | undefined): string {
  return found === null || found === undefined || found < scanned ? String(scanned) : `${scanned} (of ${found} found)`;
}

export function buildScanPages(
  pages: EvidencePageItem[] | null | undefined, meta: WebsiteScanMetadata | null, scanUrl: string, fallbackLimit: number | null = null,
): ScanPagesView | null {
  if (!pages && !meta) return null;
  const host = hostOf(scanUrl);
  const records = (pages ?? []).map((p) => ({ p, ...statusOf(p) }));

  // The homepage first: the entered address, which the scan marks "seed". For a scan that did not store that, the page at the root
  // of the entered domain, else the first recorded page. The rest keep the order the scan recorded them, pages that loaded before
  // pages that did not.
  const isRoot = (url: string) => {
    try {
      const u = new URL(url);
      return (u.pathname === "/" || u.pathname === "") && !u.search && hostOf(url) === host;
    } catch {
      return false;
    }
  };
  const seedAt = records.findIndex((r) => r.p.discovered_via === "seed");
  const rootAt = seedAt >= 0 ? seedAt : records.findIndex((r) => (r.scanned || r.p.discovered_via == null) && isRoot(r.p.url));
  const home = rootAt >= 0 ? records[rootAt] : null;
  const rest = records.filter((r) => r !== home);
  const ordered = [...(home ? [home] : []), ...rest.filter((r) => r.scanned), ...rest.filter((r) => !r.scanned)];

  const rows: ScanPageRow[] = ordered.map((r, i) => {
    const via = r.p.discovered_via;
    const found = via === "link" || via === "sitemap" ? ` (found by ${via === "link" ? "a link" : "the sitemap"})` : "";
    return {
      no: i + 1,
      pageType: r === home ? "Homepage" : r.scanned ? `Scanned Page${found}` : "Attempted Page",
      url: r.p.url,
      status: r.text,
      scanned: r.scanned,
    };
  });

  // One limit for every scan: the server's. The scan's own recorded limit is only a fallback when the server's was not loaded.
  const limit = fallbackLimit ?? meta?.page_limit ?? null;
  const loaded = rows.filter((r) => r.scanned).length;
  const attemptedRows = ordered.filter((r) => (r.p.discovered_via ?? "") !== "robots_disallowed").length;
  const attempted = meta?.pages_attempted ?? attemptedRows;
  const scannedCount = meta?.pages_scanned ?? loaded;
  const failedRows = rows.filter((r) => !r.scanned && r.status.startsWith("Not scanned"));

  const found = meta?.pages_discovered ?? null;
  const review: { label: string; text: string }[] = [];
  if (found !== null) {
    review.push({ label: "Pages found", text: `${Math.max(found, scannedCount)}: every distinct page of the entered domain the scan saw a link or sitemap entry for.` });
  }
  review.push({
    label: "Pages attempted",
    text: `${attempted}${limit ? ` (scans are limited to ${limit} pages)` : ""}.`,
  });
  review.push({ label: "Pages scanned successfully", text: `${scannedCount} of ${attempted}.` });
  // An attempt that loaded but ended on a page already scanned, or on another host, leaves no page record of its own (the crawler
  // counts it as attempted, not as scanned). It is neither a failure nor a scanned page, so it is said separately.
  const unaccounted = Math.max(0, attempted - scannedCount - failedRows.length);
  let failedText = failedRows.length === 0
    ? "None failed to load."
    : failedRows.map((r) => `${r.url} (${r.status.replace("Not scanned: ", "")})`).join("; ") + ".";
  if (unaccounted > 0) {
    failedText += ` ${plural(unaccounted, "attempted page", "attempted pages")} ended on a page already scanned or outside the entered domain, so ` +
      `${unaccounted === 1 ? "it was" : "they were"} not counted as scanned.`;
  }
  review.push({ label: "Pages that failed or could not be scanned", text: failedText });

  const skipped = meta?.skipped_by_reason ?? {};
  const notScanned: string[] = Object.entries(skipped)
    .filter(([reason, n]) => n > 0 && reason !== "duplicate_url")
    .map(([reason, n]) => `${plural(n, "page", "pages")} ${SKIP_REASON[reason] ?? `skipped (${reason.replace(/_/g, " ")})`}`);
  // The crawler counts the pages still queued at the limit in `skipped_by_reason.page_limit` and, separately, in the budget figure. Say it once.
  const unattempted = meta?.pages_not_attempted_budget_exhausted ?? 0;
  if (unattempted > 0 && !(skipped.page_limit > 0)) notScanned.push(`${plural(unattempted, "more page", "more pages")} found after the limit was reached`);
  if (notScanned.length) review.push({ label: "Found but not scanned", text: `${notScanned.join("; ")}.` });

  review.push({ label: "How the pages were selected", text: SELECTION_TEXT });

  const allFive = limit !== null && loaded === limit;
  // A scan stored before the 5-page limit was set can hold more pages than the limit; say so instead of "21 of 5".
  const overLimit = limit !== null && loaded > limit;
  review.push({
    label: "Included in the compliance assessment",
    text: loaded === 0
      ? "No page loaded, so no page contributed evidence."
      : overLimit
        ? `All ${loaded} scanned pages were included. This scan ran before the page limit was set to ${limit}; scans are now limited to ${limit} pages.`
        : allFive
        ? `All ${loaded} scanned pages were included. Evidence from each of them feeds the checks and the score.`
        : `${plural(loaded, "scanned page", "scanned pages")}${limit ? `, fewer than the limit of ${limit}` : ""}, ` +
          `${loaded === 1 ? "was" : "were"} included. ${failedRows.length ? "Pages that failed contributed no evidence. " : ""}` +
          `${meta?.stop_reason ? STOP_REASON[meta.stop_reason] ?? "" : ""}`.trim(),
  });

  const limitations: string[] = [];
  if (meta?.stop_reason && STOP_REASON[meta.stop_reason] && allFive) limitations.push(STOP_REASON[meta.stop_reason]);
  limitations.push("Only the entered domain was scanned. Other subdomains and other websites were not.");
  limitations.push("Before consent was observed on all the scanned pages. After Accept and After Reject were observed on the homepage only.");
  if (failedRows.length) limitations.push("A page that failed to load or timed out was not assessed, so findings say nothing about it.");
  if (meta?.pages_blocked_robots) limitations.push("Pages the site's robots.txt disallows were not scanned.");
  if (meta && meta.pages_scanned !== undefined && meta.pages_scanned !== loaded && pages) {
    limitations.push(`The scan summary reports ${meta.pages_scanned} scanned pages but ${loaded} stored page records loaded. Treat the list as the record.`);
  }
  const off = rows.filter((r) => hostOf(r.url) !== host);
  if (host && off.length) limitations.push(`${plural(off.length, "recorded page is", "recorded pages are")} not on ${host}.`);
  review.push({ label: "Limitations", text: limitations.join(" ") });

  const skippedList = Object.entries(skipped).filter(([reason, n]) => n > 0 && reason !== "duplicate_url").map(([reason, n]) => ({ reason: SKIP_REASON[reason] ?? `skipped (${reason.replace(/_/g, " ")})`, count: n }));
  if (unattempted > 0 && !(skipped.page_limit > 0)) skippedList.push({ reason: "found after the limit was reached", count: unattempted });
  return {
    rows, pageLimit: limit, host, attempted, scannedCount, coverageLine: pagesCoverageLine(scannedCount, found), discovered: found === null ? null : Math.max(found, scannedCount),
    failed: failedRows.map((r) => ({ url: r.url, reason: r.status.replace("Not scanned: ", "") })), skipped: skippedList,
    stopReason: meta?.stop_reason ? STOP_REASON[meta.stop_reason] ?? null : null, review,
  };
}
