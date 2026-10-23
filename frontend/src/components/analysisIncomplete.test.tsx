import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// The PDF is drawn with a recording stand-in for jsPDF.
const drawn: string[][] = [[]];
vi.mock("jspdf", () => {
  class FakeDoc {
    constructor() { drawn.length = 0; drawn.push([]); }
    setFont() { return this; } setFontSize() { return this; } setTextColor() { return this; } setDrawColor() { return this; }
    setFillColor() { return this; } setLineWidth() { return this; } setLineCap() { return this; } line() { return this; }
    circle() { return this; } roundedRect() { return this; } rect() { return this; } link() { return this; } addImage() { return this; }
    getTextWidth(t: string) { return t.length * 4.5; }
    splitTextToSize(text: string, width: number): string[] {
      const per = Math.max(10, Math.floor(width / 4.6));
      const out: string[] = [];
      for (const part of text.split("\n")) for (let i = 0; i < Math.max(part.length, 1); i += per) out.push(part.slice(i, i + per));
      return out;
    }
    text(t: string | string[]) { drawn[drawn.length - 1].push(...(Array.isArray(t) ? t : [t])); return this; }
    addPage() { drawn.push([]); return this; }
    getNumberOfPages() { return drawn.length; }
    setPage() { return this; }
    save() { /* not written */ }
  }
  return { jsPDF: FakeDoc };
});

import type { DpdpScoreResponse } from "../api/types";
import type { ConsentScanState } from "../hooks/useConsentScan";
import { ANALYSIS_STAGES, phaseAfterStageFailure } from "../hooks/useConsentScan";
import { ANALYSIS_INCOMPLETE_NOTE, buildReportModel } from "../report/buildReport";
import { buildScoreView } from "../report/dpdpScore";
import { downloadReport } from "../report/downloadReport";
import fixtures from "../test/dpdpScoreFixtures.json";
import { DpdpScoreSection } from "./DpdpScoreSection";
import { RecommendationsSection } from "./DpdpAndRecommendations";
import { FindingsSection } from "./FindingsSection";
import { ScanStatusSection } from "./ScanStatusSection";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;

function state(over: Partial<ConsentScanState> = {}): ConsentScanState {
  return {
    phase: "degraded", scanId: "499b709a-0000", scan: null, stages: [], websiteScanMeta: null, findings: [], audit: [], evidence: null,
    error: 'Analysis failed at stage "rag_retrieval": Request timed out.', failedStage: "rag_retrieval", startedAt: 1, completedAt: 2, durationMs: 1, ...over,
  } as ConsentScanState;
}

describe("which phase a failed stage leaves the scan in", () => {
  it.each(["rag_retrieval", "llm_analysis", "output_validation", "findings_generated", "rule_findings_generated"])(
    "%s failing after the scan completed is degraded, not failed", (stage) => {
      expect(ANALYSIS_STAGES.has(stage)).toBe(true);
      expect(phaseAfterStageFailure(stage)).toBe("degraded");
    });

  it.each(["website_scan", "url_validation", "data_structuring", "classification", "rules_check", "audit_saved"])(
    "%s failing is still failed", (stage) => expect(phaseAfterStageFailure(stage)).toBe("failed"));
});

describe("a scan that completed but whose analysis did not", () => {
  it("is labelled as a complete scan with an incomplete analysis, not as Failed, when no rule matched", () => {
    render(<ScanStatusSection state={state()} />);
    expect(screen.getByText("Scan complete — analysis incomplete")).toBeInTheDocument();
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
  });

  it("keeps the existing label when the rules did produce findings", () => {
    render(<ScanStatusSection state={state({ findings: [{ id: "f" }] as never })} />);
    expect(screen.getByText("Findings ready — explanations unavailable")).toBeInTheDocument();
  });

  it("says plainly that nothing was found by the rules and that the analysis did not run, with no recommendation claimed", () => {
    const { container } = render(<FindingsSection findings={[]} onDecision={async () => {}} analysisFailed />);
    expect(container.textContent).toContain("the rules matched nothing on the scanned pages");
    expect(container.textContent).toContain("did not complete");
    expect(container.textContent).not.toContain("deterministic rules matched nothing to fall back on");
    const recs = render(<RecommendationsSection findings={[]} analysisFailed />);
    expect(recs.container.textContent).toContain("no findings to recommend against");
  });

  it("states on the score card that the score does not depend on the analysis, without changing the score", () => {
    const { container } = render(<DpdpScoreSection score={F.mixed} analysisNote={ANALYSIS_INCOMPLETE_NOTE} />);
    expect(screen.getByTestId("dpdp-analysis-note")).toHaveTextContent("do not depend on it");
    expect(screen.getByTestId("dpdp-headline")).toHaveTextContent(buildScoreView(F.mixed).headline);
    expect(container.querySelector('[data-testid="dpdp-analysis-note"]')).not.toBeNull();
  });

  it("shows no note when the analysis completed", () => {
    render(<DpdpScoreSection score={F.mixed} />);
    expect(screen.queryByTestId("dpdp-analysis-note")).not.toBeInTheDocument();
  });

  it("says so in the PDF beside the score, and nowhere when the analysis completed", async () => {
    const scan = { id: "499b709a-0000", url: "https://x.test/", evidence_counts: { pages: 1, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 } } as never;
    await downloadReport(buildReportModel(scan, [], null, [], [], buildScoreView(F.mixed), null, ANALYSIS_INCOMPLETE_NOTE));
    expect(drawn[0].join("")).toContain("The analysis did not complete for this scan");
    expect(drawn[0].join("")).toContain(buildScoreView(F.mixed).headline);   // the score itself is unchanged
    await downloadReport(buildReportModel(scan, [], null, [], [], buildScoreView(F.mixed), null, null));
    expect(drawn.map((p) => p.join("")).join("")).not.toContain("The analysis did not complete");
    expect(drawn.map((p) => p.join("")).join("")).not.toContain("Everything the scan recorded");   // still only the summary: there is no appendix without the scan's evidence
  });
});
