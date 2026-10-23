import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { PolicyAnalysis } from "../api/types";
import { buildPolicyView } from "../report/policyAnalysis";
import { PolicyAnalysisSection } from "./PolicyAnalysisSection";

const row = (id: string, status: string, basis = "required", quote = "") => ({
  id, title: `Title ${id}`, group: id.startsWith("g") ? "Grievance" : "Notice", basis, provisions: ["DPDP Act s.13(1)"], status, quote,
  source_url: quote ? "https://x.test/privacy" : null, detail: "detail", fix: `Fix ${id}`,
});
const A = (over: Partial<PolicyAnalysis> = {}): PolicyAnalysis => ({
  status: "analysed", reason: null, sector: null, documents: [], notes: [],
  summary: { applicable: 4, verified: 2, partial: 0, not_found: 2, not_applicable: 0, quotes_rejected: 0, completeness_percent: 50 },
  requirements: [row("n1", "verified", "required", "A quoted passage of the policy that is long enough."), row("n2", "not_found", "practice"), row("g1", "not_found", "required"), row("g2", "verified", "required", "Another quoted passage that is long enough too.")],
  limitations: ["Only the policy text was read."], model: null, ...over,
});

describe("the policy analysis view", () => {
  it("is null when the analysis never ran or was not loaded", () => {
    expect(buildPolicyView(null)).toBeNull();
    expect(buildPolicyView(undefined)).toBeNull();
    expect(buildPolicyView(A({ status: "not_run" }))).toBeNull();
  });

  it("groups the requirements, and lists the required missing points before the good-practice ones", () => {
    const v = buildPolicyView(A())!;
    expect(v.available).toBe(true);
    expect(v.headline).toBe("Policy completeness 50%");
    expect(v.groups.map((g) => g.name)).toEqual(["Notice", "Grievance"]);
    expect(v.fixes.map((f) => f.row.id)).toEqual(["g1", "n2"]);
  });

  it("explains an unavailable analysis and carries its reason", () => {
    const v = buildPolicyView(A({ status: "unavailable", reason: "A policy page was found but its text could not be read.", summary: null, requirements: [] }))!;
    expect(v.available).toBe(false);
    expect(v.reason).toContain("could not be read");
  });
});

describe("the policy analysis section", () => {
  it("shows the headline, the top fixes, each requirement with its status and basis, the exact quote with its source, and the limits", () => {
    render(<PolicyAnalysisSection analysis={A()} />);
    expect(screen.getByTestId("policy-headline")).toHaveTextContent("Policy completeness 50%");
    expect(screen.getByTestId("policy-fixes")).toHaveTextContent("Fix g1");
    expect(screen.getByTestId("policy-status-n1")).toHaveTextContent("Quoted");
    expect(screen.getByTestId("policy-status-n2")).toHaveTextContent("Not found");
    expect(screen.getByTestId("policy-req-n2")).toHaveTextContent("Good practice with a duty behind it");
    expect(screen.getByTestId("policy-req-g1")).toHaveTextContent("Required by the Act or Rules");
    expect(screen.getByTestId("policy-quote-n1")).toHaveTextContent("A quoted passage of the policy that is long enough.");
    expect(screen.getByTestId("policy-quote-n1").querySelector("a")).toHaveAttribute("href", "https://x.test/privacy");
    expect(screen.getByTestId("policy-req-g1")).not.toHaveTextContent("Fix:");   // the fix is in the ordered list, once, not repeated on each point
    expect(screen.getByTestId("policy-fix-g1")).toHaveTextContent("Fix g1");
    expect(screen.getByTestId("policy-fix-g1")).toHaveTextContent("Required");
    expect(screen.getByTestId("policy-fix-g1")).toHaveTextContent("Impact: Stating this would raise the policy's completeness from 50% to 75%.");
    expect(screen.getByTestId("policy-fix-n2")).toHaveTextContent("Good practice");
    expect(screen.queryByTestId("policy-fix-n1")).not.toBeInTheDocument();      // nothing to fix where the policy already says it
    // the checklist: every point, its status and its basis, at a glance
    const table = screen.getByTestId("policy-checklist");
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 4);
    expect(screen.getByTestId("policy-check-n1")).toHaveTextContent("Quoted");
    expect(screen.getByTestId("policy-check-g1")).toHaveTextContent("Not found");
    expect(screen.getByTestId("policy-check-g1")).toHaveTextContent("Required by the Act or Rules");
    expect(screen.getByTestId("policy-check-n2")).toHaveTextContent("Good practice with a duty behind it");
    expect(screen.getByTestId("policy-limits")).toHaveTextContent("Only the policy text was read.");
  });

  it("says why when unavailable, and renders nothing when the analysis never ran", () => {
    const { container, rerender } = render(<PolicyAnalysisSection analysis={A({ status: "unavailable", reason: "No policy page was found.", summary: null, requirements: [] })} />);
    expect(screen.getByTestId("policy-unavailable")).toHaveTextContent("No policy page was found.");
    rerender(<PolicyAnalysisSection analysis={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
