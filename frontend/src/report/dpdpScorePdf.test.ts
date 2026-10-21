import { render } from "@testing-library/react";
import { createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Same recording stand-in for jsPDF as downloadReport.test.ts: what is drawn, on which page.
interface RecordedPage { orientation: "portrait" | "landscape"; texts: string[] }
const recorded: { pages: RecordedPage[] } = { pages: [] };

vi.mock("jspdf", () => {
  class FakeDoc {
    constructor() { recorded.pages = [{ orientation: "portrait", texts: [] }]; }
    private get page() { return recorded.pages[recorded.pages.length - 1]; }
    setFont() { return this; }
    setFontSize() { return this; }
    setTextColor() { return this; }
    setDrawColor() { return this; }
    setFillColor() { return this; }
    setLineWidth() { return this; }
    line() { return this; }
    roundedRect() { return this; }
    rect() { return this; }
    circle() { return this; }
    getTextWidth(t: string) { return t.length * 4.5; }
    splitTextToSize(text: string, width: number): string[] {
      const per = Math.max(10, Math.floor(width / 4.6));
      const lines: string[] = [];
      for (let i = 0; i < text.length; i += per) lines.push(text.slice(i, i + per));
      return lines.length ? lines : [""];
    }
    text(t: string | string[]) { this.page.texts.push(...(Array.isArray(t) ? t : [t])); return this; }
    addPage(_f: string, orientation: "portrait" | "landscape") { recorded.pages.push({ orientation, texts: [] }); return this; }
    save() {}
  }
  return { jsPDF: FakeDoc };
});

import type { DpdpScoreResponse } from "../api/types";
import { DpdpScoreSection } from "../components/DpdpScoreSection";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildReportModel } from "./buildReport";
import { buildScoreView } from "./dpdpScore";
import { downloadReport } from "./downloadReport";

const mixed = fixtures.mixed as unknown as DpdpScoreResponse;
const insufficient = fixtures.insufficient as unknown as DpdpScoreResponse;
const review = fixtures.review as unknown as DpdpScoreResponse;
const custom = fixtures.custom as unknown as DpdpScoreResponse;
const thin = fixtures.thin as unknown as DpdpScoreResponse;
const confirmed = fixtures.confirmed as unknown as DpdpScoreResponse;

const META = {
  consent_mechanism: "cmp", cmp_vendor: null, accept_interaction: "clicked", reject_interaction: "clicked",
  pages_scanned: 1, trackers_by_consent_state: { pre_consent: 1 },
} as never;
const SCAN = { url: "https://x.test/", evidence_counts: { pages: 1, cookies: 0, trackers: 1, third_party_services: 0, policies: 1, forms: 0 } } as never;

async function pdf(score: DpdpScoreResponse | null) {
  await downloadReport(buildReportModel(SCAN, [], META, [], [], score ? buildScoreView(score) : null));
  return {
    page: (i: number) => recorded.pages[i].texts.join(""),
    // Text drawn by the page furniture (footer and continuation header) is removed, so a line that straddles a page break reads whole.
    all: () => recorded.pages.map((p) => p.texts.join("")).join("")
      .replace(/DPDP Compliance Score, https:\/\/x\.test\/\. Methodology[^]*?Page \d+\./g, "")
      .replace(/DPDP Compliance Score \(continued\)https:\/\/x\.test\/ · proposed methodology/g, ""),
  };
}

beforeEach(() => { recorded.pages = []; });

describe("the PDF carries the dashboard's DPDP score, unchanged", () => {
  it("puts the headline and coverage on the summary page and the detail on its own pages before the cookie inventory", async () => {
    const p = await pdf(mixed);

    const orientations = recorded.pages.map((x) => x.orientation);
    expect(orientations[0]).toBe("portrait");
    expect(orientations[orientations.length - 1]).toBe("landscape");
    expect(orientations.filter((o) => o === "portrait").length).toBeGreaterThanOrEqual(2);
    expect(p.page(0)).toContain("DPDP Compliance Score (proposed methodology): 56.5%");
    expect(p.page(0)).toContain("Assessment coverage 62.2%");
    expect(p.page(1)).toContain("DPDP Compliance Score");
    expect(p.page(recorded.pages.length - 1)).toContain("Cookie inventory");
  });

  it("prints exactly the strings the dashboard shows", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: mixed }));
    const v = buildScoreView(mixed);
    const p = await pdf(mixed);
    const dashboard = container.textContent ?? "";

    for (const s of [v.headline, v.coverageLine, v.basisLine, v.inForceLine, v.formulaLine, v.unableLine!, v.methodologyLine, v.disclaimer]) {
      expect(dashboard).toContain(s);
      expect(p.all()).toContain(s);
    }
  });

  it("lists every requirement the dashboard lists, with the same status, weight and provision", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: mixed }));
    const p = await pdf(mixed);

    for (const r of mixed.requirements) {
      const row = container.querySelector(`[data-testid="dpdp-req-${r.id}"]`);
      expect(row, r.id).not.toBeNull();
      const v = buildScoreView(mixed);
      const view = [...v.assessed, ...v.unableToVerify, ...v.excluded].find((x) => x.id === r.id)!;
      for (const s of [r.id, view.status, view.provisions, `weight ${view.weight}`, view.points]) {
        expect(row!.textContent, `${r.id} dashboard`).toContain(s);
        expect(p.all(), `${r.id} pdf`).toContain(s);
      }
    }
  });

  it("prints every category score the dashboard shows", async () => {
    const p = await pdf(mixed);
    for (const c of buildScoreView(mixed).categories) {
      expect(p.all()).toContain(c.name);
      expect(p.all()).toContain(c.score);
    }
    expect(p.all()).toContain("47.1%");
  });

  it("prints Insufficient Evidence, and no percentage, when the score is withheld, on both surfaces", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: insufficient }));
    const p = await pdf(insufficient);

    expect(container.querySelector('[data-testid="dpdp-headline"]')!.textContent).toBe("Insufficient Evidence");
    expect(p.page(0)).toContain("DPDP Compliance Score (proposed methodology): Insufficient Evidence");
    expect(p.page(1)).toContain("Insufficient Evidence");
    expect(p.all()).toContain("A score is not shown because the evidence is not sufficient");
    expect(p.page(0)).not.toMatch(/Score \(proposed methodology\): \d/);
  });

  it("states the methodology version, that it is proposed, and what it does not assess", async () => {
    const p = await pdf(mixed);
    expect(p.all()).toContain("Methodology v1.5.0");
    expect(p.all()).toContain("not an officially prescribed legal formula");
    expect(p.all()).toContain("Rule 5, Second Schedule");
  });

  it("leaves the PDF exactly as before when no score was loaded", async () => {
    const p = await pdf(null);
    expect(recorded.pages.map((x) => x.orientation)).toEqual(["portrait", "landscape"]);
    expect(p.page(0)).toContain("DPDP Compliance Score: not available for this report");
    expect(p.all()).not.toContain("proposed methodology");
  });
});

describe("the Logic Required / Human Review box and the breakdown are the same on the dashboard and in the PDF", () => {
  it("never mentions subdomains, discovered domains or other websites", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: review }));
    const p = await pdf(review);
    for (const text of [container.textContent ?? "", p.all()]) {
      expect(text.toLowerCase()).not.toContain("subdomain");
      expect(text.toLowerCase()).not.toContain("discovered");
      expect(text.toLowerCase()).not.toContain("external website");
    }
  });

  it("states on both surfaces that a scan cannot show complete compliance", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: mixed }));
    const p = await pdf(mixed);
    expect(container.textContent).toContain("cannot establish complete DPDP compliance");
    expect(p.all()).toContain("cannot establish complete DPDP compliance");
  });

  it("is unchanged for an insufficient-evidence scan: still no number, still a review box", async () => {
    const p = await pdf(insufficient);
    expect(p.all().toUpperCase()).toContain("LOGIC REQUIRED / HUMAN REVIEW");
    expect(p.page(0)).toContain("Insufficient Evidence");
  });
});

describe("the PDF carries the same check records, gaps and remediation as the dashboard", () => {
  it("prints each requirement's definition, criteria, expected evidence, sufficiency and limitation", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: review }));
    const v = buildScoreView(review);
    const p = await pdf(review);
    for (const r of [...v.assessed, ...v.unableToVerify]) {
      for (const s of [`Checks: ${r.description}`, `Assessment criteria: ${r.criteria}`, `Evidence expected: ${r.expectedEvidence}`]) {
        expect(p.all(), `${r.id}: ${s.slice(0, 40)}`).toContain(s);
      }
      expect(container.querySelector(`[data-testid="dpdp-record-${r.id}"]`)!.textContent).toContain(r.criteria);
      if (r.sufficiency) expect(p.all()).toContain(r.sufficiency);
      if (r.limitation) expect(p.all()).toContain(r.limitation);
      if (r.timingNote) expect(p.all()).toContain(r.timingNote);
    }
  });

});

describe("the score replaces the overall risk level", () => {
  it("the first page banner is the DPDP Compliance Score, never an overall risk", async () => {
    const p = await pdf(mixed);
    expect(p.page(0)).toContain("DPDP Compliance Score (proposed methodology): 56.5%");
    expect(p.all()).not.toContain("Overall risk");
    expect(p.all()).not.toContain("highest severity");
  });

  it("shows Insufficient Evidence in the banner, not a risk level, when the score is withheld", async () => {
    const p = await pdf(insufficient);
    expect(p.page(0)).toContain("DPDP Compliance Score (proposed methodology): Insufficient Evidence");
    expect(p.all()).not.toContain("Overall risk");
  });

  it("prints the check type, applicability, confidence and rationale for every row, as the dashboard does", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: review }));
    const v = buildScoreView(review);
    const p = await pdf(review);
    expect(p.all()).toContain(v.checkTypesLine);
    for (const r of [...v.assessed, ...v.unableToVerify]) {
      for (const s of [`Applies when: ${r.applicability}`, `Confidence: ${r.confidence}`, `Scoring rationale: ${r.scoringRationale}`, r.checkType]) {
        // A long line can straddle a page break, where the page footer is drawn between its two halves: match its ends.
        expect(p.all(), `${r.id}: ${s.slice(0, 40)}`).toContain(s.slice(0, 40));
        expect(p.all(), `${r.id}: ...${s.slice(-30)}`).toContain(s.slice(-30));
      }
      const rec = container.querySelector(`[data-testid="dpdp-record-${r.id}"]`)!.textContent ?? "";
      expect(rec).toContain(r.applicability);
      expect(rec).toContain(r.scoringRationale);
    }
  });
});

describe("the four finding groups are the same on the dashboard and in the PDF", () => {
  // A long line can straddle a page break, where the page footer is drawn between its halves: match its ends.
  const has = (haystack: string, text: string, label: string) => {
    expect(haystack, `${label}: ${text.slice(0, 40)}`).toContain(text.slice(0, 40));
    expect(haystack, `${label}: ...${text.slice(-30)}`).toContain(text.slice(-30));
  };

  it("prints the four group headings with their counts, in order", async () => {
    const p = await pdf(review);
    const v = buildScoreView(review);
    const all = p.all();
    expect(all).toContain(v.groupCountsLine);
    const at = v.groups.map((g) => all.toUpperCase().indexOf(g.key === "needs_review" ? "LOGIC REQUIRED / HUMAN REVIEW" : `${g.title} (${g.count})`.toUpperCase()));
    expect(at.every((i) => i >= 0)).toBe(true);
    expect([...at].sort((a, b) => a - b)).toEqual(at);
  });

  it("prints every finding in every group with the detail the dashboard shows", async () => {
    for (const sc of [review, confirmed]) {
      const { container } = render(createElement(DpdpScoreSection, { score: sc }));
      const v = buildScoreView(sc);
      const p = await pdf(sc);
      const dashboard = container.textContent ?? "";
      for (const g of v.groups) {
        for (const f of g.entries) {
          const fields = [f.title, f.outcome, f.severity, f.legal && `Legal reference: ${f.legal}`,
            `Affected page: ${f.pages.length ? f.pages.join(", ") : "none recorded"} · Consent state: ${f.consentState}`,
            f.businessImpact && `Business impact: ${f.businessImpact}`, f.remediation && `Recommended remediation: ${f.remediation}`,
            f.howToVerify && `How to verify: ${f.howToVerify}`, f.explanation && `Why this group: ${f.explanation}`,
            f.whatToReview.length > 0 && `What must be reviewed: ${f.whatToReview.join(" ")}`, ...f.observed, ...f.missing, f.caveat].filter(Boolean) as string[];
          for (const text of fields) {
            has(p.all(), text, `${f.id} pdf`);
            has(dashboard, text, `${f.id} dashboard`);
          }
        }
      }
    }
  });

  it("prints the Needs Human Review box with the statement", async () => {
    const p = await pdf(review);
    expect(p.all().toUpperCase()).toContain("LOGIC REQUIRED / HUMAN REVIEW: NEEDS HUMAN REVIEW");
    has(p.all(), buildScoreView(review).reviewStatement, "statement");
  });

  it("prints the group notes: future duties are not current violations, and unverified is neither pass nor fail", async () => {
    const p = await pdf(review);
    expect(p.all()).toContain("Duties that start later. Not current violations.");
    expect(p.all()).toContain("The scan lacks the evidence. Neither a pass nor a failure.");
    expect(p.all()).toContain("Only duties that are in force today can be confirmed findings.");
  });

  it("prints the effective dates of the upcoming duties", async () => {
    const p = await pdf(review);
    for (const l of buildScoreView(review).upcomingLines) has(p.all(), l, "upcoming");
  });

  it("puts a gap in Confirmed Findings only once the duty applies, in both places", async () => {
    const v = buildScoreView(confirmed);
    expect(v.groups[0].entries.map((e) => e.id)).toContain("DPDP-C1");
    const p = await pdf(confirmed);
    expect(p.all()).toContain("Confirmed Findings (");
    expect(p.all()).toContain("Current: the duty is in force.");
    expect(p.all()).not.toContain("Not a current violation.\n");
  });

  it("never mentions subdomains or other websites in any group", async () => {
    const p = await pdf(review);
    expect(p.all().toLowerCase()).not.toContain("subdomain");
    expect(p.all().toLowerCase()).not.toContain("discovered");
  });
});

describe("the methodology, weights and explanation are the same on the dashboard and in the PDF", () => {
  const has = (haystack: string, text: string, label: string) => expect(haystack, `${label}: ${text.slice(0, 50)}`).toContain(text);

  it("prints the version, the formulas and rules, and the result explanation the dashboard shows", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: mixed }));
    const v = buildScoreView(mixed);
    const p = await pdf(mixed);
    const dashboard = container.textContent ?? "";
    for (const text of [v.methodology.versionLine, v.methodology.weightsNote, ...v.methodology.rules, ...v.explanationLines, ...v.methodology.dedup]) {
      has(p.all(), text, "pdf");
      has(dashboard, text, "dashboard");
    }
  });

  it("prints every category weight and every requirement weight with its rationale", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: mixed }));
    const v = buildScoreView(mixed);
    const p = await pdf(mixed);
    for (const c of v.methodology.categories) {
      has(p.all(), `${c.name}: weight ${c.weight} (${c.share} of the applicable weight). ${c.rationale}`, "category");
      expect(container.querySelector('[data-testid="dpdp-category-weights"]')!.textContent).toContain(c.rationale);
    }
    for (const w of v.methodology.weights) {
      has(p.all(), `${w.id}  ${w.title} · ${w.type} · weight ${w.weight} · ${w.applies}`, "weight");
      has(p.all(), `Why this weight: ${w.rationale}`, "rationale");
      has(p.all(), `Applies when: ${w.applicability}`, "applicability");
      expect(container.querySelector(`[data-testid="dpdp-weight-${w.id}"]`)!.textContent).toContain(w.rationale);
    }
  });

  it("shows the coverage as its own figure in the banner, apart from the score", async () => {
    const p = await pdf(mixed);
    expect(p.page(0)).toContain("DPDP Compliance Score (proposed methodology): 56.5%");
    expect(p.page(0)).toContain("Assessment coverage (separate from the score): 62.2%");
  });

  it("prints Insufficient Evidence with its coverage and the reason, never a score", async () => {
    const p = await pdf(insufficient);
    expect(p.page(0)).toContain("DPDP Compliance Score (proposed methodology): Insufficient Evidence");
    expect(p.page(0)).toContain("Assessment coverage (separate from the score): 0%");
    expect(p.all()).toContain("No score is shown: ");
  });

  it("declares custom weights in both places", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: custom }));
    const p = await pdf(custom);
    for (const text of ["CUSTOM weights", "DPDP-C1 = 8 (default 5)", "not comparable"]) {
      expect(p.all()).toContain(text);
      expect(container.textContent).toContain(text);
    }
    expect(p.all()).toContain("weight 8 (default 5)");
  });

  it("explains that a duplicate is scored once, in both places", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: mixed }));
    const p = await pdf(mixed);
    expect(p.all()).toContain("Each requirement is scored once");
    expect(container.textContent).toContain("Each requirement is scored once");
  });
});

describe("scope, versions and prioritised remediation are the same on the dashboard and in the PDF", () => {
  it("prints the scope, scan time, both versions, the nature note and the caution", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: review }));
    const v = buildScoreView(review);
    const p = await pdf(review);
    for (const text of [...v.scopeLines, v.scanTimeLine, v.versionsLine, v.natureNote, ...v.legalSources]) {
      expect(p.all(), text.slice(0, 50)).toContain(text);
      expect(container.textContent, text.slice(0, 50)).toContain(text);
    }
  });

  it("prints the caution for a thin score, on the first page and in the detail, as the dashboard does", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: thin }));
    const caution = buildScoreView(thin).caution!;
    const p = await pdf(thin);
    expect(p.page(0)).toContain(caution);
    expect(p.all()).toContain(caution);
    expect(container.textContent).toContain(caution);
  });

  it("puts the scope, scan time and versions on the first page too", async () => {
    const p = await pdf(review);
    expect(p.page(0)).toContain("Scope: the entered main domain (x.test) only.");
    expect(p.page(0)).toContain("Scan time: 21 Oct 2026, 06:41 UTC");
    expect(p.page(0)).toContain("Scoring methodology v1.5.0 · Legal mapping v2026.10.1");
  });

  it("prints every remediation step in the same order as the dashboard", async () => {
    const { container } = render(createElement(DpdpScoreSection, { score: confirmed }));
    const plan = buildScoreView(confirmed).remediationPlan;
    const p = await pdf(confirmed);
    let last = -1;
    for (const s of plan) {
      const at = p.all().indexOf(`${s.rank}. ${s.title} · ${s.priority}`);
      expect(at, `step ${s.rank}`).toBeGreaterThan(last);
      last = at;
      for (const text of [`Action: ${s.action}`, `How to verify: ${s.howToVerify}`, s.legal, `${s.outcome} · ${s.timing} · ${s.priorityReason}`]) {
        expect(p.all()).toContain(text);
      }
      expect(container.querySelector(`[data-testid="dpdp-step-${s.rank}"]`)!.textContent).toContain(s.action);
    }
  });

  it("states there is nothing to remediate when no check failed", async () => {
    const p = await pdf(buildScoreOf("clean"));
    expect(p.all().toUpperCase()).toContain("PRIORITIZED REMEDIATION (0)");
    expect(p.all()).toContain("No remediation actions");
  });

  it("makes no mention of other domains anywhere", async () => {
    const p = await pdf(confirmed);
    expect(p.all().toLowerCase()).not.toContain("subdomain");
    expect(p.all().toLowerCase()).not.toContain("discovered");
  });
});

function buildScoreOf(name: "clean"): DpdpScoreResponse {
  return (name === "clean" ? fixtures.clean_in_force : fixtures.mixed) as unknown as DpdpScoreResponse;
}
