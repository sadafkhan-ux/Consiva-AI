import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpScoreResponse, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import { ScanHero } from "../components/ScanHero";
import { TenChecksPanel } from "../components/TenChecksPanel";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildReportModel } from "./buildReport";
import { buildScoreView } from "./dpdpScore";
import { FreeReport } from "./print/FreeReport";
import { FullReport } from "./print/FullReport";
import { SnapshotReport } from "./print/SnapshotReport";
import { fmtNum, formatScore, ringNumber } from "./scoreFormat";
import { buildTenChecks } from "./tenChecks";

afterEach(cleanup);
const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
const META = { consent_mechanism: "cmp", pages_scanned: 1, pages_discovered: 20, page_limit: 5 } as unknown as WebsiteScanMetadata;
const EVIDENCE = { pages: [{ id: "p", url: "https://x.test/", title: "t", http_status: 200, discovered_via: "seed" }], forms: [], cookies: [], trackers: [], third_party_services: [], policies: [], consent_signals: [] } as unknown as ScanEvidenceResponse;
const SCAN = { id: "11111111-1111-1111-1111-111111111111", url: "https://x.test/", evidence_counts: { pages: 1, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 } } as never;
const model = () => buildReportModel(SCAN, [], META, [], [], buildScoreView(F.mixed), EVIDENCE, null, null, null);

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

  it("the ten-check engine writes its score with it", () => {
    const t = buildTenChecks(model());
    expect(t.scoreText).toBe(formatScore(t.score));
    expect(t.scoreText).toMatch(/^\d+(\.\d+)?\/100$|^No meaningful score$/);
  });
});

describe("the same score, in the same form, on every surface", () => {
  it("the dashboard hero, the breakdown panel, the summary PDF and the Free and Full PDFs all show one number", () => {
    const m = model();
    const ten = buildTenChecks(m);
    const number = ringNumber(ten.scoreText);

    render(<ScanHero score={null} siteUrl="https://x.test/" screenshot={null} tenScore={{ score: ten.score, totalText: ten.totalText, scoreText: ten.scoreText, passed: ten.counts.passed, applicable: ten.applicableMax, notAssessed: ten.counts.notAssessed }} />);
    expect(screen.getByTestId("hero-score-label")).toHaveTextContent(`Privacy Readiness Score: ${ten.scoreText}`);
    expect(screen.getByTestId("hero-percent")).toHaveTextContent(number);
    cleanup();

    render(<TenChecksPanel ten={ten} />);
    expect(screen.getByTestId("ten-checks-panel")).toHaveTextContent(ten.scoreText);
    cleanup();

    render(<SnapshotReport model={m} />);
    expect(screen.getByTestId("pg-percent-snapshot")).toHaveTextContent(number);
    expect(screen.getByTestId("sn-total-row")).toHaveTextContent(ten.scoreText);
    cleanup();

    for (const R of [FreeReport, FullReport]) {
      const { container } = render(<R model={m} />);
      expect(container.querySelector(".pg-score-ring")!.textContent).toContain(number);
      cleanup();
    }
  });

  it("no surface prints the ten-check score with a percent sign", () => {
    const m = model();
    const ten = buildTenChecks(m);
    const { container } = render(<SnapshotReport model={m} />);
    expect(container.textContent).not.toContain(`Privacy Readiness Score ${fmtNum(ten.score ?? 0)}%`);
    expect(container.textContent).toContain(`Privacy Readiness Score ${ten.scoreText}`);
  });
});

describe("the dashboard hero while the run is still going", () => {
  const pending = { score: 66.7, totalText: "2 / 3", scoreText: "66.7/100", passed: 1, applicable: 3, notAssessed: 7, pending: true };

  it("shows no number: a score from the checks that happen to be ready is not the score the PDF will print", () => {
    render(<ScanHero score={null} siteUrl="https://x.test/" screenshot={null} tenScore={pending} />);
    expect(screen.getByTestId("hero-score-label")).toHaveTextContent("assessing");
    expect(screen.getByTestId("hero-band")).toHaveTextContent("Assessing");
    expect(screen.getByTestId("hero-percent")).toHaveTextContent("…");
    expect(document.body.textContent).not.toContain("66.7");
    expect(document.body.textContent).not.toContain("1 of 3");
    expect(screen.queryByTestId("hero-ring-arc")).toBeNull();
  });

  it("shows the final score, with the number of checks left out, once the run is over", () => {
    render(<ScanHero score={null} siteUrl="https://x.test/" screenshot={null} tenScore={{ ...pending, pending: false }} />);
    expect(screen.getByTestId("hero-score-label")).toHaveTextContent("Privacy Readiness Score: 66.7/100 (2 / 3 marks)");
    expect(screen.getByTestId("hero-band")).toHaveTextContent("1 of 3 assessed checks passed");
    expect(screen.getByTestId("hero-not-assessed")).toHaveTextContent("7 of 10 checks could not be assessed and are left out of the score");
  });

  it("says nothing about checks left out when none were", () => {
    render(<ScanHero score={null} siteUrl="https://x.test/" screenshot={null} tenScore={{ ...pending, pending: false, notAssessed: 0 }} />);
    expect(screen.queryByTestId("hero-not-assessed")).toBeNull();
  });

  it("the hero sits in a container its layout can query, so it stacks by its own column rather than the window", () => {
    const { container } = render(<ScanHero score={null} siteUrl="https://x.test/" screenshot={null} tenScore={pending} />);
    expect(container.querySelector(".scan-hero-wrap > .scan-hero")).not.toBeNull();
  });
});
