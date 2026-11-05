import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TenChecksPanel } from "../components/TenChecksPanel";
import { buildHeroView } from "./hero";
import { fromApiAssessment, scoreContext } from "./tenChecks";

afterEach(cleanup);

/** A Quick Scan result as the server serves it (field names as on the wire). */
const wire = (over: Record<string, unknown> = {}) => ({
  engine: "scanner", scan_id: "s", methodology_version: "scanner-2.0.0", scan_status: "completed", policy_analysis: "done", final: true,
  score: 20, score_text: "20/100", total_text: "16 / 57", awarded: 16, applicable_max: 57, incomplete: 0, limited_basis: false, summary: "20/100 .", formula: "x",
  counts: { passed: 3, partial: 0, failed: 7, not_assessed: 0, not_applicable: 3 }, recommendations: [], pages: { discovered: 1, scanned: 1, failed: 0, skipped: 0 }, limitations: [],
  checks: [{ id: 1, ref: "A1", name: "A1 Privacy notice published and reachable", kind: "dpdp", result: "Failed", awarded: 0, max: 8, marks_text: "0 / 8", evidence: "Searched 1 page", source_url: "https://x.test/",
    explanation: "No privacy notice was found.", legal: "DPDP Act s.5(1); Rules r.3", completed: true, limitation: null, basis: "Not found in scanned pages", quality: "None", coverage: null }],
  scanner: {
    version: "2.0.0", mode: "quick", status: "completed", score: 20, band: "Needs attention", band_notes: ["For a Quick Scan the band describes what it can see."], coverage: 100,
    confidence: { level: "low", label: "Low", reasons: ["Only one page could be read."] }, explanation: "The score is the share of the assessable points earned.", disclaimer: "An observation of a public website on the date shown.",
    what_was_not_assessed: ["Records of processing are not visible from the public site."],
    purposes: { tested: true, reason: null, requests_only: true, purposes: [
      { kind: "analytics", label: "Analytics cookies", observed: [{ vendor: "Google Analytics", category: "analytics", host: "g.test" }], verdict: "as_expected" },
      { kind: "marketing", label: "Marketing cookies", observed: [{ vendor: "Meta Pixel", category: "marketing", host: "p.test" }, { vendor: null, category: "analytics", host: "a.test" }], verdict: "unexpected" }] },
    categories: [{ category: "Cookie Consent", marks: 48, awarded: 40, assessable: 44 }, { category: "Privacy Notice", marks: 36, awarded: 36, assessable: 36 }],
    rows: [{ id: "P3", name: "No non-essential cookies or trackers before a choice", outcome: "pass", sub_checks: [{ id: "P3.1", name: "No non-essential cookies before a choice", result: "met" }, { id: "P3.3", name: "No tracking identifiers in browser storage", result: "not recorded" }] }],
    indicators: [{ id: "I1", name: "Retention and erasure stated", scored: false, outcome: "Not detected", found: "Not found in the notice text read: retention and erasure stated.", evidence: [{ observed: "Searched the notice text read from https://x.test/privacy for it.", page: "https://x.test/privacy", at: null }],
      statutory: [{ provision: "DPDP Act s.8(7)", summary: "erase", relation: "related context", commences: "2027-05-13", in_force: false }] }], beyond_quick_scan: [{ ref: "B5", name: "Consent can be withdrawn", points: 3 }], removed: [], findings: [], rules: {}, scanned_at: null,
    ...over,
  },
});

describe("the Scanner Score on the dashboard and in the reports", () => {
  it("keeps the band, coverage and evidence confidence together with the score", () => {
    const a = fromApiAssessment(wire());
    const ctx = scoreContext(a)!;
    expect(ctx).toMatchObject({ band: "Needs attention", coverage: 100, confidence: "Low" });
    expect(ctx.text).toContain("Band: Needs attention");
    expect(ctx.text).not.toContain("Ceiling");
    expect(ctx.text).toContain("Coverage 100%");
    expect(ctx.text).toContain("Evidence confidence: Low");
  });

  it("takes the band from the server and never works one out in the browser", () => {
    const hero = buildHeroView(fromApiAssessment(wire()));
    expect(hero.band).toBe("Needs attention");
    expect(hero.percentLabel).toBe("20/100");
    expect(hero.headline.startsWith("The score is the share of the assessable points earned.")).toBe(true);      // the explanation comes before the result
    expect(hero.headline).not.toMatch(/ceiling|\/70/i);
    const insufficient = buildHeroView(fromApiAssessment(wire({ band: "Insufficient evidence", score: 20 })));
    expect(insufficient.band).toBe("Insufficient evidence");
    expect(insufficient.tone).toBe("mute");
  });

  it("shows the whole-number score, never a decimal", () => {
    expect(fromApiAssessment(wire()).scoreText).toBe("20/100");
    expect(buildHeroView(fromApiAssessment(wire())).percentLabel).not.toMatch(/\./);
  });

  it("has no scanner context under the ten-check rollback", () => {
    const a = fromApiAssessment({ ...wire(), engine: "ten_check", scanner: null });
    expect(scoreContext(a)).toBeNull();
  });

  it("explains the calculation, lists what a Quick Scan cannot reach, and states what was not assessed", () => {
    render(<TenChecksPanel ten={fromApiAssessment(wire())} />);
    expect(screen.getByTestId("scanner-explanation")).toHaveTextContent("The score is the share of the assessable points earned.");
    expect(screen.getByTestId("scanner-band")).toHaveTextContent("Needs attention");
    expect(screen.queryByTestId("scanner-ceiling")).toBeNull();
    expect(screen.getByTestId("scanner-coverage")).toHaveTextContent("Coverage 100%");
    expect(screen.getByTestId("scanner-confidence")).toHaveTextContent("Evidence confidence: Low");
    expect(screen.getByTestId("scanner-beyond")).toHaveTextContent("B5 Consent can be withdrawn");
    expect(screen.getByTestId("scanner-not-assessed")).toHaveTextContent("Records of processing");
    expect(screen.getByTestId("scanner-disclaimer")).toBeInTheDocument();
    expect(screen.getByTestId("ten-check-1")).toHaveTextContent("A1");
  });

  it("lists the readiness indicators apart from the score, with the provisions and when they start", () => {
    render(<TenChecksPanel ten={fromApiAssessment(wire())} />);
    const row = screen.getByTestId("indicator-I1");
    expect(row).toHaveTextContent("Retention and erasure stated");
    expect(row).toHaveTextContent("Not detected");
    expect(row).toHaveTextContent("DPDP Act s.8(7) (from 2027-05-13)");
    expect(screen.getByTestId("scanner-indicators")).toHaveTextContent("never change the score");
  });

  it("shows each purpose that was switched on alone, what it contacted, and whether that was expected", () => {
    render(<TenChecksPanel ten={fromApiAssessment(wire())} />);
    expect(screen.getByTestId("purpose-analytics")).toHaveTextContent("Google Analytics (analytics)");
    expect(screen.getByTestId("purpose-analytics")).toHaveTextContent("As expected");
    expect(screen.getByTestId("purpose-marketing")).toHaveTextContent("Meta Pixel (marketing), a.test (analytics)");
    expect(screen.getByTestId("purpose-marketing")).toHaveTextContent("Unexpected");
    expect(screen.getByTestId("scanner-purposes")).toHaveTextContent("cookies were not read");
  });

  it("shows the scorecard's categories and each row's detailed checks, including what was not recorded", () => {
    render(<TenChecksPanel ten={fromApiAssessment(wire())} />);
    expect(screen.getByTestId("scorecard-categories")).toHaveTextContent("Cookie Consent: 40 of 44 marks");
    expect(screen.getByTestId("scorecard-categories")).toHaveTextContent("Privacy Notice: 36 of 36 marks");
    const d = screen.getByTestId("details-P3");
    expect(d).toHaveTextContent("P3.1 No non-essential cookies before a choice: met");
    expect(d).toHaveTextContent("P3.3 No tracking identifiers in browser storage: not recorded");
  });
});
