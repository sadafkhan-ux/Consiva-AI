import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpRequirementsResponse, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import { ScanHero } from "../components/ScanHero";
import { TenChecksPanel } from "../components/TenChecksPanel";
import fixtures from "../test/requirementsFixtures.json";
import { buildReportModel } from "./buildReport";
import { buildRequirementsView } from "./requirements";
import { FreeReport } from "./print/FreeReport";
import { FullReport } from "./print/FullReport";
import { SnapshotReport } from "./print/SnapshotReport";
import { fmtNum, formatScore, ringNumber } from "./scoreFormat";
import { A } from "../test/assessments";
import type { TenAssessment } from "./tenChecks";

afterEach(cleanup);
const F = fixtures as unknown as Record<string, DpdpRequirementsResponse>;
const META = { consent_mechanism: "cmp", pages_scanned: 1, pages_discovered: 20, page_limit: 5 } as unknown as WebsiteScanMetadata;
const EVIDENCE = { pages: [{ id: "p", url: "https://x.test/", title: "t", http_status: 200, discovered_via: "seed" }], forms: [], cookies: [], trackers: [], third_party_services: [], policies: [], consent_signals: [] } as unknown as ScanEvidenceResponse;
const SCAN = { id: "11111111-1111-1111-1111-111111111111", url: "https://x.test/", evidence_counts: { pages: 1, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 } } as never;
const withA = (a: TenAssessment | null) => buildReportModel(SCAN, [], META, [], [], buildRequirementsView(F.mixed), EVIDENCE, null, null, null, undefined, a);

describe("one way to write a score: N/100", () => {
  it.each([[94.4, "94.4/100"], [100, "100/100"], [0, "0/100"], [66.7, "66.7/100"], [31.25, "31.25/100"], [75, "75/100"]])("%s is written %s", (n, text) => {
    expect(formatScore(n)).toBe(text);
  });

  it("a score that is not available is words, never 0 or 100", () => {
    expect(formatScore(null)).toBe("No meaningful score");
    expect(formatScore(undefined, "no score")).toBe("no score");
    expect(formatScore(0)).toBe("0/100");
  });

  it("never carries a percent sign", () => {
    for (const n of [0, 12.5, 94.4, 100]) expect(formatScore(n)).not.toContain("%");
  });

  it("the ring prints the number alone, whatever form the score arrives in", () => {
    expect(ringNumber("94.4/100")).toBe("94.4");
    expect(ringNumber("94.4%")).toBe("94.4");
    expect(ringNumber("94.4")).toBe("94.4");
    expect(fmtNum(94.40001)).toBe("94.4");
  });

  it("the server's ten-check score is written with it", () => {
    for (const a of Object.values(A)) {
      expect(a.scoreText).toBe(formatScore(a.score));
      expect(a.scoreText).toMatch(/^\d+(\.\d+)?\/100$|^No meaningful score$/);
    }
  });
});

describe("one score: the server's ten-check score, on every surface", () => {
  it("the dashboard hero, the Summary PDF, the Free PDF and the Full PDF print the number the server sent, written N/100", () => {
    const a = A.mixed;
    const m = withA(a);
    const number = ringNumber(a.scoreText);

    render(<ScanHero assessment={a} siteUrl="https://x.test/" screenshot={null} />);
    expect(screen.getByTestId("hero-score-label")).toHaveTextContent(`Privacy Readiness Score: ${a.scoreText}`);
    expect(screen.getByTestId("hero-percent")).toHaveTextContent(number);
    cleanup();

    render(<SnapshotReport model={m} />);
    expect(screen.getByTestId("pg-percent-snapshot")).toHaveTextContent(number);
    expect(screen.getByTestId("sn-summary-text")).toHaveTextContent(`The Privacy Readiness Score is ${a.scoreText}`);
    cleanup();

    for (const R of [FreeReport, FullReport]) {
      const { container } = render(<R model={m} />);
      expect(container.querySelector(".pg-score-ring")!.textContent).toContain(number);
      cleanup();
    }
  });

  it("the ten checks are the score: the marks total, the ring and the panel agree, and the weighted reading is never printed as a score", () => {
    const a = A.mixed;
    const m = withA(a);
    render(<SnapshotReport model={m} />);
    expect(screen.getByTestId("sn-total-marks")).toHaveTextContent(a.totalText);
    expect(screen.getByTestId("sn-total-row")).toHaveTextContent(a.scoreText);
    expect(document.body.textContent).not.toContain("56.5");                    // the weighted requirements percentage is not a score anywhere
    cleanup();
    render(<TenChecksPanel ten={a} />);
    expect(screen.getByTestId("ten-note")).toHaveTextContent(/these marks added up, as the server worked them out/);
  });

  it("when the server withholds the score, every surface says there is none and none invents a number", () => {
    const m = withA(A.noScore);
    render(<ScanHero assessment={A.noScore} siteUrl="https://x.test/" screenshot={null} />);
    expect(screen.getByTestId("hero-percent")).toHaveTextContent("No score");
    cleanup();
    render(<SnapshotReport model={m} />);
    expect(screen.getByTestId("pg-percent-snapshot")).toHaveTextContent("No score");
    expect(screen.getByTestId("sn-summary-text")).toHaveTextContent("No Privacy Readiness Score is shown");
    cleanup();
    const { container } = render(<FreeReport model={m} />);
    expect(container.querySelector(".pg-score-ring")!.textContent).toContain("No score");
  });

  it("the Summary PDF's ring and the headline in its 'how to read' text are the same figure", () => {
    const m = withA(A.typical);
    render(<SnapshotReport model={m} />);
    const ring = screen.getByTestId("pg-percent-snapshot").textContent;
    expect(screen.getByTestId("sn-summary-text").textContent).toContain(`${ring}/100`);
  });
});

describe("the dashboard hero while the run is still going", () => {
  it("shows no number: the policy reading is not in yet, so any number could differ from the finished report", () => {
    render(<ScanHero assessment={A.mixed} siteUrl="https://x.test/" screenshot={null} pending />);
    expect(screen.getByTestId("hero-score-label")).toHaveTextContent("assessing");
    expect(screen.getByTestId("hero-band")).toHaveTextContent("Assessing");
    expect(screen.getByTestId("hero-percent")).toHaveTextContent("…");
    expect(document.body.textContent).not.toContain(A.mixed.scoreText);
    expect(screen.queryByTestId("hero-ring-arc")).toBeNull();
  });

  it("shows the score and the marks it rests on once the run is over", () => {
    render(<ScanHero assessment={A.mixed} siteUrl="https://x.test/" screenshot={null} />);
    expect(screen.getByTestId("hero-score-label")).toHaveTextContent(`Privacy Readiness Score: ${A.mixed.scoreText}`);
    expect(screen.getByTestId("hero-basis")).toHaveTextContent(A.mixed.summary);
  });

  it("says a score is provisional while the policy reading has not finished", () => {
    render(<ScanHero assessment={{ ...A.mixed, final: false }} siteUrl="https://x.test/" screenshot={null} />);
    expect(screen.getByTestId("hero-provisional")).toBeInTheDocument();
  });

  it("the hero sits in a container its layout can query, so it stacks by its own column rather than the window", () => {
    const { container } = render(<ScanHero assessment={A.mixed} siteUrl="https://x.test/" screenshot={null} />);
    expect(container.querySelector(".scan-hero-wrap > .scan-hero")).not.toBeNull();
  });
});
