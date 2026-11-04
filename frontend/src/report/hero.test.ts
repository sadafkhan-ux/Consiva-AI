import { describe, expect, it } from "vitest";

import { buildHeroView } from "./hero";
import { A, withScore } from "../test/assessments";

const limited = (score: number | null) => ({ ...withScore(score), limitedBasis: true });

describe("the hero's band does not call a score strong when it rests on little", () => {
  it("a high score on a full basis keeps its plain band", () => {
    expect(buildHeroView(withScore(100)).band).toBe("Strong indicators");
  });

  it("a high score on a limited basis says it is on limited evidence", () => {
    const h = buildHeroView(limited(100));
    expect(h.band).toBe("Strong indicators, on limited evidence");
    expect(h.tone).toBe("warn");
  });

  it("a middling score on a limited basis says so too", () => {
    expect(buildHeroView(limited(65)).band).toBe("Progress needed, on limited evidence");
  });

  it("a low score keeps its band, and a withheld score says so", () => {
    expect(buildHeroView(limited(20)).band).toBe("Significant gaps");
    expect(buildHeroView(A.noScore).band).toBe("Insufficient evidence");
    expect(buildHeroView(null).band).toBe("Score not available");
  });

  it("the headline and the figure come from the assessment", () => {
    const h = buildHeroView(A.mixed);
    expect(h.percent).toBe(75);
    expect(h.percentLabel).toBe("75/100");
    expect(h.headline).toContain("6 of 10 assessed checks passed");
  });
});
