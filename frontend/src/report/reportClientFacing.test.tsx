import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpScoreResponse, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildReportModel } from "./buildReport";
import { buildScoreView } from "./dpdpScore";
import { FreeReport } from "./print/FreeReport";
import { FullReport } from "./print/FullReport";
import { ReportDocument } from "./print/ReportDocument";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
afterEach(cleanup);

const META = { consent_mechanism: "cmp", pages_scanned: 1, pages_discovered: 1, page_limit: 5 } as unknown as WebsiteScanMetadata;
const EVIDENCE = { pages: [{ id: "p", url: "https://x.test/", title: "t", http_status: 200, discovered_via: "seed" }], forms: [], cookies: [], trackers: [], third_party_services: [], policies: [], consent_signals: [] } as unknown as ScanEvidenceResponse;
const SCAN = { id: "11111111-1111-1111-1111-111111111111", url: "https://x.test/", evidence_counts: { pages: 1, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 } } as never;
/** A score response as an older or a newer server might send it, even carrying a `deductions` block: the report must never show it. */
const model = (extra: Record<string, unknown> = {}) =>
  buildReportModel(SCAN, [], META, [], [], buildScoreView({ ...F.mixed, ...extra } as unknown as DpdpScoreResponse), EVIDENCE, null, null, null);

describe("the client-facing report keeps the scoring formula and the deductions out", () => {
  const LEAK = { deductions: { status: "scored", score_percent: 80, steps: [{ requirement_id: "DPDP-C1", title: "t" }], summary: "The score is 80.0%, so 20.0 points were deducted from 100", formula: "Score = points earned ÷ points assessed × 100" } };

  it.each([["the long report", false], ["the Full appendix", true]])("%s has no deductions section and no deduction formula", (_n, appendixOnly) => {
    const { container } = render(<ReportDocument model={model(LEAK)} appendixOnly={appendixOnly as boolean} />);
    expect(screen.queryByTestId("score-deductions")).toBeNull();
    expect(container.textContent).not.toMatch(/Why were points deducted/i);
    expect(container.textContent).not.toMatch(/points were deducted from 100/);
    expect(container.textContent).not.toMatch(/Score = points earned/);
  });

  it("the score view does not carry the deductions", () => {
    expect(Object.keys(buildScoreView({ ...F.mixed, ...LEAK } as unknown as DpdpScoreResponse))).not.toContain("deductions");
  });

  it("the Free and Full sheets show no deductions either", () => {
    for (const R of [FreeReport, FullReport]) {
      const { container } = render(<R model={model(LEAK)} />);
      expect(container.textContent).not.toMatch(/Why were points deducted|points were deducted from 100/);
      cleanup();
    }
  });
});

describe("one header for every page", () => {
  it("every page, the first included, uses the same dark header, in the Free and the Full report", () => {
    const m = model();
    const { container } = render(<FullReport model={m} />);
    const heads = [...container.querySelectorAll("header.pg-head")];
    expect(heads.length).toBe(8);
    for (const h of heads) expect(h.classList.contains("pg-head-dark")).toBe(true);
    cleanup();
    const free = render(<FreeReport model={m} />);
    const fh = [...free.container.querySelectorAll("header.pg-head")];
    expect(fh.length).toBe(2);
    expect(fh.every((h) => h.classList.contains("pg-head-dark"))).toBe(true);
  });

  it("the first page header names the domain, the scan time and the pages scanned, as the later pages do", () => {
    const m = model();
    for (const R of [FreeReport, FullReport]) {
      const { container } = render(<R model={m} />);
      const first = container.querySelector("header.pg-head")!;
      expect(first.querySelector("b")!.textContent).toBe("x.test");
      expect(first.textContent).toMatch(/Scanned .+ UTC · 1 pages? scanned of 1 found/);
      cleanup();
    }
  });
});
