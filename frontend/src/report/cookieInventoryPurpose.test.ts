import { describe, expect, it } from "vitest";
import type { EvidenceCookieItem } from "../api/types";
import { buildCookieInventory, humanDuration } from "./cookieInventory";

const cookie = (over: Partial<EvidenceCookieItem> = {}): EvidenceCookieItem => ({
  id: over.name ?? "c", name: "_ga", domain: "x.test", category: "analytics", vendor: "Google Analytics", is_first_party: true, source: "lookup", consent_states: ["post_accept"],
  path: "/", expiry: "2027-11-26T15:18:31+00:00", secure: false, http_only: false, same_site: "Lax", observations: [],
  provider: "Google Analytics", purpose: "Gives each browser a random identifier so Google Analytics can count visitors and sessions.", purpose_source: "catalogue", is_session: false, lifetime_days: 400, ...over,
});

describe("the readable lifetime", () => {
  it.each([[29 / 1440, "29 minutes"], [1 / 1440, "1 minute"], [0.25, "6 hours"], [1, "1 day"], [6, "6 days"], [45, "45 days"], [90, "3 months"], [365, "12 months"], [400, "13 months"], [730, "2 years"], [1095, "3 years"]])(
    "%s days is %s", (days, text) => expect(humanDuration(days)).toBe(text));
});

describe("each cookie's provider, purpose and lifetime", () => {
  it("carries the provider, the purpose and how it is known", () => {
    const [r] = buildCookieInventory([cookie()]).rows;
    expect(r.provider).toBe("Google Analytics");
    expect(r.purpose).toContain("count visitors and sessions");
    expect(r.purposeSource).toBe("catalogue");
  });

  it("falls back to the vendor, then to saying the provider is not identified, never to a guess", () => {
    expect(buildCookieInventory([cookie({ provider: null, vendor: "Some Vendor" })]).rows[0].provider).toBe("Some Vendor");
    expect(buildCookieInventory([cookie({ provider: null, vendor: null })]).rows[0].provider).toBe("Not identified");
  });

  it("says what could and could not be established about the purpose of a cookie that is not recognised", () => {
    const r = buildCookieInventory([cookie({ name: "zz_unknown", provider: null, vendor: null, category: null, purpose: "The purpose could not be identified from the cookie's name.", purpose_source: "unknown" })]).rows[0];
    expect(r.purposeSource).toBe("unknown");
    expect(r.purpose).toBe("The purpose could not be identified from the cookie's name.");
    const old = buildCookieInventory([cookie({ purpose: undefined, purpose_source: undefined })]).rows[0];       // a response from an older server
    expect(old.purpose).toBe("The purpose is not recorded for this scan.");
    expect(old.purposeSource).toBe("unknown");
  });

  it("shows a session cookie as Session, a persistent one in the unit a person would say, and marks one that lives over a year", () => {
    const rows = buildCookieInventory([
      cookie({ name: "s", expiry: null, is_session: true, lifetime_days: null }), cookie({ name: "m", lifetime_days: 29 / 1440 }),
      cookie({ name: "y", lifetime_days: 400 }), cookie({ name: "e", lifetime_days: 365 }),
    ]).rows;
    const by = Object.fromEntries(rows.map((r) => [r.name, r]));
    expect(by.s).toMatchObject({ lifetime: "Session", isSession: true, longLived: false });
    expect(by.m).toMatchObject({ lifetime: "29 minutes", longLived: false });
    expect(by.y).toMatchObject({ lifetime: "13 months", longLived: true });
    expect(by.e).toMatchObject({ lifetime: "12 months", longLived: false });       // exactly a year is not over a year
  });

  it("shows the expiry date when the time it was first seen was not recorded, and says so when nothing was", () => {
    expect(buildCookieInventory([cookie({ lifetime_days: null })]).rows[0].lifetime).toBe("2027-11-26");
    expect(buildCookieInventory([cookie({ lifetime_days: null, expiry: undefined, is_session: undefined })]).rows[0].lifetime).toBe("Not available");
  });
});

describe("what each cookie did in each consent state", () => {
  it("says Set, Not seen, or Not tested for each of before, after Accept and after Reject", () => {
    const rows = buildCookieInventory([
      cookie({ name: "a", consent_states: ["pre_consent", "post_accept"] }), cookie({ name: "b", consent_states: ["post_accept", "post_reject"] }),
    ], []).rows;
    const by = Object.fromEntries(rows.map((r) => [r.name, r.behaviour]));
    expect(by.a).toEqual({ before: "Set", accept: "Set", reject: "Not seen" });
    expect(by.b).toEqual({ before: "Not seen", accept: "Set", reject: "Set" });
  });

  it("says Not tested, not Not seen, for a state in which no choice was made", () => {
    const [r] = buildCookieInventory([cookie({ consent_states: ["pre_consent"] })], ["post_accept", "post_reject"]).rows;
    expect(r.behaviour).toEqual({ before: "Set", accept: "Not tested", reject: "Not tested" });
  });

  it("still says Set for a state that was not tested if the cookie was recorded there", () => {
    const [r] = buildCookieInventory([cookie({ consent_states: ["post_reject"] })], ["post_reject"]).rows;
    expect(r.behaviour.reject).toBe("Set");
  });
});

describe("the counts and the groups", () => {
  const set = [
    cookie({ name: "_ga", consent_states: ["pre_consent", "post_accept", "post_reject"], lifetime_days: 400 }),
    cookie({ name: "_fbp", category: "marketing", is_first_party: false, consent_states: ["post_accept"], lifetime_days: 90 }),
    cookie({ name: "sid", category: "functional", expiry: null, is_session: true, lifetime_days: null, consent_states: ["pre_consent"] }),
    cookie({ name: "zz", category: null, is_first_party: null, consent_states: ["post_accept"] }),
  ];

  it("counts cookies found, third-party, before consent, long-lived and after Reject from the same rows", () => {
    const { summary } = buildCookieInventory(set, []);
    expect(summary).toEqual({ found: 4, thirdParty: 1, beforeConsent: 2, longLived: 2, afterReject: 1 });
  });

  it("does not say how many cookies were seen after Reject when no choice was made", () => {
    expect(buildCookieInventory(set, ["post_reject"]).summary.afterReject).toBeNull();
  });

  it("groups cookies by what they are for, in a fixed order, leaving out an empty group", () => {
    const inv = buildCookieInventory(set, []);
    expect(inv.groups.map((g) => g.title)).toEqual(["Necessary and functional", "Analytics", "Advertising and marketing", "Not classified"]);
    expect(inv.groups.map((g) => g.rows.map((r) => r.name))).toEqual([["sid"], ["_ga"], ["_fbp"], ["zz"]]);
    expect(buildCookieInventory([set[0]], []).groups.map((g) => g.title)).toEqual(["Analytics"]);
    expect(inv.groups.every((g) => g.note.length > 20)).toBe(true);
  });

  it("holds every cookie exactly once across the groups", () => {
    const inv = buildCookieInventory(set, []);
    expect(inv.groups.flatMap((g) => g.rows).length).toBe(inv.total);
  });

  it("keeps everything the inventory had before", () => {
    const inv = buildCookieInventory(set, []);
    expect(inv.byState).toHaveLength(3);
    expect(inv.rows[0]).toMatchObject({ name: expect.any(String), domain: "x.test", sameSite: expect.any(String), sightings: expect.any(Array) });
  });
});
