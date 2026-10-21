import { beforeEach, describe, expect, it, vi } from "vitest";

// A recording stand-in for jsPDF: what is drawn, on which page, and in which orientation. The layout code only talks to
// this API, so the PDF's pagination and content can be asserted without rendering a file.
interface RecordedPage { orientation: "portrait" | "landscape"; texts: string[]; rects: number }
const recorded: { pages: RecordedPage[]; saved: string | null } = { pages: [], saved: null };

vi.mock("jspdf", () => {
  class FakeDoc {
    constructor() {
      recorded.pages = [{ orientation: "portrait", texts: [], rects: 0 }];
      recorded.saved = null;
    }
    private get page() { return recorded.pages[recorded.pages.length - 1]; }
    setFont() { return this; }
    setFontSize() { return this; }
    setTextColor() { return this; }
    setDrawColor() { return this; }
    setFillColor() { return this; }
    setLineWidth() { return this; }
    line() { return this; }
    roundedRect() { this.page.rects += 1; return this; }
    rect() { this.page.rects += 1; return this; }
    circle() { return this; }
    getTextWidth(t: string) { return t.length * 4.5; }
    splitTextToSize(text: string, width: number): string[] {
      const per = Math.max(10, Math.floor(width / 4.6));
      const lines: string[] = [];
      for (let i = 0; i < text.length; i += per) lines.push(text.slice(i, i + per));
      return lines.length ? lines : [""];
    }
    text(t: string | string[]) { this.page.texts.push(...(Array.isArray(t) ? t : [t])); return this; }
    addPage(_format: string, orientation: "portrait" | "landscape") {
      recorded.pages.push({ orientation, texts: [], rects: 0 });
      return this;
    }
    save(name: string) { recorded.saved = name; }
  }
  return { jsPDF: FakeDoc };
});

import type { ReportModel } from "./buildReport";
import { buildReportModel } from "./buildReport";
import { downloadReport } from "./downloadReport";
import { deriveDomainReport } from "../components/subdomains";

const MAIN = "https://gignaati.com/";
const meta = (discovered: unknown[] = []) => ({
  consent_mechanism: "cmp", cmp_vendor: "Consiva Consent SDK", accept_interaction: "clicked", reject_interaction: "clicked",
  pages_scanned: 3, trackers_by_consent_state: { pre_consent: 0 }, discovered_domains: discovered,
}) as never;
const finding = (id: string, evidence: string[]) => ({
  id, finding_text: `Trackers fired before consent (${id}).`, risk_level: "high", evidence, status: "pending", priority: "high",
  category: "other", dpdp_reference: [], recommendations: [{ recommendation_text: "Block non-essential scripts." }], requires_human_review: true,
}) as never;
const tracker = (id: string, src: string, category: string | null = null) =>
  ({ id, script_src: src, vendor: null, category, source: "rule", consent_states: ["pre_consent"], page_url: MAIN }) as never;

function model(over: { discovered?: unknown[]; trackers?: unknown[]; findings?: unknown[]; withReport?: boolean } = {}): ReportModel {
  const trackers = (over.trackers ?? []) as never[];
  const findings = (over.findings ?? [finding("f1", ["t1"])]) as never[];
  const m = meta(over.discovered ?? []);
  const report = deriveDomainReport({ scanUrl: MAIN, trackers, cookies: [], findings, meta: m });
  const count = report.subdomains.length + report.external.length;
  return buildReportModel(
    { url: MAIN, evidence_counts: { pages: 3, cookies: 0, trackers: trackers.length, third_party_services: 0, policies: 1, forms: 0 } } as never,
    findings, m, [], [], over.withReport === false ? null : { requests: trackers.length, cookies: 0, domains: count },
    over.withReport === false ? null : report,
  );
}

const SUBS = [
  { host: "masterclass.gignaati.com", kind: "subdomain", links: 4, sample_url: "https://masterclass.gignaati.com/", framed: false },
  { host: "academy.gignaati.com", kind: "subdomain", links: 2, sample_url: "https://academy.gignaati.com/", framed: false },
  { host: "twitter.com", kind: "external", links: 1, sample_url: "https://twitter.com/gignaati", framed: false },
];
const TRACKERS = [
  tracker("t1", "https://masterclass.gignaati.com/api/track"),
  tracker("t2", "https://calendly.com/gignaati/discovery-call?hide_gdpr_banner=1"),
  tracker("t3", "https://www.googletagmanager.com/gtm.js", "analytics"),
];
const pageText = (i: number) => recorded.pages[i].texts.join("");
const allText = () => recorded.pages.map((p) => p.texts.join("")).join("");

beforeEach(() => { recorded.pages = []; recorded.saved = null; });

describe("the PDF with discovered subdomains and websites", () => {
  it("prints them on their own highlighted pages between the summary and the cookie inventory", async () => {
    await downloadReport(model({ discovered: SUBS, trackers: TRACKERS }));

    expect(recorded.pages.map((p) => p.orientation)).toEqual(["portrait", "portrait", "landscape"]);
    expect(pageText(0)).toContain("Consent Compliance Summary");
    expect(pageText(1)).toContain("Discovered Subdomains & Websites");
    expect(pageText(2)).toContain("Cookie inventory");
    expect(recorded.pages[1].rects).toBeGreaterThanOrEqual(2);              // the tinted band and its edge
    expect(recorded.saved).toMatch(/^consiva-consent-summary-gignaati\.com-\d{4}-\d{2}-\d{2}\.pdf$/);
  });

  it("lists every subdomain and separate website, with its URLs, labelled by kind", async () => {
    await downloadReport(model({ discovered: SUBS, trackers: TRACKERS }));

    const t = pageText(1);
    for (const name of ["masterclass.gignaati.com", "academy.gignaati.com", "calendly.com", "twitter.com"]) expect(t).toContain(name);
    expect(t).toContain("https://masterclass.gignaati.com/");                  // found through a link
    expect(t).toContain("https://masterclass.gignaati.com/api/track");        // requested by a scanned page
    expect(t).toContain("https://calendly.com/gignaati/discovery-call?hide_gdpr_banner=1".slice(0, 30));
    expect(t).toContain("https://twitter.com/gignaati");
    expect(t).toContain("SUBDOMAIN");
    expect(t).toContain("SEPARATE WEBSITE");
    expect(t).toContain("REVIEW");
    expect(t).toContain("2 subdomains · 2 separate websites");
    expect(t).not.toContain("googletagmanager.com");                          // a named tracker is the site's own result
  });

  it("shows a finding under the domain it involves, so it reads alongside the main-site findings", async () => {
    await downloadReport(model({ discovered: SUBS, trackers: TRACKERS }));

    const t = pageText(1);
    expect(t).toContain("FINDING · HIGH");
    expect(t).toContain("Trackers fired before consent (f1).");
    expect(pageText(0)).toContain("Trackers fired before consent (f1).");    // and it is still on the summary page
  });

  it("says on the first page that they are counted apart and where to read them", async () => {
    await downloadReport(model({ discovered: SUBS, trackers: TRACKERS }));

    const first = pageText(0);
    expect(first).toContain("counted apart from the site's own results");
    expect(first).toContain("Discovered Subdomains & Websites section that follows this page");
  });

  it("flows over as many pages as it needs and lists all of them", async () => {
    const many = Array.from({ length: 60 }, (_, i) => ({
      host: `www.shop${String(i).padStart(2, "0")}.example${i}.net`, kind: "external", links: 1,
      sample_url: `https://www.shop${String(i).padStart(2, "0")}.example${i}.net/landing`, framed: false,
    }));

    await downloadReport(model({ discovered: many }));

    const discoveredPages = recorded.pages.filter((p) => p.texts.some((t) => t.startsWith("Discovered Subdomains & Websites")));
    expect(discoveredPages.length).toBeGreaterThan(1);
    expect(discoveredPages[1].texts.some((t) => t.includes("(continued)"))).toBe(true);
    expect(allText()).toContain("60 separate websites");
    for (let i = 0; i < 60; i++) expect(allText()).toContain(`https://www.shop${String(i).padStart(2, "0")}.example${i}.net/landing`);
    expect(recorded.pages[recorded.pages.length - 1].orientation).toBe("landscape");   // the cookie inventory still follows
    expect(allText()).toContain("Page 2.");                                            // each page is numbered in its footer
  });

  it("lists the other hosts of a website that is grouped into one row, and counts the rest", async () => {
    const hosts = Array.from({ length: 9 }, (_, i) => ({ host: `p${i}.partner.org`, kind: "external", links: 1, sample_url: `https://p${i}.partner.org/x`, framed: false }));

    await downloadReport(model({ discovered: hosts }));

    const t = pageText(1);
    for (let i = 0; i < 6; i++) expect(t).toContain(`https://p${i}.partner.org/x`);
    expect(t).toContain("and 3 more hosts of this website");
  });

  it("marks a site that is displayed in a frame", async () => {
    await downloadReport(model({ discovered: [{ host: "corp.other.com", kind: "external", links: 1, sample_url: "https://corp.other.com/", framed: true }] }));

    expect(pageText(1)).toContain("SHOWN IN A FRAME");
    expect(pageText(1)).toContain("SHOWN IN A FRAME".toUpperCase());
  });
});

describe("the PDF is unchanged when nothing was discovered", () => {
  it("has the summary page and the cookie inventory only, with no discovered section or line", async () => {
    await downloadReport(model({ discovered: [], trackers: [] }));

    expect(recorded.pages.map((p) => p.orientation)).toEqual(["portrait", "landscape"]);
    expect(allText()).not.toContain("Discovered Subdomains & Websites");
    expect(pageText(0)).not.toContain("counted apart");
    expect(pageText(0)).toContain("Consent Compliance Summary");
    expect(pageText(1)).toContain("Cookie inventory");
  });

  it("is also unchanged when no domain report is passed at all (older callers)", async () => {
    await downloadReport(model({ discovered: SUBS, trackers: TRACKERS, withReport: false }));

    expect(recorded.pages.map((p) => p.orientation)).toEqual(["portrait", "landscape"]);
    expect(allText()).not.toContain("Discovered Subdomains & Websites");
  });
});
