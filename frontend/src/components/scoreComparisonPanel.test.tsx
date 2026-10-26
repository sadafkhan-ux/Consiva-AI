import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ScoreComparison } from "../api/types";
import { ApiError } from "../api/types";

const compareDpdpScore = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...(await orig<typeof import("../api/client")>()), api: { compareDpdpScore: (id: string) => compareDpdpScore(id) } }));

import { ScoreComparisonPanel } from "./ScoreComparisonPanel";

const side = (over = {}) => ({ scan_id: "s", scanned_at: "2026-10-20T10:00:00Z", score_percent: 100, status: "scored", coverage_percent: 64.1, applicable_weight: 39, assessed_weight: 25, insufficient_reason: null, ...over });
const cmp = (over: Partial<ScoreComparison> = {}): ScoreComparison => ({
  previous: side(), latest: side({ score_percent: 76.2, coverage_percent: 51.3, assessed_weight: 20, scanned_at: "2026-10-26T09:00:00Z" }), score_change: -23.8,
  summary: "Score 100% to 76.2%. Why: C3 could not be tested.", causes: ["scan_coverage"],
  changes: [{
    id: "C3", title: "Reject honoured", category: "consent", cause: "scan_coverage", explanation: "The Reject choice could not be tested in this scan.", score_before: 100, score_after: 76.2, score_change: -23.8,
    score_becomes_available: false, score_becomes_withheld: false,
    previous: { status: "met", status_label: "Met", points_earned: 5, weight: 5, counts_in_score: true, evidence: ["Reject honoured"], evidence_ids: [] },
    latest: { status: "unable_to_verify", status_label: "Unable to verify", points_earned: 0, weight: 5, counts_in_score: false, evidence: ["No reject control was reachable"], evidence_ids: [] },
  }],
  changed_inputs: [], denominators: { previous: { applicable_weight: 39, assessed_weight: 25 }, latest: { applicable_weight: 39, assessed_weight: 20 }, changed: true },
  requirements_missing_from_one_scan: [], attribution: { method: "sequential", sums_to_total: true }, methodology_version: "1.0.0", assessed_on: "2026-10-26", methodology_note: "Both scans are assessed now.", ...over,
});

afterEach(() => { cleanup(); compareDpdpScore.mockReset(); });

describe("ScoreComparisonPanel", () => {
  it("renders nothing without a scan", () => {
    render(<ScoreComparisonPanel scanId={null} />);
    expect(screen.queryByTestId("score-comparison")).toBeNull();
  });

  it("makes no request until asked, then shows both scores, the changed requirement and its cause", async () => {
    compareDpdpScore.mockResolvedValue(cmp());
    render(<ScoreComparisonPanel scanId="abc" />);
    expect(compareDpdpScore).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("score-comparison-load"));
    await waitFor(() => expect(screen.getByTestId("score-comparison-result")).toBeTruthy());
    expect(compareDpdpScore).toHaveBeenCalledWith("abc");
    const text = screen.getByTestId("score-comparison-result").textContent ?? "";
    expect(text).toContain("100%");
    expect(text).toContain("76.2%");
    expect(text).toContain("-23.8 points");
    expect(text).toContain("Unable to verify");
    expect(text).toContain("scan coverage");
  });

  it("never prints 0% or 100% for a scan that has no score", async () => {
    compareDpdpScore.mockResolvedValue(cmp({ latest: side({ score_percent: null, status: "insufficient_evidence", coverage_percent: null }), score_change: null }));
    render(<ScoreComparisonPanel scanId="abc" />);
    fireEvent.click(screen.getByTestId("score-comparison-load"));
    await waitFor(() => expect(screen.getByTestId("score-comparison-result")).toBeTruthy());
    expect(screen.getByTestId("score-comparison-result").textContent).toContain("no score");
  });

  it("says plainly when there is no earlier scan", async () => {
    compareDpdpScore.mockRejectedValue(new ApiError(409, "no earlier scan"));
    render(<ScoreComparisonPanel scanId="abc" />);
    fireEvent.click(screen.getByTestId("score-comparison-load"));
    await waitFor(() => expect(screen.getByTestId("score-comparison-error")).toBeTruthy());
    expect(screen.getByTestId("score-comparison-error").textContent).toContain("no earlier completed scan");
  });
});
