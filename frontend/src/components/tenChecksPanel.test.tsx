import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpScoreResponse, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import { buildReportModel } from "../report/buildReport";
import { buildScoreView } from "../report/dpdpScore";
import { buildTenChecks } from "../report/tenChecks";
import fixtures from "../test/dpdpScoreFixtures.json";
import { TenChecksPanel } from "./TenChecksPanel";

afterEach(cleanup);
const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
const META = { consent_mechanism: "cmp", pages_scanned: 1, pages_discovered: 20, page_limit: 5 } as unknown as WebsiteScanMetadata;
const EVIDENCE = { pages: [{ id: "p", url: "https://x.test/", title: "t", http_status: 200, discovered_via: "seed" }], forms: [], cookies: [], trackers: [], third_party_services: [], policies: [], consent_signals: [] } as unknown as ScanEvidenceResponse;
const SCAN = { id: "11111111-1111-1111-1111-111111111111", url: "https://x.test/", evidence_counts: { pages: 1, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 } } as never;
const ten = () => buildTenChecks(buildReportModel(SCAN, [], META, [], [], buildScoreView(F.mixed), EVIDENCE, null, null, null));

describe("the dashboard's score breakdown", () => {
  it("shows nothing when there is no assessment", () => {
    render(<TenChecksPanel ten={null} />);
    expect(screen.queryByTestId("ten-checks-panel")).toBeNull();
  });

  it("lists all ten checks with result, marks, finding, and the same total and score the PDF prints", () => {
    const t = ten();
    render(<TenChecksPanel ten={t} />);
    expect(screen.getByTestId("ten-checks-panel")).toHaveTextContent(`${t.totalText} marks, ${t.scoreText}`);
    for (const c of t.checks) {
      const row = within(screen.getByTestId(`ten-check-${c.id}`));
      expect(row.getByText(c.result)).toBeTruthy();
      expect(screen.getByTestId(`ten-check-${c.id}`)).toHaveTextContent(c.marksText);
    }
    expect(screen.getByTestId("ten-summary")).toHaveTextContent(t.summary);
    expect(screen.getByTestId("ten-formula")).toHaveTextContent(t.formula);
  });

  it("says plainly when a result has no source address, and never shows an invented one", () => {
    render(<TenChecksPanel ten={ten()} />);
    for (const row of screen.getAllByTestId(/^ten-check-\d+$/)) {
      const links = row.querySelectorAll("a");
      for (const a of links) expect(a.getAttribute("href")).toMatch(/^https?:\/\//);
    }
  });
});
