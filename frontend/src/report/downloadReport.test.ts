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

import { buildReportModel } from "./buildReport";
import { downloadReport } from "./downloadReport";

const MAIN = "https://gignaati.com/";
const META = {
  consent_mechanism: "cmp", cmp_vendor: "Consiva Consent SDK", accept_interaction: "clicked", reject_interaction: "clicked",
  pages_scanned: 3, trackers_by_consent_state: { pre_consent: 0 },
} as never;
const finding = {
  id: "f1", finding_text: "Trackers fired before consent (f1).", risk_level: "high", evidence: ["t1"], status: "pending", priority: "high",
  category: "other", dpdp_reference: [], recommendations: [{ recommendation_text: "Block non-essential scripts." }], requires_human_review: true,
} as never;
const SCAN = { url: MAIN, evidence_counts: { pages: 3, cookies: 0, trackers: 1, third_party_services: 0, policies: 1, forms: 0 } } as never;

const pageText = (i: number) => recorded.pages[i].texts.join("");
const allText = () => recorded.pages.map((p) => p.texts.join("")).join("");

beforeEach(() => { recorded.pages = []; recorded.saved = null; });

describe("the PDF is about the entered main domain only", () => {
  it("is the summary page followed by the cookie inventory, with no section about other domains", async () => {
    await downloadReport(buildReportModel(SCAN, [finding], META));

    expect(recorded.pages.map((p) => p.orientation)).toEqual(["portrait", "landscape"]);
    expect(pageText(0)).toContain("Consent Compliance Summary");
    expect(pageText(0)).toContain("Trackers fired before consent (f1).");
    expect(pageText(1)).toContain("Cookie inventory");
    expect(recorded.saved).toMatch(/^consiva-consent-summary-gignaati\.com-\d{4}-\d{2}-\d{2}\.pdf$/);
  });

  it("never mentions discovered subdomains or websites", async () => {
    await downloadReport(buildReportModel(SCAN, [finding], META));

    const text = allText().toLowerCase();
    expect(text).not.toContain("discovered subdomains");
    expect(text).not.toContain("separate website");
    expect(text).not.toContain("counted apart");
  });
});
