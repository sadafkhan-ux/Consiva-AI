import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpRequirementsResponse, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import { A } from "../test/assessments";
import type { TenAssessment } from "./tenChecks";
import fixtures from "../test/requirementsFixtures.json";
import { buildReportModel } from "./buildReport";
import { buildRequirementsView } from "./requirements";
import { SnapshotReport } from "./print/SnapshotReport";
import { ringNumber } from "./scoreFormat";
import { buildPageReport } from "./pageReports";
import { FreeReport } from "./print/FreeReport";
import { FullReport } from "./print/FullReport";
import { ReportDocument } from "./print/ReportDocument";

/**
 * One scan, one score, on every surface. A website scored 100 in some scans and 80 in others; the scoring engine was proven not to be the cause (the scans
 * recorded different evidence). What the front end must guarantee is the other half: whatever number the server sends is the number every surface shows, none
 * recomputes it, and the same evidence drawn twice gives the same pictures.
 */
const F = fixtures as unknown as Record<string, DpdpRequirementsResponse>;
afterEach(cleanup);

const META = { consent_mechanism: "cmp", pages_scanned: 3, pages_discovered: 10, page_limit: 5 } as unknown as WebsiteScanMetadata;
const EVIDENCE = {
  pages: [{ id: "p", url: "https://x.test/", title: "t", http_status: 200, discovered_via: "seed" }], forms: [], cookies: [], trackers: [], third_party_services: [], policies: [], consent_signals: [],
} as unknown as ScanEvidenceResponse;
const SCAN = { id: "11111111-1111-1111-1111-111111111111", url: "https://x.test/", evidence_counts: { pages: 1, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 } } as never;
const model = (assessment: TenAssessment | null, evidence: ScanEvidenceResponse = EVIDENCE) => buildReportModel(SCAN, [], META, [], [], buildRequirementsView(F.mixed), evidence, null, null, null, undefined, assessment);

const SCORED = Object.entries(A).filter(([, v]) => v.score !== null);

describe("every surface shows the number the server sent", () => {
  it.each(SCORED)("%s", (_name, assessment) => {
    const m = model(assessment);
    const server = assessment.score!;
    expect(m.assessment!.score).toBe(server);

    // the page-report builders behind the Free and Full sheets
    expect(buildPageReport(m, "free").hero.percent).toBe(server);
    expect(buildPageReport(m, "full").hero.percent).toBe(server);

    // the long report
    render(<ReportDocument model={m} />);
    expect(screen.getByTestId("hero-percent").textContent).toBe(ringNumber(assessment.scoreText));
    expect(screen.getByTestId("rpt-framework-score").textContent).toBe(assessment.scoreText);
    cleanup();

    // the fixed sheets, as drawn: the server's number on every one (Free, Full and the two-page summary)
    for (const R of [FreeReport, FullReport, SnapshotReport]) {
      const { container } = render(<R model={m} />);
      expect(container.querySelector(".pg-score-ring")!.textContent).toContain(String(server));
      cleanup();
    }
  });

  it("no surface shows a number the server did not send, whatever else the evidence says", () => {
    const loud: ScanEvidenceResponse = {
      ...EVIDENCE,
      trackers: Array.from({ length: 25 }, (_, i) => ({ id: `t${i}`, script_src: `https://t${i}.example/a.js`, vendor: "V", category: "analytics", source: "rule", consent_states: ["pre_consent"], page_url: "https://x.test/" })),
    } as unknown as ScanEvidenceResponse;
    for (const [, assessment] of SCORED) {
      const calm = model(assessment);
      const noisy = model(assessment, loud);
      expect(noisy.assessment!.score).toBe(calm.assessment!.score);
      expect(buildPageReport(noisy, "free").hero.percent).toBe(buildPageReport(calm, "free").hero.percent);
    }
  });
});

describe("the same evidence draws the same report twice", () => {
  it.each(SCORED)("%s", (_name, assessment) => {
    const a = render(<ReportDocument model={model(assessment)} />).container.textContent;
    cleanup();
    const b = render(<ReportDocument model={model(assessment)} />).container.textContent;
    expect(b).toBe(a);
  });
});

describe("a score is never invented", () => {
  it("an unavailable score is unavailable on every surface, never 0 or 100", () => {
    const m = model(A.noScore);
    expect(m.assessment!.score).toBeNull();
    expect(buildPageReport(m, "free").hero.percent).toBeNull();
    render(<ReportDocument model={m} />);
    expect(screen.getByTestId("hero-percent").textContent).toBe("No score");
  });

  it("with no assessment at all there is no score, and nothing is worked out in its place", () => {
    const m = model(null);
    expect(buildPageReport(m, "free").hero.percent).toBeNull();
    render(<ReportDocument model={m} />);
    expect(screen.getByTestId("hero-percent").textContent).toBe("No score");
  });
});
