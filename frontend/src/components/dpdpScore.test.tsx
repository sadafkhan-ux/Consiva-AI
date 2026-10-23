import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { DpdpScoreResponse } from "../api/types";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildScoreView, STATUS_LABEL } from "../report/dpdpScore";
import { DpdpScoreSection } from "./DpdpScoreSection";

// Real output of the backend calculation (backend/tests/test_dpdp_score.py builds these inputs), written by a script, so the
// frontend is tested against the single source of truth rather than a hand-made copy of it.
const mixed = fixtures.mixed as unknown as DpdpScoreResponse;
const insufficient = fixtures.insufficient as unknown as DpdpScoreResponse;
const review = fixtures.review as unknown as DpdpScoreResponse;
const custom = fixtures.custom as unknown as DpdpScoreResponse;
const thin = fixtures.thin as unknown as DpdpScoreResponse;
const confirmed = fixtures.confirmed as unknown as DpdpScoreResponse;
const clean = fixtures.clean_in_force as unknown as DpdpScoreResponse;

describe("buildScoreView: the one place the score becomes words", () => {
  it("shows the server's number, never a recalculated one", () => {
    const v = buildScoreView(mixed);
    expect(v.scored).toBe(true);
    expect(v.headline).toBe("56.5%");
    expect(v.coverageLine).toBe("Assessment coverage 62.2%: 23 of 37 weighted points of applicable requirements were assessed.");
    expect(v.formulaLine).toContain("weighted points earned (13) divided by weighted points assessed (23), times 100");
    expect(v.formulaLine).toContain("Met = 100%");
    expect(v.formulaLine).toContain("Partially Met = 50%");
  });

  it("says Insufficient Evidence, never a number, when the score is withheld", () => {
    const v = buildScoreView(insufficient);
    expect(v.scored).toBe(false);
    expect(v.headline).toBe("Insufficient Evidence");
    expect(v.headline).not.toMatch(/\d/);
    expect(v.withheldReason).toBeTruthy();
    expect(v.coverageLine).toContain("coverage 0%");
  });

  it("separates assessed, unable to verify and excluded requirements, and the three never overlap", () => {
    const v = buildScoreView(mixed);
    const ids = (rows: { id: string }[]) => rows.map((r) => r.id);
    expect(ids(v.assessed)).toEqual(["DPDP-C1", "DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-N1", "DPDP-N2"]);
    expect(ids(v.unableToVerify)).toEqual(["DPDP-R9", "DPDP-R14A", "DPDP-R14B", "DPDP-S6", "DPDP-S7"]);
    expect(ids(v.excluded)).toEqual(["DPDP-C5", "DPDP-X4", "DPDP-X8", "DPDP-X10", "DPDP-X13"]);
    expect(new Set([...ids(v.assessed), ...ids(v.unableToVerify), ...ids(v.excluded)]).size).toBe(mixed.requirements.length);
    expect(v.unableLine).toContain("5 applicable requirements (14 points) could not be verified");
    expect(v.unableLine).toContain("never counted as met");
  });

  it("traces every row to its legal provision and its scan evidence", () => {
    const c1 = buildScoreView(mixed).assessed.find((r) => r.id === "DPDP-C1")!;
    expect(c1.provisions).toBe("DPDP Act, 2023 s.6(1); DPDP Rules, 2025 r.3");
    expect(c1.statusKey).toBe("not_met");
    expect(c1.points).toBe("0 of 5 points");
    expect(c1.evidenceIds).toEqual(["t1"]);
    expect(c1.rules).toContain("R-001");
    expect(c1.observations[0]).toContain("fired before any consent interaction");
  });

  it("scores a partially met requirement as half its weight", () => {
    const n2 = buildScoreView(mixed).assessed.find((r) => r.id === "DPDP-N2")!;
    expect(n2.status).toBe("Partially Met");
    expect(n2.points).toBe("1 of 2 points");
  });

  it("explains a duplicate instead of penalising it twice", () => {
    const c5 = buildScoreView(mixed).excluded.find((r) => r.id === "DPDP-C5")!;
    expect(c5.status).toBe(STATUS_LABEL.not_applicable);
    expect(c5.points).toBe("not scored");
    expect(c5.note).toContain("DPDP-C4");
  });

  it("is honest that the obligations are not yet in force, with the date", () => {
    const v = buildScoreView(mixed);
    expect(v.basisLine).toContain("not yet in force");
    expect(v.basisLine).toContain("13 May 2027");
    expect(v.inForceLine).toContain("21 October 2026");
    expect(v.inForceLine).toContain("no score");
  });

  it("drops the readiness wording once the obligations are in force", () => {
    const v = buildScoreView(clean);
    expect(v.basisLine).toBe("Against obligations that are in force on the assessment date.");
    expect(v.inForceLine).toContain("100%");
  });

  it("reports category scores with the same formula", () => {
    const cats = Object.fromEntries(buildScoreView(mixed).categories.map((c) => [c.name, c]));
    expect(cats["Consent"].score).toBe("47.1%");
    expect(cats["Notice"].score).toBe("83.3%");
    expect(cats["Data principal rights"].score).toBe("Not assessed");
  });

  it("labels the method as proposed and versioned", () => {
    const v = buildScoreView(mixed);
    expect(v.title).toContain("proposed methodology");
    expect(v.methodologyLine).toBe("Methodology v1.5.0 · assessed as of 21 October 2026");
    expect(v.disclaimer).toContain("does not constitute legal advice");
  });
});

describe("DpdpScoreSection", () => {
  it("renders nothing when the score could not be loaded, rather than a made-up number", () => {
    const { container } = render(<DpdpScoreSection score={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the headline, coverage, basis and the methodology note", () => {
    render(<DpdpScoreSection score={mixed} />);
    expect(screen.getByTestId("dpdp-headline")).toHaveTextContent("56.5%");
    expect(screen.getByTestId("dpdp-coverage-figure")).toHaveTextContent("62.2%");
    // the card shows the final percentages only: no formula or working in view, and the arithmetic is behind a closed disclosure
    const card = screen.getByTestId("dpdp-score");
    const details = screen.getByTestId("dpdp-calculation-details") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(screen.queryByTestId("dpdp-coverage")).not.toBeInTheDocument();
    const methodology = screen.getByTestId("dpdp-methodology") as HTMLDetailsElement;
    expect(methodology.open).toBe(false);   // the methodology is collapsed
    // and no formula or arithmetic is printed anywhere in the card, collapsed sections included
    expect((card.textContent ?? "").match(/.{0,50}(Score = |Coverage = |÷|×|divided by|times 100).{0,50}/)?.[0]).toBeUndefined();
    expect(screen.getByTestId("dpdp-basis")).toHaveTextContent("not yet in force");
    expect(screen.getByTestId("dpdp-inforce")).toHaveTextContent("no score");
    expect(screen.getByTestId("dpdp-disclaimer")).toHaveTextContent("for informational purposes only");
    expect(screen.queryByTestId("dpdp-withheld")).not.toBeInTheDocument();
  });

  it("shows Insufficient Evidence and why, with no percentage", () => {
    render(<DpdpScoreSection score={insufficient} />);
    expect(screen.getByTestId("dpdp-headline")).toHaveTextContent("Insufficient Evidence");
    expect(screen.getByTestId("dpdp-headline").textContent).not.toMatch(/%/);
    expect(screen.getByTestId("dpdp-withheld")).toHaveTextContent("never counted as compliance");
  });

  it("lists each assessed requirement with its status, provision and evidence", () => {
    render(<DpdpScoreSection score={mixed} />);
    const c1 = screen.getByTestId("dpdp-req-DPDP-C1");
    expect(within(c1).getByTestId("dpdp-status-DPDP-C1")).toHaveTextContent("Not Met");
    expect(c1).toHaveTextContent("DPDP Act, 2023 s.6(1)");
    expect(c1).toHaveTextContent("weight 5");
    expect(c1).toHaveTextContent("Related rules: R-001");
    expect(c1).toHaveTextContent("Evidence: t1");
    expect(within(screen.getByTestId("dpdp-req-DPDP-N2")).getByTestId("dpdp-status-DPDP-N2")).toHaveTextContent("Partially Met");
  });

  it("keeps unable-to-verify and excluded requirements out of the assessed list, in their own sections", () => {
    render(<DpdpScoreSection score={mixed} />);
    expect(screen.getByTestId("dpdp-unable")).toHaveTextContent("could not be verified from a website scan");
    // security and data-rights checks are not shown on the card (they stay in the score as not tested)
    expect(screen.queryByTestId("dpdp-req-DPDP-S6")).not.toBeInTheDocument();
    const excluded = screen.getByTestId("dpdp-excluded-list");
    expect(within(excluded).getByTestId("dpdp-req-DPDP-C5")).toHaveTextContent("Counted under DPDP-C4");
    expect(within(excluded).getByTestId("dpdp-req-DPDP-X10")).toHaveTextContent("excluded unless declared");
  });

  it("shows category scores", () => {
    render(<DpdpScoreSection score={mixed} />);
    const table = screen.getByTestId("dpdp-categories");
    expect(table).toHaveTextContent("Consent");
    expect(table).toHaveTextContent("47.1%");
    expect(table).not.toHaveTextContent("Data principal rights");
    expect(table).not.toHaveTextContent("Security and breach");
  });

  it("shows no data-rights or security section, check, finding or weight anywhere on the card, and says once why", () => {
    for (const key of ["mixed", "review", "confirmed", "thin"]) {
      const { container, unmount } = render(<DpdpScoreSection score={(fixtures as never as Record<string, typeof mixed>)[key]} />);
      const text = container.textContent ?? "";
      for (const word of ["Data principal rights", "Security and breach", "DPDP-R9", "DPDP-R14A", "DPDP-R14B", "DPDP-S6", "DPDP-S7", "GDPR", "CCPA"]) {
        expect(text, `${key}: ${word}`).not.toContain(word);
      }
      expect(screen.getByTestId("dpdp-omitted-note")).toHaveTextContent("cannot be observed from a website");
      unmount();
    }
  });

  it("keeps the score, the coverage and the status counts as they were", () => {
    render(<DpdpScoreSection score={mixed} />);
    expect(screen.getByTestId("dpdp-headline")).toHaveTextContent("56.5%");
    expect(screen.getByTestId("dpdp-coverage-figure")).toHaveTextContent("62.2%");
    expect(screen.getByTestId("dpdp-status-counts")).toHaveTextContent(buildScoreView(mixed).statusCountsLine);
  });
});

describe("a reply that is not a whole score", () => {
  it("is treated as no score, not rendered", () => {
    for (const bad of [{}, { requirements: [] }, "nope", 7]) {
      const { container } = render(<DpdpScoreSection score={bad as never} />);
      expect(container).toBeEmptyDOMElement();
    }
  });
});

describe("what the scan found: verified gaps, missing evidence, uncertain", () => {
  it("summarises the breakdown in one line", () => {
    const v = buildScoreView(review);
    expect(v.breakdownLine).toMatch(/^\d+ current gaps? · \d+ readiness gaps? \(duty not yet in force\) · \d+ partially satisfied · \d+ requirements? with missing evidence · \d+ uncertain, for review$/);
    expect(v.verifiedGaps).toEqual([]);                                     // nothing is in force yet
    expect(v.readinessGaps.map((g) => g.id)).toEqual(expect.arrayContaining(["DPDP-C1", "DPDP-N1"]));
  });

  it("counts requests, known tracker requests and cookies apart, and says a request is not a tracker that ran", () => {
    const v = buildScoreView(review);
    expect(v.evidenceLines[0]).toBe("Third-party network requests: 2 (before consent 2 · after Accept 0 · after Reject 0)");
    expect(v.evidenceLines[1]).toContain("Requests matching known analytics or advertising services: 1");
    expect(v.evidenceLines[1]).toContain("execution not confirmed");
    expect(v.evidenceLines[2]).toBe("Requests that matched no known service: 1");
    expect(v.evidenceLines[3]).toContain("Cookies: 0");
    expect(v.evidenceLines[4]).toBe("Pages of the main domain scanned: 5");
    expect(v.evidenceNote).toContain("does not show that a tracker ran");
  });

  it("splits the review into uncertain findings and missing evidence", () => {
    const v = buildScoreView(review);
    expect(v.reviewUncertain.map((i) => i.id)).toEqual(expect.arrayContaining(["known-tracker-before-consent", "privacy-notice-not-found"]));
    expect(v.reviewMissing.map((i) => i.id)).toEqual(expect.arrayContaining(["page-limit", "organisational-requirements"]));
    expect(v.reviewTitle).toBe("Logic Required / Human Review");
  });

  it("labels what each item needs", () => {
    const item = buildScoreView(review).reviewUncertain.find((i) => i.id === "known-tracker-before-consent")!;
    expect(item.needs).toEqual(["Business context", "Legal interpretation"]);
    expect(item.known.join(" ")).toContain("not proof that a tracker ran");
    expect(item.missing.join(" ")).toContain("Whether personal data was actually sent");
  });

  it("separates requirements that need organisational evidence from ones the scan could not observe", () => {
    const kinds = Object.fromEntries(buildScoreView(review).missingEvidence.map((m) => [m.id, m.kind]));
    expect(kinds["DPDP-S6"]).toBe("needs organisational evidence");
  });
});

describe("the dashboard's Logic Required / Human Review box", () => {
  it("is shown even when the score is withheld", () => {
    render(<DpdpScoreSection score={insufficient} />);
    expect(screen.getByTestId("dpdp-review-box")).toBeInTheDocument();
  });

});

describe("each check as a documented record", () => {
  it("shows the status counts for all five statuses, and only those five", () => {
    const v = buildScoreView(mixed);
    expect(v.statusCountsLine).toBe("Met 3 · Partially Met 1 · Not Met 2 · Unable to Verify 5 · Not Applicable 5");
    expect(Object.keys(STATUS_LABEL)).toEqual(["met", "partially_met", "not_met", "unable_to_verify", "not_applicable"]);
    expect(v.statusCountsLine).not.toContain("Counted under");
  });

  it("labels a gap in a duty that is not yet in force a readiness gap, never a current violation", () => {
    const v = buildScoreView(review);
    const c1 = v.assessed.find((r) => r.id === "DPDP-C1")!;
    expect(c1.status).toBe("Not Met");
    expect(c1.timingNote).toBe("Readiness gap: applies from 13 May 2027");
    expect(v.readinessGaps[0].timingNote).toBe("Readiness gap: applies from 13 May 2027");
    expect(v.verifiedGaps).toEqual([]);
    expect(v.assessed.find((r) => r.id === "DPDP-N1")!.timingNote).toContain("Readiness gap");
    expect(v.assessed.find((r) => r.id === "DPDP-C2")?.timingNote ?? null).toBeNull();     // met or not applicable: no gap
  });

  it("calls the same gap a current gap once the duty applies", () => {
    const after = { ...review, requirements: review.requirements.map((r) => ({ ...r, in_force: true, timing: "in_force" as const })),
      breakdown: { ...review.breakdown, verified_gaps: review.breakdown.readiness_gaps.map((g) => ({ ...g, timing: "in_force" as const })), readiness_gaps: [] } };
    const v = buildScoreView(after);
    expect(v.verifiedGaps.length).toBeGreaterThan(0);
    expect(v.verifiedGaps[0].timingNote).toBe("Current gap: in force");
    expect(v.readinessGaps).toEqual([]);
  });

  it("gives every gap what was observed, why it matters, the action and how to verify it", () => {
    const g = buildScoreView(review).readinessGaps.find((x) => x.id === "DPDP-C1")!;
    expect(g.detail.length).toBeGreaterThan(0);
    expect(g.pages).toEqual(["https://x.test/pricing"]);
    expect(g.whyItMatters).toContain("Act s.6(1)");
    expect(g.action).toContain("after the visitor accepts");
    expect(g.verify).toContain("Re-scan");
    for (const x of [...buildScoreView(review).readinessGaps, ...buildScoreView(mixed).partiallySatisfied]) {
      expect(x.whyItMatters && x.action && x.verify, x.id).toBeTruthy();
    }
  });

  it("carries the check definition and the evidence record on every row", () => {
    const c1 = buildScoreView(review).assessed.find((r) => r.id === "DPDP-C1")!;
    expect(c1.description).toContain("do not run before the visitor has given consent");
    expect(c1.criteria).toContain("Not Met:");
    expect(c1.expectedEvidence).toContain("Before-consent observations");
    expect(c1.observations.length).toBeGreaterThan(0);
    expect(c1.pages).toEqual(["https://x.test/pricing"]);
    expect(c1.sufficiency).toMatch(/^Sufficient: /);
    expect(c1.limitation).toContain("Covers only the 5 main-domain page(s) scanned");
  });

  it("says plainly when the evidence is not sufficient", () => {
    const s6 = buildScoreView(review).unableToVerify.find((r) => r.id === "DPDP-S6")!;
    expect(s6.sufficiency).toMatch(/^Not sufficient: /);
    expect(s6.limitation).toBe("A website scan cannot observe this.");
  });
});

describe("the dashboard shows the new records", () => {
  it("shows the definition, criteria and evidence record under each requirement", () => {
    render(<DpdpScoreSection score={review} />);
    const rec = screen.getByTestId("dpdp-record-DPDP-C1");
    expect(rec).toHaveTextContent("Assessment criteria:");
    expect(rec).toHaveTextContent("Evidence expected:");
    expect(rec).toHaveTextContent("Pages: https://x.test/pricing");
    expect(rec).toHaveTextContent("Sufficient:");
  });
});

describe("website checks versus business checks, confidence and rationale", () => {
  it("labels each row and counts the two kinds", () => {
    const v = buildScoreView(review);
    expect(v.assessed.every((r) => r.checkType === "Website check")).toBe(true);
    expect(v.unableToVerify.find((r) => r.id === "DPDP-S6")!.checkType).toBe("Business check");
    expect(v.checkTypesLine).toContain("website checks and 5 business checks apply");
    expect(v.checkTypesLine).toContain("never counted as met");
  });

  it("shows applicability, confidence and the scoring rationale", () => {
    const c1 = buildScoreView(review).assessed.find((r) => r.id === "DPDP-C1")!;
    expect(c1.applicability).toContain("Every website");
    expect(["High", "Medium"]).toContain(c1.confidence);
    expect(c1.scoringRationale).toMatch(/^Not Met earns 0 of 5 points\. Weight 5:/);
    expect(buildScoreView(review).unableToVerify.find((r) => r.id === "DPDP-S6")!.confidence).toBe("None");
  });

  it("renders them on the dashboard", () => {
    render(<DpdpScoreSection score={review} />);
    expect(screen.getByTestId("dpdp-type-DPDP-C1")).toHaveTextContent("Website check");
    expect(screen.getByTestId("dpdp-type-DPDP-X10")).toHaveTextContent("Business check");   // entity-specific business checks are still listed
    expect(screen.getByTestId("dpdp-check-types")).toHaveTextContent("business checks apply");
    const rec = screen.getByTestId("dpdp-record-DPDP-C1");
    expect(rec).toHaveTextContent("Applies when:");
    expect(rec).toHaveTextContent("Confidence:");
    expect(rec).toHaveTextContent("Scoring rationale:");
  });

  it("makes no claim that ROPA, DPIA, retention, grievance handling or breach times are in place", () => {
    const item = buildScoreView(review).reviewMissing.find((i) => i.id === "organisational-requirements")!;
    const text = item.known.join(" ");
    for (const w of ["retention", "DPIA", "grievance", "breach response times", "ROPA"]) expect(text).toContain(w);
    expect(text).toContain("makes no statement");
  });
});

describe("the four finding groups", () => {
  const titles = ["Confirmed Findings", "Needs Human Review", "Unable to Verify", "Future Readiness"];

  it("builds exactly the four groups, in order, with their counts", () => {
    const v = buildScoreView(review);
    expect(v.groups.map((g) => g.title)).toEqual(titles);
    expect(v.groups.map((g) => g.key)).toEqual(["confirmed", "needs_review", "unable_to_verify", "future_readiness"]);
    expect(v.groupCountsLine).toBe(v.groups.map((g) => `${g.title} ${g.count}`).join(" · "));
    expect(v.groups.every((g) => g.count === g.entries.length)).toBe(true);
  });

  it("keeps a gap in a future duty out of Confirmed Findings and gives it its effective date", () => {
    const v = buildScoreView(review);
    expect(v.groups[0].entries).toEqual([]);
    const c1 = v.groups[3].entries.find((e) => e.id === "DPDP-C1")!;
    expect(c1.timingNote).toBe("Future readiness: applies from 2027-05-13. Not a current violation.");
    expect(c1.severity).toBe("Severity: not rated");
    expect(v.upcomingLines[0]).toContain("13 May 2027");
  });

  it("moves the same gap to Confirmed Findings, rated, once the duty applies", () => {
    const v = buildScoreView(confirmed);
    const c1 = v.groups[0].entries.find((e) => e.id === "DPDP-C1")!;
    expect(c1.severity).toBe("Severity: High");
    expect(c1.timingNote).toBe("Current: the duty is in force.");
    expect(v.groups[3].entries.find((e) => e.id === "DPDP-C1")).toBeUndefined();
    expect(v.upcomingLines).toEqual([]);
  });

  it("gives every finding its page, consent state, evidence, legal reference, impact and remediation", () => {
    const c1 = buildScoreView(confirmed).groups[0].entries.find((e) => e.id === "DPDP-C1")!;
    expect(c1.pages).toEqual(["https://x.test/pricing"]);
    expect(c1.consentState).toBe("Before Consent");
    expect(c1.evidenceIds).toEqual(["t1"]);
    expect(c1.legal).toContain("DPDP Act, 2023 s.6(1)");
    expect(c1.businessImpact).toContain("analytics and advertising providers");
    expect(c1.remediation).toContain("after the visitor accepts");
    expect(c1.howToVerify).toContain("Re-scan");
    expect(c1.caveat).toContain("not proof of a legal violation");
  });

  it("explains what is missing, and how to verify it, for Unable to Verify", () => {
    const s6 = buildScoreView(review).groups[2].entries.find((e) => e.id === "DPDP-S6")!;
    expect(s6.outcome).toBe("Unable to Verify");
    expect(s6.missing.length).toBeGreaterThan(0);
    expect(s6.howToVerify).toContain("Provide the evidence");
    expect(s6.severity).toBe("Severity: not rated");
  });

  it("says what must be reviewed for Needs Human Review", () => {
    const e = buildScoreView(review).groups[1].entries.find((x) => x.id === "known-tracker-before-consent")!;
    expect(e.outcome).toBe("Needs review");
    expect(e.whatToReview.join(" ")).toContain("legal advice");
    expect(e.explanation).toContain("legal interpretation");
    expect(e.related).toBe("DPDP-C1");
  });

  it("renders the groups: Confirmed, Unable to Verify and Future Readiness in the main column, Needs Human Review in the side box", () => {
    render(<DpdpScoreSection score={review} />);
    for (const k of ["confirmed", "unable_to_verify", "future_readiness"]) expect(screen.getByTestId(`dpdp-group-${k}`)).toBeInTheDocument();
    expect(screen.queryByTestId("dpdp-group-needs_review")).not.toBeInTheDocument();
    const box = screen.getByTestId("dpdp-review-box");
    expect(box).toHaveTextContent("Logic Required / Human Review");
    expect(box).toHaveTextContent("Needs Human Review (");
    expect(within(box).getByTestId("dpdp-finding-known-tracker-before-consent")).toHaveTextContent("What must be reviewed:");
    expect(screen.getByTestId("dpdp-review-statement")).toHaveTextContent("cannot establish complete DPDP compliance");
    expect(screen.getByTestId("dpdp-group-counts")).toHaveTextContent("Confirmed Findings 0");
  });

  it("shows a finding's full detail on the dashboard", () => {
    render(<DpdpScoreSection score={confirmed} />);
    const card = screen.getByTestId("dpdp-finding-DPDP-C1");
    expect(within(screen.getByTestId("dpdp-group-confirmed")).getByTestId("dpdp-finding-DPDP-C1")).toBe(card);
    expect(screen.getByTestId("dpdp-severity-DPDP-C1")).toHaveTextContent("Severity: High");
    for (const t of ["Legal reference:", "Affected page: https://x.test/pricing", "Consent state: Before Consent", "What was observed",
      "Business impact:", "Recommended remediation:", "How to verify:"]) expect(card).toHaveTextContent(t);
  });

  it("never lists the same requirement twice across Confirmed, Unable to Verify and Future Readiness", () => {
    for (const sc of [mixed, review, confirmed, insufficient, clean]) {
      const ids = buildScoreView(sc).groups.filter((g) => g.key !== "needs_review").flatMap((g) => g.entries.map((e) => e.id));
      expect(new Set(ids).size).toBe(ids.length);
    }
  });

  it("shows the empty groups as None rather than hiding them", () => {
    render(<DpdpScoreSection score={review} />);
    expect(screen.getByTestId("dpdp-group-confirmed")).toHaveTextContent("None.");
  });

  it("shows the whole status counts and group counts together", () => {
    render(<DpdpScoreSection score={review} />);
    expect(screen.getByTestId("dpdp-status-counts")).toHaveTextContent("Met ");
    expect(screen.getByTestId("dpdp-check-types")).toHaveTextContent("business checks apply");
  });
});

describe("the methodology, weights and calculation explanation", () => {
  it("states the version and that the weights are the documented defaults", () => {
    const m = buildScoreView(mixed).methodology;
    expect(m.versionLine).toBe("Methodology v1.5.0 · documented default weights");
    expect(m.custom).toBe(false);
    expect(m.weightsNote).toBe("Documented default weights.");
  });

  it("declares custom weights, and the default each one replaced", () => {
    const m = buildScoreView(custom).methodology;
    expect(m.versionLine).toBe("Methodology v1.5.0 · CUSTOM weights");
    expect(m.custom).toBe(true);
    expect(m.weightsNote).toContain("DPDP-C1 = 8 (default 5)");
    expect(m.weightsNote).toContain("not comparable");
    expect(m.weights.find((w) => w.id === "DPDP-C1")!.weight).toBe("8 (default 5)");
    expect(m.weights.find((w) => w.id === "DPDP-C2")!.weight).toBe("4");
  });

  it("explains the points rules, the reliability rule, duplicates and timing, without showing a formula", () => {
    const rules = buildScoreView(mixed).methodology.rules.join("\n");
    expect(rules).not.toMatch(/÷|×|Score \(%\)|Assessment coverage \(%\)/);   // the formulas are not shown, only the rules in words
    for (const t of ["Met earns 100%", "Partially Met earns 50%", "Not Met earns 0%", "never counted as met or as not met",
      "Insufficient Evidence", "scored once", "never as current violations", "up to 5 pages"]) expect(rules).toContain(t);
  });

  it("lists every requirement with its weight, rationale and when it applies", () => {
    const m = buildScoreView(mixed).methodology;
    expect(m.weights).toHaveLength(mixed.requirements.length);
    for (const w of m.weights) expect([w.rationale, w.applicability, w.type].every(Boolean), w.id).toBe(true);
    expect(m.weights.find((w) => w.id === "DPDP-X10")!.applies).toBe("not applicable here");
  });

  it("shows category weights with their share and rationale", () => {
    const m = buildScoreView(mixed).methodology;
    const consent = m.categories.find((c) => c.name === "Consent")!;
    expect(consent.weight).toBe(17);
    expect(consent.share).toMatch(/%$/);
    expect(consent.rationale).toContain("core of the Act's consent duty");
    expect(m.categories.reduce((n, c) => n + c.weight, 0)).toBe(mixed.readiness.applicable_weight);
  });

  it("lists the duplicate rules", () => {
    expect(buildScoreView(mixed).methodology.dedup).toEqual(expect.arrayContaining([
      expect.stringContaining("DPDP-C1 and DPDP-C2: Tracking without a consent mechanism is one gap, counted once."),
    ]));
  });

  it("shows coverage as its own figure, apart from the score", () => {
    const v = buildScoreView(mixed);
    expect(v.headline).toBe("56.5%");
    expect(v.coverageHeadline).toBe("62.2%");
    render(<DpdpScoreSection score={mixed} />);
    expect(screen.getByTestId("dpdp-headline")).toHaveTextContent("56.5%");
    expect(screen.getByTestId("dpdp-coverage-figure")).toHaveTextContent("62.2%");
  });

  it("still shows coverage when the score is withheld, and never shows a score", () => {
    const v = buildScoreView(insufficient);
    expect(v.headline).toBe("Insufficient Evidence");
    expect(v.coverageHeadline).toBe("0%");
    render(<DpdpScoreSection score={insufficient} />);
    expect(screen.getByTestId("dpdp-coverage-figure")).toHaveTextContent("0%");
  });

  it("explains the result in numbers", () => {
    const lines = buildScoreView(mixed).explanationLines;
    expect(lines[0]).toBe("The score is 56.5% because 13 of 23 weighted points were earned across 6 assessed requirements.");
    expect(lines[1]).toContain("DPDP-C1 Not Met 0 of 5");
    expect(lines[1]).toContain("DPDP-N2 Partially Met 1 of 2");
    expect(lines[2]).toContain("Coverage is 62.2% because 23 of 37 applicable weighted points could be assessed");
    expect(lines[2]).toContain("14 points were unverifiable and are not counted as met or as not met");
  });

  it("explains a withheld score instead", () => {
    const lines = buildScoreView(insufficient).explanationLines;
    expect(lines[0]).toMatch(/^No score is shown: /);
    expect(lines.join(" ")).not.toMatch(/The score is \d/);
  });

  it("renders the methodology on the dashboard", () => {
    render(<DpdpScoreSection score={custom} />);
    expect(screen.getByTestId("dpdp-version")).toHaveTextContent("Methodology v1.5.0 · CUSTOM weights");
    expect(screen.getByTestId("dpdp-weights-note")).toHaveTextContent("DPDP-C1 = 8 (default 5)");
    expect(screen.getByTestId("dpdp-rules")).toHaveTextContent("Insufficient Evidence");
    expect(screen.getByTestId("dpdp-weight-DPDP-C1")).toHaveTextContent("8 (default 5)");
    expect(screen.getByTestId("dpdp-category-weights")).toHaveTextContent("Consent");
    expect(screen.getByTestId("dpdp-dedup")).toHaveTextContent("counted once");
    expect(screen.queryByTestId("dpdp-explanation")).not.toBeInTheDocument();   // the arithmetic narrative is not shown
  });
});

describe("scope, scan time, versions and the prioritised remediation", () => {
  it("states the main domain, the pages scanned against the limit, and the consent states tested", () => {
    const v = buildScoreView(review);
    expect(v.scopeLines[0]).toBe("Scope: the entered main domain (x.test) only. 5 pages scanned, of a limit of 5.");
    expect(v.scopeLines[1]).toContain("https://x.test/0");
    expect(v.scopeLines[2]).toBe("Consent states tested: Before Consent, After Accept, After Reject.");
    expect(v.scopeLines.join(" ").toLowerCase()).not.toContain("subdomain");
  });

  it("says which consent states were not tested", () => {
    const lines = buildScoreView(insufficient).scopeLines;
    expect(lines[0]).toContain("0 pages scanned");
    expect(lines.at(-1)).toBe("Consent states tested: none. Not tested: Before Consent, After Accept, After Reject.");
  });

  it("shows the scan timestamp in UTC and the date effective dates were judged against", () => {
    expect(buildScoreView(review).scanTimeLine).toBe("Scan time: 21 Oct 2026, 06:41 UTC · assessed against effective dates on 21 October 2026");
  });

  it("shows the scoring methodology version and the legal mapping version separately", () => {
    const v = buildScoreView(review);
    expect(v.versionsLine).toBe("Scoring methodology v1.5.0 · Legal mapping v2026.10.1");
    expect(v.legalSources.join(" ")).toContain("G.S.R. 846(E)");
    expect(v.legalSources.join(" ")).toContain("13 May 2027");
  });

  it("says it is website readiness and not full legal compliance", () => {
    const v = buildScoreView(review);
    expect(v.natureNote).toContain("website readiness");
    expect(v.natureNote).toContain("not a guarantee");
    expect(v.natureNote).toContain("full legal compliance");
  });

  it("orders the remediation by the stated rule", () => {
    const plan = buildScoreView(review).remediationPlan;
    expect(plan.map((p) => p.rank)).toEqual(plan.map((_, i) => i + 1));
    expect(plan[0].title.startsWith("DPDP-C1")).toBe(true);
    expect(plan[0].priority).toBe("Prepare before 2027-05-13");
    expect(plan.every((p) => p.action && p.howToVerify && p.priorityReason && p.legal)).toBe(true);
    const now = buildScoreView(confirmed).remediationPlan;
    expect(now[0].priority).toBe("Fix now");
    expect(now[0].timing).toBe("Current");
  });

  it("flags a score that rests on little", () => {
    expect(buildScoreView(thin).caution).toContain("Treat this score with caution");
    expect(buildScoreView(thin).caution).toContain("2 assessed requirements");
    expect(buildScoreView(review).caution).toBeNull();          // enough was assessed, with decent coverage
    expect(buildScoreView(insufficient).caution).toBeNull();    // no score at all, so nothing to caution about
  });

  it("renders the scope, versions, caution and the remediation plan on the dashboard", () => {
    render(<DpdpScoreSection score={confirmed} />);
    expect(screen.getByTestId("dpdp-scope")).toHaveTextContent("the entered main domain (x.test) only");
    expect(screen.getByTestId("dpdp-scan-time")).toHaveTextContent("Scan time: 21 Oct 2026, 06:41 UTC");
    expect(screen.getByTestId("dpdp-versions")).toHaveTextContent("Legal mapping v2026.10.1");
    expect(screen.getByTestId("dpdp-nature")).toHaveTextContent("not a guarantee");
    expect(screen.getByTestId("dpdp-remediation")).toHaveTextContent("Fix now");
    expect(screen.getByTestId("dpdp-step-1")).toHaveTextContent("How to verify:");
    expect(screen.getByTestId("dpdp-legal-sources")).toHaveTextContent("G.S.R. 846(E)");
  });

  it("shows the caution beside a thin score on the dashboard", () => {
    render(<DpdpScoreSection score={thin} />);
    expect(screen.getByTestId("dpdp-caution")).toHaveTextContent("Treat this score with caution");
  });

  it("says plainly when there is nothing to remediate", () => {
    render(<DpdpScoreSection score={clean} />);
    expect(screen.getByTestId("dpdp-remediation")).toHaveTextContent("No remediation actions");
  });
});
