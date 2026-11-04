import { describe, expect, it } from "vitest";

/**
 * The ten-check assessment (GET /assessment, report/tenChecks.ts) is the only score in the application. This guard reads the shipped source (every file under src that is
 * not a test, a test helper or a fixture) and fails when a trace of the removed weighted scoring engine comes back: its endpoint, its field names, its builder.
 */
const BANNED = [
  "score_percent", "points_earned", "points_assessed", "weighted", "dpdp-score", "dpdpScore", "DpdpScore", "buildScoreView", "ScoreView", "isDpdpScore",
  "percent_if_fixed", "gain_percent", "coverage_percent", "score_contribution", "counts_in_score", "ScoreComparison", "compareDpdpScore",
];

/** Every shipped source file as text: all of src except tests, test helpers and fixtures. */
const SOURCES = import.meta.glob(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}", "!../test/**", "!../**/_*"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;

describe("no second scoring engine in the shipped source", () => {
  const shipped = Object.keys(SOURCES);

  it("reads the source it is guarding", () => {
    expect(shipped.length).toBeGreaterThan(50);
    expect(shipped.some((p) => p.endsWith("/tenChecks.ts"))).toBe(true);
  });

  it.each(BANNED)("does not mention %s", (term) => {
    const hits = shipped.filter((p) => SOURCES[p].toLowerCase().includes(term.toLowerCase()));
    expect(hits).toEqual([]);
  });

  it("has no per-requirement weight, penalty or points field in the requirements reading", () => {
    const types = SOURCES["../api/types.ts"];
    const reading = types.slice(types.indexOf("export interface DpdpRequirementResult"), types.indexOf("export interface DpdpRequirementsResponse"));
    expect(reading).not.toMatch(/\b(weight|default_weight|points|penalty_points|coverage_share_percent|scoring_rationale)\b/);
  });
});
