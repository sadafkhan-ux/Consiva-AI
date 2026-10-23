import { describe, expect, it } from "vitest";
import type { DpdpScoreResponse, PolicyAnalysis, PolicyRequirementResult } from "../api/types";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildScoreView } from "./dpdpScore";
import { buildPolicyView, POLICY_STATUS_LABEL } from "./policyAnalysis";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;

const req = (id: string, status: string, basis: string, group = "Notice"): PolicyRequirementResult => ({
  id, title: `Title ${id}`, group, basis, provisions: ["DPDP Act s.5(1)(i)", "DPDP Rules r.3(b)(i)"], status, quote: "", source_url: null, detail: "d", fix: `Fix ${id}`,
});
const analysis = (rows: PolicyRequirementResult[], verified: number, partial: number): PolicyAnalysis => {
  const live = rows.filter((r) => r.status !== "not_applicable");
  return { status: "analysed", reason: null, sector: null, documents: [], notes: [], limitations: [], model: null, requirements: rows,
    summary: { applicable: live.length, verified, partial, not_found: live.length - verified - partial, not_applicable: rows.length - live.length, quotes_rejected: 0,
      completeness_percent: live.length ? Math.round(((verified + 0.5 * partial) / live.length) * 1000) / 10 : null } };
};

describe("the policy checklist uses the four words the report uses everywhere", () => {
  it("says Quoted, Partially stated, Not found and Not applicable", () => {
    expect(POLICY_STATUS_LABEL).toEqual({ verified: "Quoted", partial: "Partially stated", not_found: "Not found", not_applicable: "Not applicable" });
  });

  it("lists every point with its status, whether the law requires it, and where", () => {
    const v = buildPolicyView(analysis([req("a", "verified", "required"), req("b", "partial", "required"), req("c", "not_found", "practice"), req("d", "not_applicable", "required", "Children")], 1, 1))!;
    expect(v.checklist.map((c) => [c.id, c.status, c.basis])).toEqual([
      ["a", "Quoted", "Required by the Act or Rules"], ["b", "Partially stated", "Required by the Act or Rules"],
      ["c", "Not found", "Good practice with a duty behind it"], ["d", "Not applicable", "Required by the Act or Rules"],
    ]);
    expect(v.checklist[0].provisions).toBe("DPDP Act, 2023, Section 5(1)(i); DPDP Rules, 2025, Rule 3(b)(i)");
    expect(v.summaryLine).toBe("1 quoted · 1 partially stated · 1 not found · 1 not applicable, of 4 points checked");
  });
});

describe("the policy fixes, in order, with what each does to the completeness", () => {
  const rows = [
    req("p_found", "not_found", "practice"), req("r_partial", "partial", "required"), req("r_found", "not_found", "required"), req("p_partial", "partial", "practice"),
    req("ok1", "verified", "required"), req("ok2", "verified", "required"), req("na", "not_applicable", "required"),
  ];
  const v = buildPolicyView(analysis(rows, 2, 2))!;

  it("puts the required points first, and within each a point not found before one only partly stated", () => {
    expect(v.fixes.map((f) => f.row.id)).toEqual(["r_found", "r_partial", "p_found", "p_partial"]);
    expect(v.fixes.map((f) => f.rank)).toEqual([1, 2, 3, 4]);
    expect(v.fixes.map((f) => f.kind)).toEqual(["Required", "Required", "Good practice", "Good practice"]);
  });

  it("is the completeness arithmetic run once more with that one point stated: a point not found counts in full, a partial one adds the half it lacks", () => {
    // 6 applicable points, 2 quoted and 2 partly stated: completeness (2 + 1) / 6 = 50%
    expect(v.fixes[0].impactLine).toBe("Stating this would raise the policy's completeness from 50% to 66.7%.");      // (3 + 1) / 6
    expect(v.fixes[1].impactLine).toBe("Stating this would raise the policy's completeness from 50% to 58.3%.");      // (3 + 0.5) / 6
  });

  it("leaves a point that is quoted or not applicable out of the fixes", () => {
    expect(v.fixes.map((f) => f.row.id)).not.toContain("ok1");
    expect(v.fixes.map((f) => f.row.id)).not.toContain("na");
  });

  it("says why each matters: required by the Act or Rules, or good practice with a duty behind it, with the provisions spelt in full", () => {
    expect(v.fixes[0].why).toBe("The Act or Rules require this to be given or published (DPDP Act, 2023, Section 5(1)(i); DPDP Rules, 2025, Rule 3(b)(i)).");
    expect(v.fixes[2].why).toContain("Good practice: a duty stands behind it");
    expect(v.fixes[2].why).toContain("do not say the policy itself must state it");
  });

  it("has no fixes when every applicable point is quoted", () => {
    expect(buildPolicyView(analysis([req("a", "verified", "required"), req("b", "not_applicable", "required")], 1, 0))!.fixes).toEqual([]);
  });

  it("gives no impact figure when the completeness was not assessed", () => {
    const none = analysis([req("a", "not_found", "required")], 0, 0);
    none.summary!.completeness_percent = null;
    expect(buildPolicyView(none)!.fixes[0].impactLine).toBe("");
  });
});

describe("the score's remediation steps say what fixing each does to the score", () => {
  const withImpact = (): DpdpScoreResponse => {
    const s = JSON.parse(JSON.stringify(F.review)) as DpdpScoreResponse;
    s.remediation_plan = s.remediation_plan.map((p, i) => ({ ...p, current_percent: 45.5, percent_if_fixed: 90.9 - i * 36.4, gain_percent: 45.4 - i * 36.4, impact: i === 0 ? "Visitors' browsing is shared before they have chosen." : "" }));
    return s;
  };

  it("states the score now, the score if that one check were fixed, and the gain", () => {
    const plan = buildScoreView(withImpact()).remediationPlan;
    expect(plan.length).toBeGreaterThan(0);
    expect(plan[0].scoreLine).toBe("Fixing this alone would raise the readiness score from 45.5% to 90.9% (+45.4 percentage points).");
    expect(plan[0].impact).toBe("Visitors' browsing is shared before they have chosen.");
  });

  it("says nothing about the score when the figures are absent (the score is withheld, or an older server)", () => {
    const plan = buildScoreView(F.review).remediationPlan;
    expect(plan.every((p) => p.scoreLine === "" && p.impact === "")).toBe(true);
    const s = withImpact();
    s.remediation_plan = s.remediation_plan.map((p) => ({ ...p, current_percent: null, percent_if_fixed: null, gain_percent: null }));
    expect(buildScoreView(s).remediationPlan.every((p) => p.scoreLine === "")).toBe(true);
  });

  it("keeps the order and everything else the plan already had", () => {
    const before = buildScoreView(F.review).remediationPlan;
    const after = buildScoreView(withImpact()).remediationPlan;
    expect(after.map((p) => [p.rank, p.title, p.priority, p.action, p.howToVerify, p.legal])).toEqual(before.map((p) => [p.rank, p.title, p.priority, p.action, p.howToVerify, p.legal]));
  });
});
