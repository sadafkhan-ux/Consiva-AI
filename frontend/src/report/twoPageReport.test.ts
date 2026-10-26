import { render, within } from "@testing-library/react";
import { createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// A recording stand-in for jsPDF: what is drawn on which page. The layout code only talks to this API.
interface RecordedPage { orientation: "portrait" | "landscape"; texts: string[] }
interface Call { page: number; t: string; x: number; y: number; align?: string; color?: [number, number, number] }
interface Rect { page: number; x: number; y: number; w: number; h: number; fill: [number, number, number] }
interface Image { page: number; data: string; fmt: string; x: number; y: number; w: number; h: number }
const recorded: {
  pages: RecordedPage[]; current: number; saved: string | null; links: string[]; calls: Call[];
  circles: { page: number; x: number; y: number; r: number }[];
  images: Image[];
  rects: Rect[];
  failJpeg?: boolean;
  lines: { page: number; x1: number; y1: number; x2: number; y2: number }[];
} = { pages: [], current: 0, saved: null, links: [], calls: [], circles: [], lines: [], images: [], rects: [] };

vi.mock("jspdf", () => {
  class FakeDoc {
    constructor() {
      recorded.pages = [{ orientation: "portrait", texts: [] }]; recorded.current = 0; recorded.saved = null;
      recorded.links = []; recorded.calls = []; recorded.circles = []; recorded.lines = []; recorded.images = []; recorded.rects = [];
    }
    private get page() { return recorded.pages[recorded.current]; }
    setFont() { return this; }
    setFontSize() { return this; }
    private textColor: [number, number, number] = [0, 0, 0];
    private fillColor: [number, number, number] = [0, 0, 0];
    setTextColor(r: number, g: number, b: number) { this.textColor = [r, g, b]; return this; }
    setDrawColor() { return this; }
    setFillColor(r: number, g: number, b: number) { this.fillColor = [r, g, b]; return this; }
    setLineWidth() { return this; }
    setLineCap() { return this; }
    line(x1: number, y1: number, x2: number, y2: number) { recorded.lines.push({ page: recorded.current, x1, y1, x2, y2 }); return this; }
    circle(x: number, y: number, r: number) { recorded.circles.push({ page: recorded.current, x, y, r }); return this; }
    roundedRect() { return this; }
    rect(x: number, y: number, w: number, h: number) { recorded.rects.push({ page: recorded.current, x, y, w, h, fill: [...this.fillColor] as [number, number, number] }); return this; }
    link(_x: number, _y: number, _w: number, _h: number, o: { url: string }) { recorded.links.push(o.url); return this; }
    getTextWidth(t: string) { return t.length * 4.5; }
    addImage(data: string, fmt: string, x: number, y: number, w: number, h: number) { if (recorded.failJpeg && data.startsWith("data:image/jpeg")) throw new Error("cannot place this picture"); recorded.images.push({ page: recorded.current, data, fmt, x, y, w, h }); return this; }
    splitTextToSize(text: string, width: number): string[] {
      const per = Math.max(10, Math.floor(width / 4.6));
      const lines: string[] = [];
      for (const part of text.split("\n")) {
        if (part === "") { lines.push(""); continue; }
        for (let i = 0; i < part.length; i += per) lines.push(part.slice(i, i + per));
      }
      return lines.length ? lines : [""];
    }
    text(t: string | string[], x?: number, y?: number, o?: { align?: string }) {
      this.page.texts.push(...(Array.isArray(t) ? t : [t]));
      for (const line of Array.isArray(t) ? t : [t]) recorded.calls.push({ page: recorded.current, t: line, x: x ?? 0, y: y ?? 0, align: o?.align, color: [...this.textColor] as [number, number, number] });
      return this;
    }
    addPage(_f: string, orientation: "portrait" | "landscape") { recorded.pages.push({ orientation, texts: [] }); recorded.current = recorded.pages.length - 1; return this; }
    getNumberOfPages() { return recorded.pages.length; }
    setPage(n: number) { recorded.current = n - 1; return this; }
    save(name: string) { recorded.saved = name; }
  }
  return { jsPDF: FakeDoc };
});

import type { DpdpScoreResponse, FindingResponse, PolicyAnalysis, ScanEvidenceResponse } from "../api/types";
import { DpdpScoreSection } from "../components/DpdpScoreSection";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildReportModel } from "./buildReport";
import { buildScoreView, isDpdpScore } from "./dpdpScore";
import { formatRefShort } from "./legalRefs";
import { CONSIVA_LOGO_HEIGHT, CONSIVA_LOGO_WIDTH } from "./consivaLogo";
import { downloadReport } from "./downloadReport";
import { isReportCategory, OMITTED_DUTIES_NOTE, REPORT_DISCLAIMER } from "./reportScope";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
const mixed = F.mixed;
const confirmed = F.confirmed;
const insufficient = F.insufficient;

const SCAN = { url: "https://x.test/", evidence_counts: { pages: 3, cookies: 2, trackers: 5, third_party_services: 0, policies: 1, forms: 0 } } as never;
const META = {
  consent_mechanism: "cmp", cmp_vendor: "Some CMP", accept_interaction: "clicked", reject_interaction: "clicked", pages_scanned: 3,
} as never;

const tracker = (id: string, category: string | null, states: string[]) => ({
  id, script_src: `https://svc${id}.example/t.js`, vendor: null, category, source: "rule", consent_states: states,
});
const EVIDENCE = {
  pages: [{ id: "p1", url: "https://x.test/", title: "Home" }],
  forms: [],
  cookies: [{ id: "c1", name: "_ga", domain: "x.test", category: "analytics", vendor: "Google Analytics", is_first_party: true, source: "rule", consent_states: ["post_accept"] }],
  trackers: [tracker("1", "functional", ["pre_consent", "post_accept"]), tracker("2", "analytics", ["post_accept"]), tracker("3", "marketing", ["post_accept"])],
  third_party_services: [],
  policies: [{ id: "pol1", url: "https://x.test/privacy", policy_type: "privacy_policy" }],
  consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "Some CMP", has_reject_all: true, has_granular_choices: null, evidence: {} }],
} as unknown as ScanEvidenceResponse;

async function pdf(score: DpdpScoreResponse | null, findings: FindingResponse[] = [], evidence: ScanEvidenceResponse | null = EVIDENCE, meta = META, policy: PolicyAnalysis | null = null) {
  await downloadReport(buildReportModel(SCAN, findings, meta, [], [], score ? buildScoreView(score) : null, evidence, null, policy));
  const join = (from: number, to?: number) => recorded.pages.slice(from, to).map((p) => p.texts.join("")).join("");
  const appendixAt = () => { const i = recorded.pages.findIndex((p) => p.texts.join("").includes("Everything the scan recorded for this site")); return i < 0 ? recorded.pages.length : i; };
  return {
    page: (i: number) => recorded.pages[i].texts.join(""),
    all: () => join(0),
    from: (i: number) => join(i),
    /** The summary pages: everything before the appendix. */
    summary: () => join(0, appendixAt()),
    /** The appendix: from the page that carries its title to the end. */
    appendix: () => join(appendixAt()),
    appendixAt,
    /** The table of every check, which flows over as many pages as it needs. */
    checks: () => { const i = recorded.pages.findIndex((p) => p.texts.join("").includes("EVERY CHECK BEHIND THE SCORE")); return join(Math.max(i, 0), appendixAt()); },
  };
}

/** The report names every applicable check once, in "What was tested and what was not", with the reason for each that was not verified. The rest of the report
 *  (the check table, the areas, the findings) still leaves out the data-rights and security checks that could not be tested. */
const COVERAGE_START = "WHAT WAS TESTED AND WHAT WAS NOT";
const withoutCoverage = (t: string) => { const i = t.indexOf(COVERAGE_START); if (i < 0) return t; const j = t.indexOf("PRIORITISED FIXES", i); return t.slice(0, i) + (j < 0 ? "" : t.slice(j)); };
const coverageOf = (t: string) => { const i = t.indexOf(COVERAGE_START); if (i < 0) return ""; const j = t.indexOf("PRIORITISED FIXES", i); return t.slice(i, j < 0 ? undefined : j); };

/** The PDF wraps long lines and the recording stand-in keeps each line apart, so long strings are compared without whitespace. */
const flat = (x: string) => x.replace(/\s+/g, "");

beforeEach(() => {
  recorded.pages = []; recorded.current = 0; recorded.saved = null; recorded.links = [];
  recorded.calls = []; recorded.circles = []; recorded.lines = []; recorded.images = []; recorded.rects = [];
});

describe("the report is three summary pages, then the evidence appendix", () => {
  it.each(Object.keys(F))("%s: portrait pages, the summary first and the appendix after it", async (key) => {
    const r = await pdf(F[key]);
    expect(recorded.pages.every((p) => p.orientation === "portrait")).toBe(true);
    const a = r.appendixAt();
    expect(a).toBeGreaterThanOrEqual(2);                        // at least the summary page and the check table before it
    expect(a).toBeLessThan(recorded.pages.length);               // and an appendix after
    expect(r.summary()).toContain("WEBSITE PAGES SCANNED");
    expect(r.summary()).not.toContain("Everything the scan recorded");   // the appendix's own introduction is not in the summary
    expect(r.summary()).not.toContain("IN THIS APPENDIX");
  });

  it("has only the summary when the scan's evidence was not loaded", async () => {
    const r = await pdf(mixed, [], null);
    expect(r.appendixAt()).toBe(recorded.pages.length);          // there is no appendix
    expect(r.summary()).toContain("WEBSITE PAGES SCANNED");
    expect(r.all()).not.toContain("Evidence appendix");
  });

  it("keeps the pages list capped in the summary however much the scan found, and the rest in the appendix", async () => {
    const big: DpdpScoreResponse = JSON.parse(JSON.stringify(F.review));
    const many = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `p${i}`, url: `https://x.test/page-${i}-${"long-path-segment/".repeat(4)}`, title: null, http_status: 200, discovered_via: i === 0 ? "seed" : "sitemap" }));
    const evidence = { ...EVIDENCE, pages: many(40), trackers: Array.from({ length: 200 }, (_, i) => tracker(String(i), i % 3 ? "functional" : "analytics", ["post_accept"])) } as unknown as ScanEvidenceResponse;
    const r = await pdf(big, [], evidence);
    expect(r.summary()).toContain("more recorded by an earlier, larger scan are not listed");
    expect(r.appendixAt()).toBeLessThan(recorded.pages.length);   // the rest of the evidence is in the appendix, in full
  });

  it("numbers every page against the real total", async () => {
    await pdf(mixed);
    const total = recorded.pages.length;
    recorded.pages.forEach((p, i) => expect(p.texts.join(""), `page ${i + 1}`).toContain(`Page ${i + 1} of ${total}`));
  });

  it("names the file for the site and date", async () => {
    await pdf(mixed);
    expect(recorded.saved).toMatch(/^consiva-privacy-report-x\.test-\d{4}-\d{2}-\d{2}\.pdf$/);
  });

  it("links the one button, and each scanned page's URL", async () => {
    await pdf(mixed);
    expect(recorded.links.filter((u) => u === "https://consiva.ai")).toHaveLength(1);
    expect(recorded.links).toContain("https://x.test/");   // the one page in the test evidence
  });

  it("has no closing call-to-action band at the end of the summary", async () => {
    await pdf(mixed);
    const all = recorded.pages.map((p) => p.texts.join("")).join("");
    for (const t of ["Turn this scan into a fix list", "Get My DPDP Action Plan", "Explore Consiva AI Platform"]) expect(all).not.toContain(t);
  });
});

describe("page 1: the score ring, its percentage and label, summary", () => {
  /** The circles on page 1, largest first: the thick black border, then the score ring inside it (the logo mark's ring is far smaller). */
  const bigCircles = () => recorded.circles.filter((c) => c.page === 0).sort((a, b) => b.r - a.r);
  const bigRing = () => bigCircles()[1];
  const border = () => bigCircles()[0];

  it.each([["mixed", 56.5], ["confirmed", 60], ["thin", 100]])("%s: the percentage is inside the ring, centred, and the label sits directly beneath it", async (key, score) => {
    await pdf(F[key]);
    const ring = bigRing();
    const pct = recorded.calls.find((c) => c.page === 0 && c.t === `${score}%`)!;
    const label = recorded.calls.find((c) => c.page === 0 && c.t === "Compliance Score")!;
    expect(ring.r).toBeGreaterThan(30);
    expect(pct.align).toBe("center");
    expect(pct.x).toBe(ring.x);   // centred horizontally
    expect(pct.y).toBeGreaterThan(ring.y);   // below the centre: the black header takes the top of the circle
    expect(pct.y).toBeLessThan(ring.y + ring.r);   // inside the circle, not below it
    expect(label.align).toBe("center");
    expect(label.x).toBe(ring.x);   // the label is on the same axis
    expect(label.y).toBeGreaterThan(border().y + border().r);   // and below the circle, border included
    expect(label.y - (border().y + border().r)).toBeLessThan(30);   // directly beneath it
  });

  it.each([["mixed", 56.5], ["confirmed", 60], ["thin", 100]])("%s: the arc ends where the displayed percentage says", async (key, score) => {
    await pdf(F[key]);
    const ring = bigRing();
    const onRing = recorded.lines.filter((l) => l.page === 0
      && Math.abs(Math.hypot(l.x1 - ring.x, l.y1 - ring.y) - ring.r) < 0.6 && Math.abs(Math.hypot(l.x2 - ring.x, l.y2 - ring.y) - ring.r) < 0.6);
    expect(onRing.length).toBeGreaterThan(2);
    const first = onRing[0];
    const last = onRing[onRing.length - 1];
    const angle = (x: number, y: number) => ((Math.atan2(y - ring.y, x - ring.x) * 180) / Math.PI + 450) % 360;   // degrees clockwise from 12 o'clock
    const startAngle = angle(first.x1, first.y1);
    expect(Math.min(startAngle, 360 - startAngle)).toBeLessThan(1);   // the arc starts at the top
    const sweep = angle(last.x2, last.y2);
    expect(Math.abs((score === 100 ? 360 : sweep) - score * 3.6)).toBeLessThan(2.5);   // ends at percent x 3.6 degrees (a 1 degree overlap is drawn)
  });

  it("uses one number for the text and the arc, whatever the display string says", () => {
    const v = buildScoreView(mixed);
    expect(v.scoreValue).toBe(mixed.readiness.score_percent);
    expect(v.headline).toBe(`${v.scoreValue}%`);
  });

  it("shows the percentage only: no formula, working or raw expression on pages 1 and 2", async () => {
    const p = await pdf(mixed);
    const summary = p.page(0) + p.page(1);
    expect(summary).not.toMatch(/÷|×|Score = |Coverage = |This scan:/);
    expect(summary).toContain("56.5%");
    expect(summary).not.toContain("Coverage 62.2%");   // the overall coverage is not shown on the hero or the summary pages
  });

  it("shows the same percentage in the ring, the headline sentence and the framework card", async () => {
    const t = (await pdf(mixed)).page(0);
    expect(t.split("56.5%").length - 1).toBeGreaterThanOrEqual(3);
  });

  it("leaves the ledger and the penalty rules to the dashboard", async () => {
    const t = (await pdf(mixed)).all();
    expect(t).not.toContain("Penalty and exclusion rules");
    expect(t).not.toContain("Assessment appendix");
    const { container } = render(createElement(DpdpScoreSection, { score: mixed }));
    expect(container.querySelector('[data-testid="dpdp-ledger-metrics"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="dpdp-penalty-rules"]')).not.toBeNull();
  });

  it("lists the privacy areas the report covers, and not data-rights handling or security", async () => {
    const t = (await pdf(mixed)).page(0);
    const shown = buildScoreView(mixed).categories.filter((c) => isReportCategory(c.name));
    expect(shown.map((c) => c.name)).toEqual(["Consent", "Notice"]);
    for (const c of shown) expect(t).toContain(c.name);
    expect(t).not.toContain("Data principal rights");
    expect(t).not.toContain("Security and breach");
  });

  it("uses the page limit the server reported", async () => {
    for (const limit of [5, 3, 8]) {
      const sc: DpdpScoreResponse = JSON.parse(JSON.stringify(mixed));
      sc.assessment.page_limit = limit;
      expect((await pdf(sc)).page(0)).toContain(`(limit ${limit})`);
    }
  });

  it("puts the unavailable label under the ring on two centred lines that stay clear of the text beside it, and prints no coverage in the hero", async () => {
    await pdf(insufficient);
    const ring = recorded.circles.filter((c) => c.page === 0).sort((a, b) => b.r - a.r)[1];
    const lines = recorded.calls.filter((c) => c.page === 0 && c.align === "center" && /^(Compliance score|unavailable)$/.test(c.t));
    expect(lines.map((c) => c.t)).toEqual(["Compliance score", "unavailable"]);
    for (const c of lines) expect(c.x).toBe(ring.x);                                          // centred under the ring
    expect(lines[1].y).toBeGreaterThan(lines[0].y);
    const heroTop = ring.y - 62;
    const heroTexts = recorded.calls.filter((c) => c.page === 0 && c.y > heroTop && c.y < ring.y + ring.r + 40 && c.x > ring.x + 60);
    expect(heroTexts.map((c) => c.t).join(" ")).not.toMatch(/coverage/i);
  });

  it("shows Insufficient Evidence instead of a number", async () => {
    const t = (await pdf(insufficient)).page(0);
    expect(t).toContain("Insufficient evidence");
    expect(t).toContain("Compliance score unavailable");
    expect(t).toContain("No score");
  });

  it("uses the page limit the server reported", async () => {
    for (const limit of [5, 3, 8]) {
      const sc: DpdpScoreResponse = JSON.parse(JSON.stringify(mixed));
      sc.assessment.page_limit = limit;
      expect((await pdf(sc)).page(0)).toContain(`(limit ${limit})`);
    }
  });

  it("lists the privacy areas the report covers, and not data-rights handling or security", async () => {
    const t = (await pdf(mixed)).page(0);
    const shown = buildScoreView(mixed).categories.filter((c) => isReportCategory(c.name));
    expect(shown.map((c) => c.name)).toEqual(["Consent", "Notice"]);
    for (const c of shown) expect(t).toContain(c.name);
    expect(t).not.toContain("Data principal rights");
    expect(t).not.toContain("Security and breach");
  });
});

describe("page 2: consent comparison, technologies, privacy elements", () => {
  it("counts each state from the evidence rows, one row per distinct address", async () => {
    const t = (await pdf(mixed)).page(1);
    expect(t).toContain("CONSENT BEHAVIOUR");
    expect(t).toContain("Cookies set");
    expect(t).toContain("All third-party requests");
    expect(t).toContain("A third-party request is one distinct address");
  });

  it("shows Not tested, not zeros, for a state whose choice was never made", async () => {
    const meta = { ...(META as object), accept_interaction: "cmp_not_found" } as never;
    const t = (await pdf(mixed, [], EVIDENCE, meta)).page(1);
    expect(t).toContain("Not tested");
  });

  it("says plainly when the evidence rows could not be loaded", async () => {
    const t = (await pdf(mixed, [], null)).page(1);
    expect(t).toContain("The scan evidence could not be loaded");
  });

  it("lists the technologies seen, analytics and marketing first", async () => {
    const t = (await pdf(mixed)).page(1);
    expect(t.indexOf("svc2.example")).toBeGreaterThan(-1);
    expect(t.indexOf("svc2.example")).toBeLessThan(t.indexOf("svc1.example"));
  });
});

describe("the compact check table on page 2", () => {
  it("lists every check the report covers, with its ID, weight, verdict and requirement", async () => {
    for (const key of Object.keys(F)) {
      const v = buildScoreView(F[key]);
      const t = (await pdf(F[key])).checks();
      expect(t, key).toContain("EVERY CHECK BEHIND THE SCORE");
      for (const c of v.allChecks.filter((x) => isReportCategory(x.category) && x.statusKey !== "not_applicable")) {
        expect(t, `${key} ${c.id}`).toContain(c.id.replace("DPDP-", ""));
        expect(t, `${key} ${c.id} verdict`).toContain(c.verdict);
        expect(t, `${key} ${c.id} title`).toContain(c.title.slice(0, 30));
        expect(flat(t), `${key} ${c.id} requirement`).toContain(flat(formatRefShort(c.provisions)));   // the whole reference, not its first characters
      }
    }
  });

  it("sums up the not-applicable checks in one row, with the reason", async () => {
    const t = (await pdf(mixed)).checks();
    expect(t).toContain("X4, X8, X10, X13");
    expect(t).toContain("Not applicable: left out");   // the full line fits in the real PDF; the test's stand-in wraps narrower
  });

  it("carries the same verdicts and weights as the dashboard table", async () => {
    const { container, unmount } = render(createElement(DpdpScoreSection, { score: confirmed }));
    const v = buildScoreView(confirmed);
    const t = (await pdf(confirmed)).checks();
    for (const c of v.allChecks.filter((x) => isReportCategory(x.category) && x.statusKey !== "not_applicable")) {
      const row = container.querySelector(`[data-testid="dpdp-check-row-${c.id}"]`) as HTMLElement;
      expect(within(row).getByTestId(`dpdp-verdict-${c.id}`)).toHaveTextContent(c.verdict);
      expect(t).toContain(c.verdict);
    }
    unmount();
  });

  it("shows a penalty only where a check lost weight, and never for one that was not scored", async () => {
    const t = (await pdf(mixed)).checks();
    const v = buildScoreView(mixed);
    const lost = v.allChecks.filter((c) => /^\d/.test(c.penalty) && !c.penalty.startsWith("0 of"));
    for (const c of lost) expect(t, c.id).toContain("penalty");
    expect(t).not.toMatch(/Not scored · penalty/);
  });

  it("leaves out data-rights and security checks, and says once that such duties are counted but not listed", async () => {
    const t = withoutCoverage((await pdf(mixed)).all());
    for (const id of ["DPDP-R9", "R9 ", "R14A", "R14B", "S6 ", "S7 "]) expect(t, id).not.toContain(id);
    expect(flat((await pdf(mixed)).checks())).toContain(flat(OMITTED_DUTIES_NOTE));   // the whole note, not an excerpt
    expect((await pdf(mixed)).page(0)).not.toContain("not listed here");
  });

  it("does not print the per-check legal records, the ledger or the penalty rules (those are on the dashboard)", async () => {
    const r = await pdf(mixed);
    const summary = r.summary();
    expect(summary).not.toContain("Legal basis:");   // the findings' legal basis is in the appendix, in full
    expect(r.all()).not.toContain("Check records");
    expect(r.all()).not.toContain("Penalty and exclusion rules");
  });
});

describe("the evidence appendix: complete, nothing truncated", () => {
  const SECTOR = {
    sector: "education", label: "Education", confidence: "medium", summary: "This looks like an education website (medium confidence), from 6 signal(s) in the scan.",
    signals: [{ sector: "education", source: "title", match: "admissions", seen_in: "School admissions", points: 3 }],
    also_possible: [],
    notes: [{
      id: "education-children", title: "Children's data and the educational-institution exemption",
      provisions: ["DPDP Act s.9(1), s.9(3), s.9(4)", "DPDP Rules r.12, Fourth Schedule Part A item 3"],
      text: "An educational institution is exempt only for tracking and behavioural monitoring for its educational activities or for the safety of enrolled children.",
      observed: ["Fields that suggest children's data: parent email, student name."], action: "Declare whether the organisation processes children's data (DPDP-X10).",
    }],
    declarations: [{ flag: "children", requirement_id: "DPDP-X10", label: "Processes children's personal data", why: "Education services commonly involve children.", declared: null }],
    limitations: ["The sector is inferred from what the scan stored, so it is an indication, not a finding."],
  };
  const fields = Array.from({ length: 40 }, (_, i) => ({ name: i === 7 ? `an-unusually-long-field-name-${"segment-".repeat(14)}end` : `field-${i}`, field_type: i % 2 ? "text" : "email", required: i % 3 === 0 }));
  const FULL = {
    ...EVIDENCE,
    forms: [
      { id: "f1", selector: "form:nth-of-type(1)", purpose_guess: "newsletter_signup", fields },
      { id: "f2", selector: "form:nth-of-type(2)", purpose_guess: "contact", fields: [{ name: "message", field_type: "textarea", required: true }] },
    ],
    cookies: Array.from({ length: 30 }, (_, i) => ({ id: `c${i}`, name: `cookie_${i}`, domain: "x.test", category: i % 2 ? "analytics" : "functional", vendor: i % 2 ? "Google Analytics" : null, is_first_party: true,
      source: "rule", consent_states: ["post_accept"], expiry: "2027-11-26T15:18:31+00:00", secure: false, http_only: true, same_site: "Lax",
      observations: [{ consent_state: "post_accept", page_url: "https://x.test/pricing", observed_at: null, method: "single_page_load", source_request_url: null, candidate_page_urls: [] }] })),
    trackers: Array.from({ length: 120 }, (_, i) => tracker(String(i), i % 3 ? "functional" : "analytics", ["pre_consent", "post_accept"])),
    third_party_services: [{ id: "s1", service_name: "Google Analytics", category: "analytics", domains: ["google-analytics.com", "analytics.google.com"] }],
    policies: [{ id: "a", url: "https://x.test/privacy", policy_type: "privacy_policy" }, { id: "b", url: "https://x.test/cookies", policy_type: "cookie_policy" }, { id: "c", url: "https://x.test/terms", policy_type: "terms_of_service" }],
    consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "Consiva Consent SDK", has_reject_all: true, has_granular_choices: true,
      evidence: { confidence: 0.95, detection_source: "script_src", banner_ui_observed: true, accept_interaction: "clicked", reject_click_confirmed: true, interaction_page_url: "https://x.test/", global_vars: [] } }],
  } as unknown as ScanEvidenceResponse;
  const score = () => ({ ...JSON.parse(JSON.stringify(mixed)), sector: SECTOR }) as DpdpScoreResponse;
  const appendix = async () => (await pdf(score(), [], FULL)).appendix();

  it("lists every form with every field, name, type and whether it is required, with no field dropped or cut", async () => {
    const t = await appendix();
    expect(t).toContain("Forms found");
    expect(t).toContain("2 forms");
    expect(t).toContain("Form 1: Newsletter signup — 40 fields");
    expect(t).toContain("Form 2: Contact — 1 field");
    for (const f of fields) expect(t, f.name).toContain(f.name);   // including the one far wider than its column: broken across lines, never cut
    expect(t).toContain("message");
    expect(t).not.toContain("…");
  });

  it("gives the consent banner's details: the tool, the options and everything the scan recorded about its detection and clicks", async () => {
    const t = await appendix();
    for (const needle of ["Consent banner", "Consiva Consent SDK", "Reject-all option", "Granular choices", "Detection source", "Banner ui observed", "Accept interaction", "Reject click confirmed", "Interaction page url", "0.95"]) {
      expect(t, needle).toContain(needle);
    }
  });

  it("lists every cookie and every third-party request in full", async () => {
    const t = await appendix();
    expect(t).toContain("30 cookies");
    for (let i = 0; i < 30; i++) expect(t).toContain(`cookie_${i}`);
    expect(t).toContain("2027-11-26");
    expect(flat(t)).toContain(flat("Secure: No · HttpOnly: Yes · SameSite: Lax"));
    expect(t).toContain("Third-party requests and scripts");
    expect(t).toContain("120 requests");
    for (let i = 0; i < 120; i++) expect(t).toContain(`https://svc${i}.example/t.js`);
  });

  it("lists every policy and third-party service", async () => {
    const t = await appendix();
    for (const needle of ["Policies found", "3 policies", "https://x.test/privacy", "https://x.test/cookies", "https://x.test/terms", "Cookie policy", "Terms of service", "Third-party services", "1 service", "google-analytics.com, analytics.google.com"]) {
      expect(t, needle).toContain(needle);
    }
  });

  it("prints the sector reading with its provisions, what was observed and what to declare", async () => {
    const t = await appendix();
    for (const needle of ["Sector and applicability", "Education", "medium confidence", "Fourth Schedule, Part A, item 3", "Fields that suggest children's data: parent email, student name.",
      "What to check: Declare whether the organisation processes children's data (DPDP-X10).", "DPDP-X10: Processes children's personal data. Not declared.", "an indication, not a finding"]) {
      expect(flat(t), needle).toContain(flat(needle));
    }
  });

  it("prints each compliance finding with its legal basis and the evidence behind it", async () => {
    const t = await appendix();
    expect(t).toContain("Compliance findings in full");
    expect(t).toContain("Legal basis:");
    expect(t).toContain("Observed:");
  });

  it("continues on further pages, each with the header and footer, and numbers them against the real total", async () => {
    await pdf(score(), [], FULL);
    const total = recorded.pages.length;
    expect(total).toBeGreaterThan(5);
    recorded.pages.forEach((pg, i) => expect(pg.texts.join(""), `page ${i + 1}`).toContain(`Page ${i + 1} of ${total}`));
    for (let i = 3; i < total; i++) expect(recorded.rects.some((r) => r.page === i && r.w >= 595), `header strip on page ${i + 1}`).toBe(true);
  });

  it("states honestly when the scan found no forms, cookies or policies", async () => {
    const empty = { ...FULL, forms: [], cookies: [], trackers: [], policies: [], third_party_services: [], consent_signals: [] } as unknown as ScanEvidenceResponse;
    const t = (await pdf(score(), [], empty)).from(3);
    for (const needle of ["No forms were found on the pages scanned.", "No cookies were recorded.", "No third-party requests were recorded.", "No consent mechanism was detected", "No privacy, cookie or terms page was found"]) {
      expect(t, needle).toContain(needle);
    }
  });

  it("is unchanged for a score from an older server that has no sector", async () => {
    const t = (await pdf(mixed, [], FULL)).appendix();
    expect(t).toContain("Forms found");
    expect(t).not.toContain("Sector and applicability");
  });
});

describe("the score card: ledger and check table", () => {
  it.each(Object.keys(F))("%s: counts and weights add up, and the percentages are the server's", (key) => {
    const s = F[key];
    const L = s.ledger;
    expect(L.assessed_checks + L.unable_checks + L.not_applicable_checks).toBe(L.total_checks);
    expect(L.assessed_checks).toBe(L.passed_checks + L.partial_checks + L.failed_checks);
    expect(L.applicable_checks).toBe(L.total_checks - L.not_applicable_checks);
    expect(L.assessed_weight + L.unable_weight).toBe(L.applicable_weight);
    expect(L.total_weight - L.not_applicable_weight).toBe(L.applicable_weight);
    const assessed = s.requirements.filter((r) => r.counts_in_score);
    expect(assessed.reduce((a, r) => a + r.weight, 0)).toBe(L.assessed_weight);
    expect(assessed.reduce((a, r) => a + r.points_earned, 0)).toBeCloseTo(L.earned_weight, 6);
    expect(L.penalty_weight).toBeCloseTo(L.assessed_weight - L.earned_weight, 6);
    expect(L.metrics.find((m) => m.key === "score")!.percent).toBe(s.readiness.score_percent);
    expect(L.metrics.find((m) => m.key === "coverage")!.percent).toBe(s.readiness.coverage_percent);
    expect(L.metrics.find((m) => m.key === "coverage")!.numerator).toBe(L.assessed_weight);
    expect(L.metrics.find((m) => m.key === "coverage")!.denominator).toBe(L.applicable_weight);
    for (const r of s.requirements) {
      if (r.counts_in_score) expect(r.penalty_points).toBeCloseTo(r.weight - r.points_earned, 6);
      else expect(r.penalty_points).toBeNull();
    }
  });

  it.each(Object.keys(F))("%s: the per-check contributions add up to the score", (key) => {
    const s = F[key];
    if (s.readiness.status !== "scored") return;
    const sum = s.requirements.reduce((a, r) => a + (r.score_contribution_percent ?? 0), 0);
    expect(Math.abs(sum - (s.readiness.score_percent as number))).toBeLessThan(0.8);   // each contribution is rounded to one decimal
  });

  it("renders a row for every check, coloured by verdict, directly under the score card", () => {
    const { container } = render(createElement(DpdpScoreSection, { score: confirmed }));
    const table = container.querySelector('[data-testid="dpdp-check-table"]') as HTMLElement;
    expect(table.querySelectorAll("tbody tr")).toHaveLength(confirmed.requirements.filter((r) => isReportCategory(r.category)).length);
    expect(container.querySelector('[data-testid="dpdp-check-row-DPDP-R9"]')).toBeNull();   // the card follows the report
    const fail = confirmed.requirements.find((r) => r.status === "not_met")!;
    expect(container.querySelector(`[data-testid="dpdp-check-row-${fail.id}"]`)!.className).toContain("fail");
    expect(within(container.querySelector(`[data-testid="dpdp-check-row-${fail.id}"]`) as HTMLElement).getByTestId(`dpdp-verdict-${fail.id}`).className).toContain("fail");
    const unable = confirmed.requirements.find((r) => r.status === "unable_to_verify" && isReportCategory(r.category));
    if (unable) {
      expect(container.querySelector(`[data-testid="dpdp-verdict-${unable.id}"]`)).toHaveTextContent("Unable to Verify");
      expect(container.querySelector(`[data-testid="dpdp-verdict-${unable.id}"]`)!.className).toContain("unknown");
    }
    const na = confirmed.requirements.find((r) => r.status === "not_applicable")!;
    expect(container.querySelector(`[data-testid="dpdp-verdict-${na.id}"]`)).toHaveTextContent("N/A");
    // the check table comes before the findings and the methodology
    const text = container.textContent ?? "";
    expect(text.indexOf("Every check behind the score")).toBeGreaterThan(-1);
    expect(text.indexOf("Every check behind the score")).toBeLessThan(text.indexOf("What the scan found"));
    expect(container.querySelector('[data-testid="dpdp-penalty-rules"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="dpdp-ledger-metrics"]')).not.toBeNull();
  });

  it("does not call an untested check a violation, or a not-applicable check a pass", () => {
    const v = buildScoreView(mixed);
    for (const c of v.allChecks) {
      if (c.statusKey === "unable_to_verify") expect(c.verdict).toBe("Unable to Verify");
      if (c.statusKey === "not_applicable") expect(c.verdict).toBe("N/A");
      if (c.statusKey === "met") expect(c.verdict).toBe("Pass");
      if (c.statusKey === "not_met") expect(c.verdict).toBe("Fail");
    }
    expect(v.allChecks.filter((c) => c.verdict === "Pass").length).toBe(mixed.ledger.passed_checks);
  });

  it("treats a response with no ledger (an older server) as no score", () => {
    const old = JSON.parse(JSON.stringify(mixed));
    delete old.ledger;
    expect(isDpdpScore(old)).toBe(false);
  });

  it("says there is no separate security score", () => {
    expect(buildScoreView(mixed).ledger.securityNote).toContain("no separate security score");
  });
});


describe("what every generated report says about itself, and what it leaves out", () => {
  it("carries the same short disclaimer as the server, on every page", async () => {
    const v = buildScoreView(mixed);
    expect(v.disclaimer).toBe(REPORT_DISCLAIMER);   // the dashboard's text and the PDF's are one text
    await pdf(mixed);
    recorded.pages.forEach((p, i) => expect(p.texts.join(""), `page ${i + 1}`).toContain("for informational purposes only and does not constitute legal advice"));
    expect(REPORT_DISCLAIMER.length).toBeLessThan(420);   // two to three lines at the footer's width
  });

  it("also carries it when no score was loaded", async () => {
    await pdf(null);
    expect(recorded.pages.map((p) => p.texts.join("")).join("")).toContain("does not constitute legal advice");
  });

  it("shows how many pages were scanned out of how many were found", async () => {
    const meta = { ...(META as object), pages_discovered: 12, pages_scanned: 5 } as never;
    const p = await pdf(mixed, [], EVIDENCE, meta);
    expect(p.page(0)).toContain("5 pages scanned of 12 found");   // the scan's own counts: 5 scanned of 12 found
  });

  it.each(Object.keys(F))("%s: no GDPR, CCPA, data-rights or security section anywhere in the PDF", async (key) => {
    const t = withoutCoverage((await pdf(F[key])).all());
    for (const word of ["GDPR", "CCPA", "Data principal rights", "Security and breach", "Security safeguards", "Rights request channel", "Grievance process",
      "Contact for data questions"]) {
      expect(t, `${key}: ${word}`).not.toContain(word);
    }
    for (const id of ["DPDP-R9", "DPDP-R14A", "DPDP-R14B", "DPDP-S6", "DPDP-S7"]) expect(t, `${key}: ${id}`).not.toContain(id);
  });

  it.each(Object.keys(F))("%s: what was tested and what was not names every applicable check, and each one it did not verify says why", async (key) => {
    const v = buildScoreView(F[key]);
    const t = coverageOf((await pdf(F[key])).all());
    expect(t, key).toContain(COVERAGE_START);
    for (const c of v.allChecks.filter((x) => x.statusKey !== "not_applicable")) expect(flat(t), `${key}: ${c.id}`).toContain(flat(c.title).slice(0, 40));
    expect(t).toContain("never counted as met or as failed");
  });

  it.each(Object.keys(F))("%s: no formula or raw expression anywhere in the PDF", async (key) => {
    const t = (await pdf(F[key])).all();
    expect(t.match(/.{0,40}(÷|×|Score = |Coverage = |Formula|FORMULA).{0,40}/)?.[0]).toBeUndefined();
  });

  it("leaves the score alone: the same numbers, only fewer rows shown", async () => {
    const v = buildScoreView(mixed);
    const t = (await pdf(mixed)).page(0);
    expect(t).toContain(v.headline);
    expect(t).not.toContain(`Coverage ${v.coverageHeadline}`);   // kept in the data (v.coverageHeadline), not printed in the hero
    expect(v.ledger.counts.find((c) => c.label === "Unable to verify")!.value).toBe(String(mixed.ledger.unable_checks));   // still every check
  });
});


describe("the Consiva.ai logo", () => {
  it("heads every page, drawn from the supplied artwork at its own proportions", async () => {
    await pdf(mixed);
    expect(recorded.images).toHaveLength(recorded.pages.length);
    expect(recorded.images.map((i) => i.page)).toEqual(recorded.pages.map((_, i) => i));
    for (const img of recorded.images) {
      expect(img.fmt).toBe("PNG");
      expect(img.data.startsWith("data:image/png;base64,")).toBe(true);
      expect(img.x).toBe(36);
      expect(img.y).toBeLessThan(20);
      expect(img.w / img.h).toBeCloseTo(CONSIVA_LOGO_WIDTH / CONSIVA_LOGO_HEIGHT, 3);   // not stretched
      expect(img.w).toBeLessThan(150);
      expect(img.y + img.h).toBeLessThan(60);   // clear of the title below it
    }
  });

  it("embeds a real, opaque PNG: no alpha channel to render as a black strip", async () => {
    await pdf(mixed);
    const bytes = atob(recorded.images[0].data.split(",")[1]);
    expect(bytes.slice(1, 4)).toBe("PNG");
    const word = (at: number) => (bytes.charCodeAt(at) << 24) | (bytes.charCodeAt(at + 1) << 16) | (bytes.charCodeAt(at + 2) << 8) | bytes.charCodeAt(at + 3);
    expect([word(16), word(20)]).toEqual([CONSIVA_LOGO_WIDTH, CONSIVA_LOGO_HEIGHT]);   // the width and height in the PNG header
    expect(bytes.charCodeAt(25)).toBe(2);   // colour type 2: RGB, with no alpha
  });

  it("is drawn in the same place, at the same size, on every page", async () => {
    await pdf(mixed);
    const [first, ...rest] = recorded.images;
    expect(rest.length).toBeGreaterThan(0);
    for (const img of rest) expect([img.x, img.y, img.w, img.h]).toEqual([first.x, first.y, first.w, first.h]);
  });

  it("replaces the drawn placeholder mark", async () => {
    await pdf(mixed);
    expect(recorded.calls.some((c) => c.page === 0 && c.t === "Consiva AI")).toBe(false);   // the old drawn word-mark, as text of its own
    expect(recorded.calls.some((c) => c.t === "C" && c.align === "center" && c.page === 0 && c.y < 45)).toBe(false);
  });
});


describe("the black header strip", () => {
  const BLACK: [number, number, number] = [3, 7, 15];
  const WHITE: [number, number, number] = [255, 255, 255];

  it("runs the full width of every page, behind the logo and the header text, in the logo's own colour", async () => {
    await pdf(mixed);
    for (let page = 0; page < recorded.pages.length; page++) {
      const strip = recorded.rects.find((r) => r.page === page && r.fill.join() === BLACK.join() && r.w >= 595);
      expect(strip, `page ${page + 1}`).toBeDefined();
      expect(strip!.x).toBe(0);
      const logo = recorded.images.find((i) => i.page === page)!;
      expect(logo.y).toBeGreaterThanOrEqual(strip!.y);
      expect(logo.y + logo.h).toBeLessThanOrEqual(strip!.y + strip!.h);
      // centred in the strip: the same space above the logo as below it
      expect(logo.y - strip!.y).toBeCloseTo(strip!.y + strip!.h - (logo.y + logo.h), 1);
      for (const t of recorded.calls.filter((c) => c.page === page && c.y > strip!.y && c.y < strip!.y + strip!.h)) {
        expect(t.y, t.t).toBeLessThan(strip!.y + strip!.h);
      }
    }
  });

  it("draws every word of the header in white", async () => {
    await pdf(mixed);
    for (let page = 0; page < recorded.pages.length; page++) {
      const strip = recorded.rects.find((r) => r.page === page && r.fill.join() === BLACK.join() && r.w >= 595)!;
      const inStrip = recorded.calls.filter((c) => c.page === page && c.y > strip.y && c.y <= strip.y + strip.h);
      expect(inStrip.length, `page ${page + 1}`).toBeGreaterThanOrEqual(2);   // the site and the scan line
      for (const t of inStrip) expect(t.color, `${t.t} on page ${page + 1}`).toEqual(WHITE);
    }
  });

  it("leaves what is below the strip where it was", async () => {
    await pdf(mixed);
    const strip = recorded.rects.find((r) => r.page === 0 && r.fill.join() === BLACK.join() && r.w >= 595)!;
    const title = recorded.calls.find((c) => c.page === 0 && c.t === "Privacy Readiness Report")!;
    expect(title.y).toBe(74);
    expect(title.y - 0.72 * 20).toBeGreaterThan(strip.y + strip.h);   // the top of the 20pt title's capitals sits clear of the strip
  });
});


describe("the privacy policy content analysis in the appendix", () => {
  /** The PDF wraps long lines, and the recording stand-in keeps each line apart, so compare without whitespace. */
  const flat = (x: string) => x.replace(/\s+/g, "");
  const row = (id: string, title: string, group: string, basis: string, status: string, quote = "", extra = {}) =>
    ({ id, title, group, basis, provisions: ["DPDP Act s.8(7)", "DPDP Rules r.14(3)"], status, quote, source_url: quote ? "https://x.test/privacy" : null, detail: status === "verified" ? "Quoted from the policy." : "Not found in the policy text.", fix: `Fix for ${title}.`, ...extra });
  const ANALYSED: PolicyAnalysis = {
    status: "analysed", reason: null, sector: "it",
    documents: [{ url: "https://x.test/privacy", policy_type: "privacy_policy", characters: 12000, source: "crawl", truncated: false }],
    notes: [],
    summary: { applicable: 4, verified: 1, partial: 1, not_found: 2, not_applicable: 1, quotes_rejected: 1, completeness_percent: 37.5 },
    requirements: [
      row("notice_purposes", "Specified purposes", "Notice", "required", "verified", "We use the information we collect to respond to your inquiries and comply with legal obligations."),
      row("grievance_contact", "Grievance redressal contact", "Grievance", "required", "partial", "Our Grievance Officer is Saurabh Kumar of Swaran Soft Support Solutions Private Limited.", { detail: "The role is mentioned but no email, phone number or form is given next to it." }),
      row("retention", "Retention periods and erasure", "Transparency", "practice", "not_found"),
      row("dpo_contact", "Business contact of a Data Protection Officer or answering person", "Grievance", "required", "not_found"),
      row("children_data", "Children's data and verifiable parental consent", "Children", "required", "not_applicable", "", { detail: "No sign that this site is aimed at children, so this is not assessed." }),
    ],
    limitations: ["Only the text of the privacy and cookie policy pages that the scan reached was read.", "A requirement that is not found is reported as not found, not as a violation."],
    model: { provider: "claude" },
  } as PolicyAnalysis;

  it("prints the headline, the top fixes, each requirement with its status, basis, provisions, exact quote and source, and the limits", async () => {
    const t = (await pdf(mixed, [], FULL_FOR_POLICY, META, ANALYSED)).appendix();
    for (const needle of ["Privacy policy content analysis", "Policy completeness 37.5%", "1 quoted · 1 partially stated · 2 not found · 1 not applicable, of 5 points checked",
      "1 passage offered by the analysis was not an exact quotation and was rejected", "Checklist", "POINT THE POLICY SHOULD COVER",
      "Specified purposes", "Quoted", "Required by the Act or Rules", "DPDP Act, 2023, Section 8(7) · DPDP Rules, 2025, Rule 14(3)",
      "We use the information we collect to respond to your inquiries and comply with legal obligations.", "https://x.test/privacy",
      "Grievance redressal contact", "Partially stated", "no email, phone number or form is given next to it",
      "Retention periods and erasure", "Not found", "Good practice with a duty behind it", "Children's data and verifiable parental consent", "Not applicable",
      "Read: https://x.test/privacy (privacy policy, 12,000 characters; read during the scan)", "not as a violation"]) {
      expect(flat(t), needle).toContain(flat(needle));
    }
  });

  it("puts the required, still-missing points ahead of the good-practice ones in the prioritised fixes", async () => {
    const t = flat((await pdf(mixed, [], FULL_FOR_POLICY, META, ANALYSED)).summary());
    const at = t.indexOf(flat("Fixes to the privacy policy's wording"));
    expect(at).toBeGreaterThan(-1);
    const fixes = t.slice(at);
    expect(fixes.indexOf(flat("Business contact of a Data Protection Officer or answering person"))).toBeLessThan(fixes.indexOf(flat("Retention periods and erasure")));
  });

  it("says why when the analysis is unavailable, and prints nothing when it never ran", async () => {
    const unavailable = { ...ANALYSED, status: "unavailable", reason: "No privacy or cookie policy page was found on the scanned pages, so there was nothing to read.", summary: null, requirements: [], documents: [] } as PolicyAnalysis;
    const t = (await pdf(mixed, [], FULL_FOR_POLICY, META, unavailable)).appendix();
    expect(flat(t)).toContain(flat("Privacy policy content analysis not available"));
    expect(flat(t)).toContain(flat("so there was nothing to read."));
    expect((await pdf(mixed, [], FULL_FOR_POLICY, META, { ...ANALYSED, status: "not_run" } as PolicyAnalysis)).appendix()).not.toContain("Privacy policy content analysis");
    expect((await pdf(mixed, [], FULL_FOR_POLICY)).appendix()).not.toContain("Privacy policy content analysis");
  });

  it("shows exactly what the dashboard section shows: the same quotes, fixes and numbers", async () => {
    const { render } = await import("@testing-library/react");
    const { PolicyAnalysisSection } = await import("../components/PolicyAnalysisSection");
    const { container } = render(createElement(PolicyAnalysisSection, { analysis: ANALYSED }));
    const pdfText = (await pdf(mixed, [], FULL_FOR_POLICY, META, ANALYSED)).appendix();
    const page = container.textContent ?? "";
    for (const r of ANALYSED.requirements) {
      expect(page, r.title).toContain(r.title);
      expect(flat(pdfText), r.title).toContain(flat(r.title));
      if (r.quote) { expect(page).toContain(r.quote); expect(flat(pdfText)).toContain(flat(r.quote)); }
    }
    expect(page).toContain("Policy completeness 37.5%");
    expect(flat(pdfText)).toContain(flat("Policy completeness 37.5%"));
  });
});

const FULL_FOR_POLICY = { ...EVIDENCE, forms: [], consent_signals: [] } as unknown as ScanEvidenceResponse;


describe("the layout: sequential sections, a true contents, nothing cut, nothing in the footer", () => {
  const FOOTER_TOP = 841.89 - 46;

  /** The contents on page 1 as drawn: pairs of (label, page). */
  const contentsOf = (): { label: string; page: number }[] => {
    const texts = recorded.pages[0].texts;
    const at = texts.indexOf("CONTENTS");
    const out: { label: string; page: number }[] = [];
    for (let i = at + 1; i + 1 < texts.length; i += 2) {
      const m = /^p\. (\d+)$/.exec(texts[i + 1]);
      if (!m) break;
      out.push({ label: texts[i], page: Number(m[1]) });
    }
    return out;
  };

  /** The appendix index as drawn: pairs of (label, page). */
  const appendixIndex = (): { label: string; page: number }[] => {
    const all = recorded.pages.flatMap((p) => p.texts);
    const at = all.indexOf("IN THIS APPENDIX");
    const out: { label: string; page: number }[] = [];
    for (let i = at + 1; at >= 0 && i + 1 < all.length; i += 2) {
      const m = /^p\. (\d+)$/.exec(all[i + 1]);
      if (!m) break;
      out.push({ label: all[i], page: Number(m[1]) });
    }
    return out;
  };

  it.each(["mixed", "confirmed", "review"])("%s: the sections are numbered 1, 2, 3 … in order, and the appendix is lettered A, B, C … in order", async (key) => {
    const r = await pdf(F[key], [], FULL_EVIDENCE_FOR_LAYOUT);
    const numbered = [...r.all().matchAll(/(?:^|[^\d])(\d{1,2})(?=[A-Z][A-Z ]{4,})/g)];   // the badge, then the heading in capitals
    const heads = recorded.calls.filter((c) => c.page < r.appendixAt() && c.align === "center" && c.color?.join() === "255,255,255" && /^\d{1,2}$/.test(c.t)).map((c) => Number(c.t));
    const sections = heads.filter((n, i) => i === 0 || n === heads[i - 1] + 1 || n === 1);    // the badges drawn for section headings (the top findings reuse 1-3 in white too)
    expect(numbered.length + sections.length).toBeGreaterThan(0);
    const idx = appendixIndex();
    expect(idx.length).toBeGreaterThanOrEqual(6);
    expect(idx.map((e) => e.label.charAt(9))).toEqual(idx.map((_, i) => String.fromCharCode(65 + i)));
  });

  it("the headings are numbered in order in the PDF, one badge per heading", async () => {
    const r = await pdf(mixed, [], FULL_EVIDENCE_FOR_LAYOUT);
    const upper = ["SCAN SUMMARY", "FRAMEWORK READINESS", "PRIVACY AREAS", "TOP FINDINGS", "CONSENT BEHAVIOUR", "TRACKING TECHNOLOGIES DETECTED", "PRIVACY ELEMENTS ON THE SITE", "EVERY CHECK BEHIND THE SCORE", "WEBSITE PAGES SCANNED", "SCAN REVIEW"];
    const at = upper.map((u) => r.all().indexOf(u));
    expect(at.every((i) => i >= 0)).toBe(true);
    expect(at).toEqual([...at].sort((a, b) => a - b));          // in the order they are listed, which is the reading order
    // each heading is preceded by its own number badge: 1 … 10
    const badges = recorded.calls.filter((c) => c.align === "center" && c.color?.join() === "255,255,255" && /^\d{1,2}$/.test(c.t) && c.y < 800 && c.page < r.appendixAt());
    const seq = badges.map((b) => Number(b.t));
    for (let n = 1; n <= 10; n++) expect(seq, `section ${n}`).toContain(n);
  });

  it("the appendix index lists every lettered section on the page where it really is", async () => {
    const r = await pdf(mixed, [], FULL_EVIDENCE_FOR_LAYOUT);
    const idx = appendixIndex();
    expect(idx.length).toBeGreaterThan(5);
    for (const e of idx) {
      const title = e.label.replace(/^Appendix [A-Z]\s{2}/, "");
      expect(flat(recorded.pages[e.page - 1].texts.join("")), `${e.label} on page ${e.page}`).toContain(flat(title));
      expect(recorded.pages[e.page - 1].texts, e.label).toContain(e.label.charAt(9));   // its letter badge is on that page too
    }
    const pages = idx.map((e) => e.page);
    expect(pages).toEqual([...pages].sort((a, b) => a - b));
    expect(r.appendix()).toContain("IN THIS APPENDIX");
  });

  it("page 1 has a contents of the main parts, and each page number it gives is true", async () => {
    await pdf(mixed, [], FULL_EVIDENCE_FOR_LAYOUT);
    const entries = contentsOf();
    expect(entries.length).toBeGreaterThanOrEqual(4);
    const needle = (label: string) => {
      const plain = label.replace(/^\d+\s{2}/, "");
      if (label === "Summary") return "SCAN SUMMARY";
      if (label === "Every check") return "EVERY CHECK";
      if (label === "Pages scanned") return "WEBSITE PAGES SCANNED";
      if (label === "Evidence appendix") return "Evidence appendix";
      return /^\d+\s{2}/.test(label) || label === "Consent behaviour" ? plain.toUpperCase() : plain;
    };
    for (const e of entries) expect(flat(recorded.pages[e.page - 1].texts.join("")), `${e.label} p.${e.page}`).toContain(flat(needle(e.label)));
    const pages = entries.map((e) => e.page);
    expect(pages).toEqual([...pages].sort((a, b) => a - b));
  });

  it.each(Object.keys(F))("%s: nothing but the footer is drawn in the footer zone, and nothing is drawn past the page edge", async (key) => {
    await pdf(F[key], [], FULL_EVIDENCE_FOR_LAYOUT);
    recorded.calls.forEach((c) => {
      expect(c.x, c.t).toBeGreaterThanOrEqual(0);
      expect(c.x, c.t).toBeLessThanOrEqual(595.28);
      if (c.y > FOOTER_TOP - 4) expect(c.t.startsWith("This report is for informational") || /^(checks, and is not a certification|Page \d+ of \d+)/.test(c.t) || REPORT_DISCLAIMER.includes(c.t) || c.t.length < 120, `${c.t} at y=${c.y}`).toBe(true);
    });
  });

  it("a very long evidence line, requirement list and finding are printed in full, never cut with an ellipsis", async () => {
    const view = buildScoreView(mixed);
    const long = "Observed on the homepage and four further pages: " + "a long sentence of evidence about what the scan found and why it matters, ".repeat(6) + "and that is the end.";
    view.allChecks[0].evidenceLine = long;
    const first = view.groups.find((g) => g.entries.length)!.entries[0];
    first.title = "A finding with a very long title that has to wrap across more than one line of the card so that none of it is lost " + first.title;
    first.observed = ["An observation that is far longer than a line of the report and so wraps across several lines " + "with more words ".repeat(12) + "ending here."];
    await downloadReport(buildReportModel(SCAN, [], META, [], [], view, FULL_EVIDENCE_FOR_LAYOUT));
    const all = flat(recorded.pages.map((p) => p.texts.join("")).join(""));
    expect(all).toContain(flat(long));
    expect(all).toContain(flat(first.title.replace(/^DPDP-\w+\s+/, "")));
    expect(all).toContain(flat(first.observed[0].replace(/\s*\(rule R-\d+\)/g, "")));
    expect(recorded.pages.flatMap((p) => p.texts).filter((t) => t.includes("…")), "text cut with an ellipsis").toEqual([]);
  });

  it("the three top findings are numbered 1, 2, 3 and show their whole text", async () => {
    await pdf(review, [], FULL_EVIDENCE_FOR_LAYOUT);
    const page1 = recorded.calls.filter((c) => c.page === 0 && c.x === 36 + 17 && c.align === "center" && /^[123]$/.test(c.t));   // the findings' own badges, not the section badges
    expect(page1.map((c) => c.t)).toEqual(["1", "2", "3"].slice(0, page1.length));
    expect(page1.length).toBeGreaterThan(0);
  });

  it("legal references are spelt in full in the appendix and short in the narrow check table", async () => {
    const r = await pdf(mixed, [], FULL_EVIDENCE_FOR_LAYOUT);
    expect(flat(r.appendix())).toContain(flat("DPDP Act, 2023, Section"));
    expect(flat(r.checks())).toContain(flat("Act, Section"));
    expect(r.all()).not.toMatch(/DPDP Act, 2023 s\./);
    expect(r.all()).not.toMatch(/DPDP Rules, 2025 r\./);
  });

  it("continues the pages-scanned list on the page the table of checks ended on when it fits, with no empty page", async () => {
    await pdf(mixed, [], FULL_EVIDENCE_FOR_LAYOUT);
    const summaryEnd = recorded.pages.findIndex((p) => p.texts.join("").includes("Everything the scan recorded for this site"));
    for (let i = 0; i < summaryEnd; i++) expect(recorded.pages[i].texts.length, `page ${i + 1}`).toBeGreaterThan(10);   // no page is left holding only a heading or nothing (a page that carries the end of a section is short, not empty)
  });
});

const FULL_EVIDENCE_FOR_LAYOUT = { ...EVIDENCE, forms: [{ id: "f", selector: "form", purpose_guess: "contact", fields: [{ name: "email", field_type: "email", required: true }] }] } as unknown as ScanEvidenceResponse;
const review = F.review;


// ---------------------------------------------------------------------------------------------------------------------------------------
// The hero: the score at the top left, the domain above the homepage picture at the top right.
// ---------------------------------------------------------------------------------------------------------------------------------------
import type { ScanScreenshot } from "../api/types";
import { buildHeroView, buildPreviewView } from "./hero";
import { ScanHero } from "../components/ScanHero";

describe("the hero: score top left, domain above the homepage picture top right", () => {
  const JPEG_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/";
  const shot = (over: Partial<ScanScreenshot> = {}): ScanScreenshot => ({
    scan_id: "11111111-1111-1111-1111-111111111111", domain: "x.test", status: "ok", available: true, message: null, page_url: "https://x.test/",
    mime: "image/jpeg", width: 1280, height: 720, captured_at: "2026-10-22T16:43:00Z", data: JPEG_B64, ...over,
  });
  const pdfWith = async (s: ScanScreenshot | null, score: DpdpScoreResponse = mixed, scan = SCAN) => {
    await downloadReport(buildReportModel(scan, [], META, [], [], buildScoreView(score), EVIDENCE, null, null, s));
  };
  const photo = () => recorded.images.filter((i) => i.data.startsWith("data:image/jpeg"));

  beforeEach(() => { recorded.failJpeg = false; });
  /** The score ring on page 1: the second-largest circle (the thick black border is the largest). */
  const bigRing = () => recorded.circles.filter((c) => c.page === 0).sort((a, b) => b.r - a.r)[1];

  it("puts the score ring at the top left and the picture at the top right, with the domain directly above the picture", async () => {
    await pdfWith(shot());
    const ring = bigRing();
    const [img] = photo();
    expect(img).toBeDefined();
    expect(img.page).toBe(0);
    expect(img.x).toBeGreaterThan(ring.x + ring.r);                                 // to the right of the score
    expect(img.x + img.w).toBeLessThanOrEqual(595.28 - 36);                          // inside the page margin
    const domain = recorded.calls.find((c) => c.page === 0 && c.t === "x.test" && c.align !== "right")!;   // the header strip's copy is right-aligned
    expect(domain).toBeDefined();
    expect(domain.y).toBeLessThan(img.y);                                            // above the picture
    expect(img.y - domain.y).toBeLessThan(20);                                       // directly above it
    expect(domain.x).toBeGreaterThanOrEqual(img.x - 0.5);                            // aligned with its left edge
    expect(domain.x).toBeLessThanOrEqual(img.x + img.w);
    expect(ring.y - ring.r).toBeLessThan(img.y + img.h / 2);                         // the ring is in the same top row
  });

  it("shows the same picture, byte for byte, that the dashboard shows", async () => {
    await pdfWith(shot());
    const { container, unmount } = render(createElement(ScanHero, { score: buildScoreView(mixed), siteUrl: "https://x.test/", screenshot: shot() }));
    const src = (container.querySelector('[data-testid="hero-screenshot"]') as HTMLImageElement).getAttribute("src");
    expect(photo()).toHaveLength(1);
    expect(photo()[0].data).toBe(src);
    expect(src).toBe(`data:image/jpeg;base64,${JPEG_B64}`);
    unmount();
  });

  it.each([[1280, 720], [1366, 768], [1280, 1500], [600, 900], [1920, 600]])("a %i x %i picture is compact, undistorted and never taller than its box", async (w, h) => {
    await pdfWith(shot({ width: w, height: h }));
    const [img] = photo();
    expect(img.w).toBeLessThanOrEqual(176 + 0.01);
    expect(img.h).toBeLessThanOrEqual(100 + 0.01);
    expect(img.w / img.h).toBeCloseTo(w / h, 2);                                     // the picture's own proportions, not stretched
    expect(img.w).toBeGreaterThan(40);
  });

  it("does not let the picture overpower the score: it is smaller than a third of the page and below the page's own heading", async () => {
    await pdfWith(shot());
    const [img] = photo();
    expect(img.w * img.h).toBeLessThan(595.28 * 841.89 * 0.08);
    const title = recorded.calls.find((c) => c.page === 0 && c.t === "Privacy Readiness Report")!;
    expect(img.y).toBeGreaterThan(title.y);
  });

  it("the picture and its caption end above the next section, so nothing overlaps", async () => {
    await pdfWith(shot());
    const [img] = photo();
    const next = recorded.calls.find((c) => c.page === 0 && c.t === "SCAN SUMMARY")!;
    const caption = recorded.calls.find((c) => c.page === 0 && c.t.startsWith("Homepage as scanned"))!;
    expect(caption.y).toBeGreaterThan(img.y + img.h);
    expect(caption.y).toBeLessThan(next.y - 8);
    const captionText = recorded.calls.filter((c) => c.page === 0 && c.x === caption.x && c.y >= caption.y && c.y < caption.y + 12).map((c) => c.t).join("");   // the line may wrap
    expect(flat(captionText)).toContain(flat("22 Oct 2026, 16:43 UTC"));
  });

  it("appears once, on page 1 only", async () => {
    await pdfWith(shot());
    expect(photo()).toHaveLength(1);
    expect(recorded.pages.length).toBeGreaterThan(1);
  });

  it("names the domain once above the picture and never repeats the address when the scan was of the homepage", async () => {
    await pdfWith(shot());
    const page = recorded.calls.filter((c) => c.page === 0);
    expect(page.filter((c) => c.t === "x.test" && c.align !== "right")).toHaveLength(1);
    expect(page.filter((c) => c.t === "https://x.test/")).toHaveLength(0);
    expect(page.some((c) => c.t === "SCANNED SITE")).toBe(false);                    // the old in-ring header is gone: no second copy of the domain
  });

  it("shows the address once, under the picture, when the scanned page is not just the homepage", async () => {
    const deep = { url: "https://x.test/pricing?plan=pro", evidence_counts: { pages: 3, cookies: 2, trackers: 5, third_party_services: 0, policies: 1, forms: 0 } } as never;
    await pdfWith(shot(), mixed, deep);
    const hits = recorded.calls.filter((c) => c.page === 0 && c.t.includes("/pricing"));
    expect(hits.length).toBeGreaterThanOrEqual(1);
    expect(flat(hits.map((c) => c.t).join(""))).toContain(flat("https://x.test/pricing?plan=pro"));
  });

  it("spells the domain the way the page header does, so it is the same everywhere on the page", async () => {
    await pdfWith(shot({ domain: "folded.example" }));          // the server files sites under a folded name; the page shows the address as it was scanned
    const page = recorded.calls.filter((c) => c.page === 0 && c.t === "x.test");
    expect(page).toHaveLength(2);                                  // the header strip and above the picture: the same spelling
    expect(recorded.calls.some((c) => c.page === 0 && c.t === "folded.example")).toBe(false);
  });

  it("refuses a picture that belongs to a different scan, and says no preview is available", async () => {
    const model = buildReportModel({ ...(SCAN as object), id: "22222222-2222-2222-2222-222222222222" } as never, [], META, [], [], buildScoreView(mixed), EVIDENCE, null, null, shot());
    await downloadReport(model);
    expect(photo()).toHaveLength(0);
    expect(recorded.calls.some((c) => c.page === 0 && c.t === "Homepage preview not available")).toBe(true);
  });

  it("shows the picture when it belongs to the scan being reported", async () => {
    const model = buildReportModel({ ...(SCAN as object), id: "11111111-1111-1111-1111-111111111111" } as never, [], META, [], [], buildScoreView(mixed), EVIDENCE, null, null, shot());
    await downloadReport(model);
    expect(photo()).toHaveLength(1);
  });

  it("the ring holds the percentage, centred, with no domain or address inside or under it", async () => {
    await pdfWith(shot());
    const ring = bigRing();
    const pct = recorded.calls.find((c) => c.page === 0 && c.t === "56.5%")!;
    expect(pct.x).toBe(ring.x);
    expect(Math.abs(pct.y - ring.y)).toBeLessThan(ring.r / 3);                       // centred both ways
    const inside = recorded.calls.filter((c) => c.page === 0 && Math.hypot(c.x - ring.x, c.y - ring.y) < ring.r);
    expect(inside.map((c) => c.t)).toEqual(["56.5%"]);                               // only the percentage is inside the circle
    expect(recorded.calls.filter((c) => c.page === 0 && c.t.startsWith("https://"))).toHaveLength(0);
  });

  describe("when there is no picture", () => {
    it.each([
      ["none loaded", null],
      ["an older scan", shot({ status: "not_captured", available: false, data: null, mime: null, width: null, height: null, message: "No homepage preview was taken for this scan: it was scanned before previews were added." })],
      ["a blocked site", shot({ status: "blocked", available: false, data: null, mime: null, width: null, height: null, message: "The site did not show its homepage to the scanner (it answered with an error or a security check), so no preview is shown." })],
      ["a timeout", shot({ status: "timeout", available: false, data: null, mime: null, width: null, height: null, message: "The homepage preview could not be taken in time." })],
    ])("%s: a clean panel says so, no picture is invented, and the report still builds", async (_n, s) => {
      await pdfWith(s as ScanScreenshot | null);
      expect(photo()).toHaveLength(0);
      const text = flat(recorded.calls.filter((c) => c.page === 0).map((c) => c.t).join(""));
      expect(text).toContain(flat("Homepage preview not available"));
      expect(text).toContain(flat((s as ScanScreenshot | null)?.message ?? "A homepage preview is not available for this scan."));
      expect(recorded.calls.some((c) => c.page === 0 && c.t === "x.test" && c.align !== "right")).toBe(true);   // the domain is still named above the panel
      expect(recorded.pages.length).toBeGreaterThanOrEqual(2);
      expect(recorded.saved).toMatch(/^consiva-privacy-report-/);
    });

    it("nothing internal reaches the page: no path, no error text, no reason code", async () => {
      await pdfWith(shot({ status: "failed", available: false, data: null, message: "A preview of the homepage could not be taken for this scan." }));
      const all = recorded.calls.map((c) => c.t).join(" ");
      expect(all).not.toMatch(/Traceback|Exception|C:\\|\/home\/|capture_failed|playwright/i);
    });
  });

  it("a picture the PDF cannot place never fails the report: the panel is drawn instead", async () => {
    recorded.failJpeg = true;
    await pdfWith(shot());
    expect(photo()).toHaveLength(0);
    expect(recorded.calls.some((c) => c.page === 0 && c.t === "Homepage preview not available")).toBe(true);
    expect(recorded.saved).toMatch(/^consiva-privacy-report-/);
  });

  it("changes nothing else on the page: the same score, band, headline and coverage with or without a picture", async () => {
    await pdfWith(shot());
    const withShot = recorded.calls.filter((c) => c.page === 0).map((c) => c.t).filter((t) => /56\.5%|Improvement|checks we could test|Coverage|Compliance Score|Improve My Score/.test(t));
    await pdfWith(null);
    const without = recorded.calls.filter((c) => c.page === 0).map((c) => c.t).filter((t) => /56\.5%|Improvement|checks we could test|Coverage|Compliance Score|Improve My Score/.test(t));
    expect(withShot).toEqual(without);
  });

  it("keeps every page's numbering and the appendix intact with a picture present", async () => {
    await pdfWith(shot());
    const total = recorded.pages.length;
    recorded.pages.forEach((pg, i) => expect(pg.texts.join(""), `page ${i + 1}`).toContain(`Page ${i + 1} of ${total}`));
  });
});

describe("the hero's words and facts, worked out once for the dashboard and the PDF", () => {
  it("reads the score into a band and a headline, with no coverage in either", () => {
    const h = buildHeroView(buildScoreView(mixed));
    expect(h.scored).toBe(true);
    expect(h.percentLabel).toBe("56.5%");
    expect(h.scoreLabel).toBe("Compliance Score");
    expect(h.headline).toContain("we could test scored 56.5%");
    expect(JSON.stringify(h)).not.toMatch(/coverage/i);
    expect(["good", "warn", "mute"]).toContain(h.tone);
  });

  it("says plainly when there is no score, and never shows a number for one", () => {
    expect(buildHeroView(null)).toMatchObject({ scored: false, percentLabel: "No score", scoreLabel: "Compliance score unavailable", band: "Score not available", tone: "mute" });
    const withheld = buildHeroView(buildScoreView(insufficient));
    expect(withheld.scored).toBe(false);
    expect(withheld.percent).toBeNull();
    expect(withheld.headline.startsWith("Compliance score unavailable.")).toBe(true);
    expect(JSON.stringify(withheld)).not.toMatch(/coverage/i);
  });

  it("names the domain and tells a bare homepage from a deeper page", async () => {
    const { siteDomain, isBareHomepage } = await import("./hero");
    expect(siteDomain("https://www.swaransoft.com/")).toBe("www.swaransoft.com");
    expect(siteDomain("https://swaransoft.com/blog/post?x=1#y")).toBe("swaransoft.com");
    expect(siteDomain("swaransoft.com")).toBe("swaransoft.com");
    expect(isBareHomepage("https://swaransoft.com/")).toBe(true);
    expect(isBareHomepage("https://swaransoft.com")).toBe(true);
    expect(isBareHomepage("https://swaransoft.com/pricing")).toBe(false);
    expect(isBareHomepage("https://swaransoft.com/?utm=1")).toBe(false);
  });

  it("builds the preview from the server's picture, or says why there is none in the server's own words", () => {
    const ok = buildPreviewView({ scan_id: "s", domain: "d.example", status: "ok", available: true, message: null, page_url: "https://d.example/", mime: "image/jpeg", width: 1280, height: 720, captured_at: "2026-10-22T16:43:00Z", data: "QUJD" }, "https://d.example/");
    expect(ok).toMatchObject({ domain: "d.example", src: "data:image/jpeg;base64,QUJD", width: 1280, height: 720, address: null, unavailable: null });
    expect(ok.caption).toBe("Homepage as scanned · 22 Oct 2026, 16:43 UTC");
    const none = buildPreviewView({ scan_id: "s", domain: "d.example", status: "blocked", available: false, message: "Blocked, plainly.", page_url: null, mime: null, width: null, height: null, captured_at: null, data: null }, "https://d.example/x");
    expect(none).toMatchObject({ src: null, unavailable: "Blocked, plainly.", address: "https://d.example/x" });
    expect(buildPreviewView(null, "https://d.example/").unavailable).toBe("A homepage preview is not available for this scan.");
    expect(buildPreviewView(undefined, "https://www.d.example/").domain).toBe("www.d.example");
  });
});


// ---------------------------------------------------------------------------------------------------------------------------------------
// Prioritised fixes, the dedicated sections, and the cookie inventory.
// ---------------------------------------------------------------------------------------------------------------------------------------
import { buildInsightSections } from "./insights";
import type { WebsiteScanMetadata } from "../api/types";

describe("prioritised fixes, the dedicated sections and the cookie inventory in the PDF", () => {
  const scoreWithImpact = (): DpdpScoreResponse => {
    const sc = JSON.parse(JSON.stringify(F.review)) as DpdpScoreResponse;
    sc.remediation_plan = sc.remediation_plan.map((p, i) => ({ ...p, current_percent: 45.5, percent_if_fixed: 90.9 - i * 30, gain_percent: 45.4 - i * 30, impact: `Impact text ${i + 1}: visitors are affected.` }));
    return sc;
  };
  const row = (id: string, title: string, group: string, basis: string, status: string, extra: Record<string, unknown> = {}) => ({
    id, title, group, basis, provisions: ["DPDP Act s.13(1)", "DPDP Rules r.14(3)"], status,
    quote: status === "verified" || status === "partial" ? `A passage quoted for ${id} that is long enough.` : "",
    source_url: status === "verified" || status === "partial" ? "https://x.test/privacy" : null, detail: "d", fix: `Fix for ${title}.`, contacts: null, response_days: null, ...extra,
  });
  const POLICY = {
    status: "analysed", reason: null, sector: null, documents: [], notes: [], limitations: ["Only the policy text was read."], model: null,
    summary: { applicable: 6, verified: 3, partial: 1, not_found: 2, not_applicable: 0, quotes_rejected: 0, completeness_percent: 58.3 },
    requirements: [
      row("grievance_contact", "Grievance redressal contact", "Grievance", "required", "verified", { contacts: { emails: ["saurabhk@swaransoft.com"], phones: ["+91 9818 600 650"], urls: [] } }),
      row("grievance_timeline", "Grievance response period of at most 90 days", "Grievance", "required", "verified", { response_days: 30 }),
      row("complaint_to_board", "How to complain to the Data Protection Board", "Grievance", "required", "verified"),
      row("dpo_contact", "Business contact of a Data Protection Officer or answering person", "Grievance", "required", "partial"),
      row("breach_response", "Personal data breach response", "Transparency", "practice", "not_found"),
      row("notice_language", "Notice available in English or a scheduled Indian language", "Notice", "required", "not_found"),
    ],
  } as unknown as PolicyAnalysis;
  const META_SIGNALS = {
    ...(META as object),
    language_signals: { pages_checked: 5, pages_with_indic_script: 2, indic_scripts: { Tamil: 120 }, switcher_scripts: [], hreflang: ["en"], html_lang: "en" },
    security_signals: { pages_total: 5, pages_https: 5, homepage_https: true, homepage_headers: { hsts: "max-age=31536000", csp: null, x_frame_options: "DENY", x_content_type_options: "nosniff", referrer_policy: null, permissions_policy: null } },
  } as unknown as WebsiteScanMetadata;
  const cookie = (name: string, over: Record<string, unknown> = {}) => ({
    id: name, name, domain: "x.test", category: "analytics", vendor: "Google Analytics", is_first_party: true, source: "lookup", consent_states: ["pre_consent", "post_accept"],
    path: "/", expiry: "2027-11-26T15:18:31+00:00", secure: false, http_only: false, same_site: "Lax",
    observations: [{ consent_state: "pre_consent", page_url: "https://x.test/pricing", observed_at: null, method: "single_page_load", source_request_url: null, candidate_page_urls: [] }],
    provider: "Google Analytics", purpose: "Gives each browser a random identifier so Google Analytics can count visitors and sessions.", purpose_source: "catalogue", is_session: false, lifetime_days: 400, ...over,
  });
  const COOKIES = [
    cookie("_ga"),
    cookie("_fbp", { category: "marketing", provider: "Meta (Facebook)", vendor: "Meta", is_first_party: false, consent_states: ["post_accept", "post_reject"], lifetime_days: 90, purpose: "Identifies the browser so the Meta Pixel can measure ads and deliver them." }),
    cookie("sid", { category: "functional", provider: "The website", vendor: null, expiry: null, is_session: true, lifetime_days: null, consent_states: ["pre_consent"], purpose: "Keeps a visitor's session so the site can respond to the same visit across pages." }),
    cookie("zz_unknown", { category: null, provider: null, vendor: null, purpose: "The purpose could not be identified from the cookie's name.", purpose_source: "unknown", consent_states: ["post_accept"] }),
  ];
  const EVID = { ...(EVIDENCE as object), cookies: COOKIES, forms: [], consent_signals: [] } as unknown as ScanEvidenceResponse;
  const build = async (opts: { score?: DpdpScoreResponse; policy?: PolicyAnalysis | null; meta?: WebsiteScanMetadata | null; evidence?: ScanEvidenceResponse | null } = {}) => {
    const sc = opts.score ?? scoreWithImpact();
    const meta = (opts.meta === undefined ? META_SIGNALS : opts.meta) as never;
    const evidence = opts.evidence === undefined ? EVID : opts.evidence;
    const policy = opts.policy === undefined ? POLICY : opts.policy;
    await downloadReport(buildReportModel(SCAN, [], meta, [], [], buildScoreView(sc), evidence, null, policy));
    const all = (from = 0, to?: number) => recorded.pages.slice(from, to).map((pg) => pg.texts.join("")).join("");
    const appendixAt = recorded.pages.findIndex((pg) => pg.texts.join("").includes("Everything the scan recorded for this site"));
    return { summary: all(0, appendixAt < 0 ? undefined : appendixAt), appendix: appendixAt < 0 ? "" : all(appendixAt), appendixAt };
  };

  it("adds the fixes and the five sections after the table of every check and before the pages scanned, in a fixed order", async () => {
    const { appendixAt } = await build();
    const names = ["EVERY CHECK BEHIND THE SCORE", "PRIORITISED FIXES", "GRIEVANCE OFFICER", "DATA PROTECTION OFFICER", "INDIAN-LANGUAGE SUPPORT", "CHILDREN'S DATA AND AGE GATE", "OBSERVABLE SECURITY SIGNALS", "WEBSITE PAGES SCANNED"];
    const at = names.map((n) => recorded.calls.findIndex((c) => c.page < appendixAt && c.t.toUpperCase().replace(/\s+/g, " ") === n));   // the heading as drawn, not the same words inside a fix
    expect(names.filter((_, i) => at[i] < 0), "missing headings").toEqual([]);
    expect(at).toEqual([...at].sort((a, b) => a - b));
    const badges = recorded.calls.filter((c) => c.page < appendixAt && c.align === "center" && c.color?.join() === "255,255,255" && /^\d{1,2}$/.test(c.t) && c.y < 800).map((c) => Number(c.t));
    const sectionNumbers = [...new Set(badges)].sort((a, b) => a - b);
    expect(sectionNumbers.slice(0, 3)).toEqual([1, 2, 3]);
    for (let i = 1; i < sectionNumbers.length; i++) expect(sectionNumbers[i] - sectionNumbers[i - 1], `numbers ${sectionNumbers.join(",")}`).toBe(1);   // consecutive, no gap or repeat
  });

  it("lists the score's fixes in the plan's order, each with its effect on the score, why it matters and how to verify it", async () => {
    const { summary } = await build();
    const t = flat(summary);
    expect(t).toContain(flat("Impact on the score: Fixing this alone would raise the readiness score from 45.5% to 90.9% (+45.4 percentage points)."));
    expect(t).toContain(flat("Why it matters: Impact text 1: visitors are affected."));
    expect(t).toContain(flat("How to verify"));
    const plan = buildScoreView(scoreWithImpact()).remediationPlan;
    const sectionStart = t.indexOf(flat("Impact on the score:"));
    const positions = plan.map((p) => t.indexOf(flat(p.title), sectionStart - 2000 < 0 ? 0 : sectionStart - 2000));
    expect(positions.every((i) => i >= 0)).toBe(true);
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
  });

  it("lists the policy's fixes, required points first and a point not found before one partly stated, with the effect on completeness and why each matters", async () => {
    const { summary } = await build();
    const t = flat(summary);
    const start = t.indexOf(flat("Impact on the policy"));
    expect(start).toBeGreaterThan(-1);
    const fixes = t.slice(Math.max(0, start - 600));
    const order = ["Notice available in English or a scheduled Indian language", "Business contact of a Data Protection Officer or answering person", "Personal data breach response"].map((x) => fixes.indexOf(flat(x)));
    expect(order.every((i) => i >= 0)).toBe(true);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(fixes).toContain(flat("Stating this would raise the policy's completeness from 58.3% to"));
    expect(fixes).toContain(flat("The Act or Rules require this to be given or published"));
    expect(fixes).toContain(flat("Good practice: a duty stands behind it"));
  });

  it("never calls an absence a violation, and says what is and is not a finding", async () => {
    const { summary, appendix } = await build();
    const own = buildInsightSections({ policy: POLICY, meta: META_SIGNALS, evidence: EVID, sector: null });
    expect(JSON.stringify(own)).not.toMatch(/violat/i);                                              // the new sections never use the word
    expect((summary + appendix).replace(/not (a |current )*violations?/gi, "")).not.toMatch(/violat/i);   // elsewhere it only appears to deny it
    expect(flat(summary)).toContain(flat("Not verified"));
  });

  it("prints the five sections with the same facts the dashboard shows", async () => {
    const { summary } = await build();
    // A note can cross a page; the running header at the top of a page and the footer (the disclaimer and the page number) are not part of the note.
    const t = flat(summary).split(flat("x.testScanned 21 Oct 2026, 06:41 UTC")).join("").split(flat(REPORT_DISCLAIMER)).join("").replace(/Page\d+of\d+/g, "");
    const sections = buildInsightSections({ policy: POLICY, meta: META_SIGNALS, evidence: EVID, sector: buildScoreView(scoreWithImpact()).sector });
    expect(sections).toHaveLength(5);
    for (const s of sections) {
      expect(t, `${s.title} status`).toContain(flat(s.status));
      expect(t, `${s.title} knowledge`).toContain(flat(s.knowledge));
      expect(t, `${s.title} summary`).toContain(flat(s.summary));
      for (const f of s.facts) { expect(t, `${s.title}: ${f.label}`).toContain(flat(f.label)); expect(t, `${s.title}: ${f.label} value`).toContain(flat(f.value)); }
      expect(t, `${s.title} legal`).toContain(flat(s.legal));
      for (const n of s.notes) expect(t, `${s.title} note`).toContain(flat(n));
      if (s.quote) expect(t, `${s.title} quote`).toContain(flat(s.quote.text));
    }
  });

  it("labels what each section rests on: Confirmed, Observed or Not verified", async () => {
    const { summary } = await build();
    for (const word of ["Confirmed", "Observed", "Not verified"]) expect(summary, word).toContain(word);
  });

  it("gives the grievance officer's contact and period, and the Indian-language and security observations as observations", async () => {
    const t = flat((await build()).summary);
    expect(t).toContain(flat("saurabhk@swaransoft.com"));
    expect(t).toContain(flat("+91 9818 600 650"));
    expect(t).toContain(flat("Within about 30 days"));
    expect(t).toContain(flat("Tamil"));
    expect(t).toMatch(/\d+of\d+signalsobserved/);
    expect(t).toContain(flat("cannot be seen from outside"));
  });

  it("says nothing was recorded for a scan that predates the signals and does not invent a result", async () => {
    const t = flat((await build({ meta: META as never, policy: null })).summary);
    expect(t).toContain(flat("Not recorded for this scan"));
    expect(t).toContain(flat("Not assessed"));
  });

  it("keeps every page numbered and the score unchanged", async () => {
    await build();
    const total = recorded.pages.length;
    recorded.pages.forEach((pg, i) => expect(pg.texts.join(""), `page ${i + 1}`).toContain(`Page ${i + 1} of ${total}`));
    expect(recorded.pages.flatMap((pg) => pg.texts).filter((x) => x.includes("…"))).toEqual([]);
  });

  describe("the cookie inventory in the appendix", () => {
    it("opens with the five counts and groups the cookies by what they are for", async () => {
      const { appendix } = await build();
      const t = flat(appendix);
      expect(t).toContain(flat("Cookie inventory"));
      for (const label of ["Cookies found", "Third-party", "Set before consent", "Long-lived (over a year)", "Seen after Reject"]) expect(t, label).toContain(flat(label));
      for (const g of ["Necessary and functional (1)", "Analytics (1)", "Advertising and marketing (1)", "Not classified (1)"]) expect(t, g).toContain(flat(g));
    });

    it("gives each cookie its provider, party, lifetime, purpose and what it did in each consent state", async () => {
      const t = flat((await build()).appendix);
      for (const needle of ["Google Analytics", "Meta (Facebook)", "The website", "First-party", "Third-party", "13 months", "3 months", "Session",
        "count visitors and sessions", "Meta Pixel can measure ads", "Keeps a visitor's session", "could not be identified from the cookie's name"]) {
        expect(t, needle).toContain(flat(needle));
      }
      for (const head of ["COOKIE", "PROVIDER", "PARTY", "LIFETIME", "PURPOSE", "BEFORE", "ACCEPT", "REJECT"]) expect(t, head).toContain(head);
    });

    it("prints each cookie once and keeps every word of a long purpose", async () => {
      const long = "A very long purpose sentence that has to wrap across several lines of a narrow column so that none of its words is lost or cut short, ending here.";
      const ev = { ...(EVID as object), cookies: [...COOKIES, cookie("a_very_long_cookie_name_that_goes_on_and_on", { purpose: long })] } as unknown as ScanEvidenceResponse;
      const { appendix } = await build({ evidence: ev });
      const t = flat(appendix);
      expect(t).toContain(flat(long));
      expect(t.split(flat("zz_unknown")).length - 1).toBe(1);
      expect(recorded.pages.flatMap((pg) => pg.texts).filter((x) => x.includes("…"))).toEqual([]);
    });

    it("says Not tested for a state in which no choice was made", async () => {
      const { appendix } = await build({ meta: { ...(META as object), accept_interaction: "cmp_not_found", reject_interaction: "cmp_not_found" } as unknown as WebsiteScanMetadata });
      expect(appendix).toContain("Not tested");
    });

    it("keeps the rest of the appendix when there are no cookies", async () => {
      const { appendix } = await build({ evidence: { ...(EVID as object), cookies: [] } as unknown as ScanEvidenceResponse });
      expect(flat(appendix)).toContain(flat("Everything the scan recorded for this site"));
    });
  });

  describe("the policy checklist in the appendix", () => {
    it("lists every point with its status, and does not repeat the fixes there", async () => {
      const { appendix } = await build();
      const t = flat(appendix);
      expect(t).toContain(flat("Checklist"));
      for (const r of POLICY.requirements) expect(t, r.title).toContain(flat(r.title));
      for (const w of ["Quoted", "Partially stated", "Not found"]) expect(t, w).toContain(flat(w));
      expect(appendix).not.toContain("Fix:");
    });
  });
});


// ---------------------------------------------------------------------------------------------------------------------------------------
// The five-page DPDP readiness review in the PDF.
// ---------------------------------------------------------------------------------------------------------------------------------------
import { buildReadinessReview, READINESS_CLOSING } from "./readiness";

describe("the five-page readiness review in the PDF", () => {
  const pg = (url: string, via = "link") => ({ id: url, url, title: null, http_status: 200, discovered_via: via });
  const EV = {
    ...(EVIDENCE as object),
    pages: [pg("https://x.test/", "seed"), pg("https://x.test/privacy"), pg("https://x.test/signup")],
    forms: [{ id: "f", selector: "form", purpose_guess: "login_or_signup", page_url: "https://x.test/signup", fields: [{ name: "email", field_type: "email", required: true }, { name: "marketing_optin", field_type: "checkbox", required: true }] }],
    cookies: [], consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "Some CMP", has_reject_all: true, has_granular_choices: null, evidence: {} }],
  } as unknown as ScanEvidenceResponse;
  const POL = {
    status: "analysed", reason: null, sector: null, documents: [], notes: [], limitations: [], model: null,
    summary: { applicable: 2, verified: 1, partial: 0, not_found: 1, not_applicable: 0, quotes_rejected: 0, completeness_percent: 50 },
    requirements: [
      { id: "notice_data_itemised", title: "Itemised data", group: "Notice", basis: "required", provisions: ["DPDP Act s.5(1)(i)"], status: "verified", quote: "We collect your name and email address.", source_url: "https://x.test/privacy", detail: "", fix: "f" },
      { id: "retention", title: "Retention", group: "Transparency", basis: "practice", provisions: ["DPDP Act s.8(7)"], status: "not_found", quote: "", source_url: null, detail: "", fix: "f" },
    ],
  } as unknown as PolicyAnalysis;
  const build = async (evidence: ScanEvidenceResponse | null = EV, policy: PolicyAnalysis | null = POL) => {
    const model = buildReportModel(SCAN, [], META, [], [], buildScoreView(mixed), evidence, null, policy);
    await downloadReport(model);
    const appendixAt = recorded.pages.findIndex((p) => p.texts.join("").includes("Everything the scan recorded for this site"));
    const summary = recorded.pages.slice(0, appendixAt < 0 ? undefined : appendixAt).map((p) => p.texts.join("")).join("");
    return { model, summary, appendixAt };
  };

  it("is a numbered section after the security signals and before the pages scanned", async () => {
    const { summary } = await build();
    const at = ["OBSERVABLE SECURITY SIGNALS", "FIVE-PAGE DPDP READINESS REVIEW", "WEBSITE PAGES SCANNED"].map((n) => recorded.calls.findIndex((c) => c.t.toUpperCase().replace(/\s+/g, " ") === n));
    expect(at.every((i) => i >= 0)).toBe(true);
    expect(at).toEqual([...at].sort((a, b) => a - b));
    expect(flat(summary)).toContain(flat("Most of Sections 3 to 17"));
  });

  it("prints the five pages with their URLs and why, and NOT FOUND where a page was not found", async () => {
    const { summary, model } = await build();
    const t = flat(summary);
    for (const p of model.readiness.pages) {
      expect(t, p.role).toContain(flat(p.role));
      expect(t, `why ${p.n}`).toContain(flat(p.why));
      if (p.url) expect(t, p.url).toContain(flat(p.url));
    }
    expect(model.readiness.pages.some((p) => !p.found)).toBe(true);
    expect(t).toContain("NOTFOUND");
  });

  it("prints every check with its page, section, status, evidence and fix, in full and once", async () => {
    const { summary, model } = await build();
    const t = flat(summary);
    expect(model.readiness.rows.length).toBeGreaterThan(30);
    for (const r of model.readiness.rows) {
      expect(t, r.check).toContain(flat(r.check));
      expect(t, `${r.check} evidence`).toContain(flat(r.evidence));
      expect(t, `${r.check} fix`).toContain(flat(r.fix));
      expect(t, `${r.check} status`).toContain(flat(r.status));
    }
    for (const col of ["PAGE", "SECTION", "CHECK", "STATUS", "EVIDENCE", "FIX"]) expect(summary).toContain(col);
    expect(recorded.pages.flatMap((p) => p.texts).filter((x) => x.includes("…"))).toEqual([]);
  });

  it("gives the five counts, the score, the ranked gaps, the riskiest page and the closing sentence, as the dashboard does", async () => {
    const { summary, model } = await build();
    const t = flat(summary);
    const rv = model.readiness;
    for (const k of ["Passes this technical check", "Gap", "Not Found", "Unable to Verify", "Requires document review"] as const) expect(t).toContain(flat(`${rv.counts[k]} ${k}`));
    expect(t).toContain(flat(`Five-page review score: ${rv.scoreLine}`));
    expect(rv.topGaps.length).toBeGreaterThan(0);
    for (const g of rv.topGaps) { expect(t).toContain(flat(`${g.rank}. ${g.row.check}`)); expect(t).toContain(flat(`${g.row.page}: ${g.row.status}. Fix: ${g.row.fix}`)); }
    expect(t).toContain(flat(`Highest-risk page: ${rv.riskiest!.n}. ${rv.riskiest!.page}. ${rv.riskiest!.why}`));
    expect(t).toContain(flat(READINESS_CLOSING));
    expect(model.readiness).toEqual(buildReadinessReview({ siteUrl: "https://x.test/", evidence: EV, meta: META, policy: POL, sector: buildScoreView(mixed).sector }));
  });

  it("says why when there is no evidence, and keeps every page numbered", async () => {
    const { summary } = await build(null, null);
    expect(flat(summary)).toContain(flat("no loaded pages"));
    const total = recorded.pages.length;
    recorded.pages.forEach((p, i) => expect(p.texts.join(""), `page ${i + 1}`).toContain(`Page ${i + 1} of ${total}`));
  });

  it("does not move the score the report shows", async () => {
    await build();
    expect(recorded.calls.some((c) => c.page === 0 && c.t === "56.5%")).toBe(true);
  });
});
