import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpScoreResponse, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import { DpdpScoreSection } from "../components/DpdpScoreSection";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildReportModel } from "./buildReport";
import { buildScoreView } from "./dpdpScore";
import { buildPageReport } from "./pageReports";
import { FreeReport } from "./print/FreeReport";
import { FullReport } from "./print/FullReport";
import { ReportDocument } from "./print/ReportDocument";

/**
 * One scan, one score, on every surface. A website scored 100 in some scans and 80 in others; the scoring engine was proven not to be the cause (the scans
 * recorded different evidence). What the front end must guarantee is the other half: whatever number the server sends is the number every surface shows, none
 * recomputes it, and the same evidence drawn twice gives the same pictures.
 */
const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
afterEach(cleanup);

const META = { consent_mechanism: "cmp", pages_scanned: 3, pages_discovered: 10, page_limit: 5 } as unknown as WebsiteScanMetadata;
const EVIDENCE = {
  pages: [{ id: "p", url: "https://x.test/", title: "t", http_status: 200, discovered_via: "seed" }], forms: [], cookies: [], trackers: [], third_party_services: [], policies: [], consent_signals: [],
} as unknown as ScanEvidenceResponse;
const SCAN = { id: "11111111-1111-1111-1111-111111111111", url: "https://x.test/", evidence_counts: { pages: 1, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 } } as never;
const model = (score: DpdpScoreResponse, evidence: ScanEvidenceResponse = EVIDENCE) => buildReportModel(SCAN, [], META, [], [], buildScoreView(score), evidence, null, null, null);

const SCORED = Object.entries(F).filter(([, v]) => v.readiness.status === "scored");

describe("every surface shows the number the server sent", () => {
  it.each(SCORED)("%s", (_name, score) => {
    const m = model(score);
    const v = m.dpdpScore!;
    const server = score.readiness.score_percent!;
    expect(v.scoreValue).toBe(server);

    // the page-report builders behind the Free and Full sheets
    expect(buildPageReport(m, "free").hero.percent).toBe(server);
    expect(buildPageReport(m, "full").hero.percent).toBe(server);

    // the long report
    render(<ReportDocument model={m} />);
    expect(screen.getByTestId("hero-percent").textContent).toBe(v.headline);
    expect(screen.getByTestId("rpt-framework-score").textContent).toBe(v.headline);
    cleanup();

    // the fixed sheets, as drawn
    for (const R of [FreeReport, FullReport]) {
      const { container } = render(<R model={m} />);
      expect(container.querySelector(".pg-score-ring")!.textContent).toContain(String(server % 1 === 0 ? server : server));
      cleanup();
    }

    // the dashboard card
    render(<DpdpScoreSection score={score} />);
    expect(screen.getByTestId("dpdp-headline").textContent).toBe(v.headline);
  });

  it("no surface shows a number the server did not send, whatever else the evidence says", () => {
    const loud: ScanEvidenceResponse = {
      ...EVIDENCE,
      trackers: Array.from({ length: 25 }, (_, i) => ({ id: `t${i}`, script_src: `https://t${i}.example/a.js`, vendor: "V", category: "analytics", source: "rule", consent_states: ["pre_consent"], page_url: "https://x.test/" })),
    } as unknown as ScanEvidenceResponse;
    for (const [, score] of SCORED) {
      const calm = model(score);
      const noisy = model(score, loud);
      expect(noisy.dpdpScore!.scoreValue).toBe(calm.dpdpScore!.scoreValue);
      expect(buildPageReport(noisy, "free").hero.percent).toBe(buildPageReport(calm, "free").hero.percent);
    }
  });
});

describe("the same evidence draws the same report twice", () => {
  it.each(SCORED)("%s", (_name, score) => {
    const a = render(<ReportDocument model={model(score)} />).container.textContent;
    cleanup();
    const b = render(<ReportDocument model={model(score)} />).container.textContent;
    expect(b).toBe(a);
  });
});

describe("a score is never invented", () => {
  it("an unavailable score is unavailable on every surface, never 0 or 100", () => {
    const score = F.insufficient;
    const m = model(score);
    expect(m.dpdpScore!.scoreValue).toBeNull();
    expect(buildPageReport(m, "free").hero.percent).toBeNull();
    render(<ReportDocument model={m} />);
    expect(screen.getByTestId("hero-percent").textContent).toBe("No score");
    cleanup();
    render(<DpdpScoreSection score={score} />);
    expect(screen.getByTestId("dpdp-headline").textContent).not.toMatch(/^(0|100)/);
  });

  it("the two scores a report prints are told apart: the headline and the five-page review say they are different measures", () => {
    const m = model(F.mixed);
    render(<ReportDocument model={m} />);
    const text = document.body.textContent ?? "";
    if (m.readiness?.available) {
      expect(text).toMatch(/Five-page review score/);
      expect(text).toMatch(/separate five-page reading, not the DPDP Compliance Score/);
    }
  });
});
