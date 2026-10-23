import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ConsentScanState } from "../hooks/useConsentScan";

let current: Partial<ConsentScanState> = {};
vi.mock("../hooks/useConsentScan", async (orig) => {
  const real = await orig<typeof import("../hooks/useConsentScan")>();
  const base = { phase: "idle", scanId: null, scan: null, stages: [], websiteScanMeta: null, findings: [], audit: [], evidence: null, dpdpScore: null,
    error: null, failedStage: null, startedAt: null, completedAt: null, durationMs: null };
  return { ...real, useConsentScan: () => ({ state: { ...base, ...current }, run: vi.fn(), afterReviewDecision: vi.fn() }) };
});

import { ConsentAgentView } from "../ConsentAgentView";

const REPORT = /Download Report \(PDF\)/;

describe("the PDF report option", () => {
  beforeEach(() => { current = {}; });

  it("is offered for a completed scan that produced no findings (a clean site)", () => {
    current = { phase: "completed", scanId: "s1", findings: [], startedAt: 1, completedAt: 2 };
    render(<ConsentAgentView />);
    expect(screen.getByRole("button", { name: REPORT })).toBeEnabled();
    expect(screen.getByText("Three-page report")).toBeInTheDocument();
    expect(screen.queryByText("One-page summary")).not.toBeInTheDocument();
  });

  it("is offered when the analysis did not complete, and while findings await review", () => {
    for (const phase of ["degraded", "awaiting_review"] as const) {
      current = { phase, scanId: "s1", findings: [], startedAt: 1, completedAt: 2 };
      const { unmount } = render(<ConsentAgentView />);
      expect(screen.getByRole("button", { name: REPORT })).toBeEnabled();
      unmount();
    }
  });

  it("is shown disabled, with the reason, while the scan is running", () => {
    for (const phase of ["starting", "scanning", "analyzing"] as const) {
      current = { phase, scanId: "s1", startedAt: 1 };
      const { unmount } = render(<ConsentAgentView />);
      expect(screen.getByRole("button", { name: REPORT })).toBeDisabled();
      expect(screen.getByTestId("report-pending")).toHaveTextContent("available when the scan finishes");
      unmount();
    }
  });

  it("is not offered before any scan, or after a scan that failed", () => {
    for (const phase of ["idle", "failed"] as const) {
      current = { phase, error: "boom" };
      const { unmount } = render(<ConsentAgentView />);
      expect(screen.queryByRole("button", { name: REPORT })).not.toBeInTheDocument();
      unmount();
    }
  });
});
