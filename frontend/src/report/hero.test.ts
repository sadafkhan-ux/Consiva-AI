import { describe, expect, it } from "vitest";

import { buildHeroView } from "./hero";
import type { ScoreView } from "./dpdpScore";

const view = (over: Partial<ScoreView>): ScoreView => ({ scoreValue: 100, coverageValue: 100, assessed: [{}] as never, unableToVerify: [], headline: "100%", scored: true, caution: null, ...over }) as ScoreView;

describe("the hero's band does not call a score strong when it rests on little", () => {
  it("a high score on full coverage keeps its plain band", () => {
    expect(buildHeroView(view({})).band).toBe("Strong indicators");
  });

  it("a high score on low coverage says it is on limited evidence", () => {
    const h = buildHeroView(view({ coverageValue: 51.5, caution: "Treat this score with caution" } as never));
    expect(h.band).toBe("Strong indicators, on limited evidence");
    expect(h.tone).toBe("warn");
  });

  it("coverage under 50% alone is enough", () => {
    expect(buildHeroView(view({ coverageValue: 30 })).band).toBe("Strong indicators, on limited evidence");
    expect(buildHeroView(view({ scoreValue: 65, coverageValue: 30 })).band).toBe("Progress needed, on limited evidence");
  });

  it("a low score keeps its band, and a withheld score says so", () => {
    expect(buildHeroView(view({ scoreValue: 20, coverageValue: 30 })).band).toBe("Significant gaps");
    expect(buildHeroView(view({ scoreValue: null, scored: false })).band).toBe("Insufficient evidence");
  });
});
