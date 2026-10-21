import { describe, expect, it } from "vitest";

import { buildReportModel } from "./buildReport";
import { registrableDomain, summariseTrackers } from "./trackerSummary";

const tracker = (id: string, src: string, category: string | null, vendor: string | null, states = ["pre_consent"]) =>
  ({ id, script_src: src, vendor, category, source: "rule", consent_states: states, page_url: null }) as never;

const finding = (risk: string) =>
  ({ id: risk, finding_text: `a ${risk} finding`, risk_level: risk, status: "pending", priority: risk, category: "other",
     evidence: [], dpdp_reference: [], recommendations: [] }) as never;

const meta = (over: Record<string, unknown> = {}) =>
  ({ consent_mechanism: "cmp", cmp_vendor: "Consiva Consent SDK", accept_interaction: "clicked", reject_interaction: "clicked",
     pages_scanned: 3, trackers_by_consent_state: { pre_consent: 5 }, ...over }) as never;

describe("registrableDomain", () => {
  it.each([
    ["https://cdn.example.com/a.js", "example.com"],
    ["https://x.bbc.co.uk/y", "bbc.co.uk"],
    ["https://www.googletagmanager.com/gtm.js", "googletagmanager.com"],
    ["https://example.com/", "example.com"],
  ])("%s -> %s", (url, expected) => expect(registrableDomain(url)).toBe(expected));
});

describe("summariseTrackers: what the 'N trackers' figure is made of", () => {
  const items = [
    tracker("1", "https://www.googletagmanager.com/gtm.js", "analytics", "Google Tag Manager"),
    tracker("2", "https://connect.facebook.net/fbevents.js", "marketing", "Meta Pixel"),
    tracker("3", "https://connect.facebook.net/other.js", "marketing", "Meta Pixel"),
    tracker("4", "https://fonts.gstatic.com/a.woff", "functional", "Google Fonts"),
    tracker("5", "https://ads.dianomi.com/x", null, null),
    tracker("6", "https://cdn.dianomi.com/y", null, null),
    tracker("7", "https://rubiconproject.com/z", null, null),
    tracker("8", "https://www.googletagmanager.com/td", "analytics", "Google Tag Manager", ["post_accept"]),
  ];

  it("separates named analytics/advertising, unknown, and everything else, for the state asked about", () => {
    const pre = summariseTrackers(items).pre_consent;

    expect(pre).toEqual({ analyticsAdRequests: 3, analyticsAdVendors: 2, unclassifiedRequests: 3, unclassifiedDomains: 2, otherRequests: 1 });
    expect(summariseTrackers(items).post_accept.analyticsAdRequests).toBe(1);
    expect(summariseTrackers(items).post_reject.analyticsAdRequests).toBe(0);
  });
});

describe("the report's consent lines", () => {
  it("replaces 'N trackers fired' with named vendors and a separate needs-review count", () => {
    const items = [
      tracker("1", "https://www.googletagmanager.com/gtm.js", "analytics", "Google Tag Manager"),
      tracker("2", "https://fonts.gstatic.com/a.woff", "functional", "Google Fonts"),
      tracker("3", "https://consiva.ai/sdk.js", "functional", "Consiva Consent SDK"),
      tracker("4", "https://ads.dianomi.com/x", null, null),
    ];

    const model = buildReportModel(null, [finding("high")], meta({ trackers_by_consent_state: { pre_consent: 4 } }), [], items);

    const text = model.consentIssues.join(" | ");
    expect(text).toContain("1 request to 1 named analytics or advertising vendor were made before any consent");
    expect(text).toContain("1 further third-party request before consent (1 domain) could not be classified and need review");
    expect(text).not.toMatch(/4 trackers/);
  });

  it("falls back to an honest generic line when no tracker detail is available", () => {
    const model = buildReportModel(null, [finding("high")], meta({ trackers_by_consent_state: { pre_consent: 26 } }));

    expect(model.consentIssues.join(" ")).toContain("26 third-party requests were made before any consent was given (not classified");
  });

  it("does not claim a missing Reject control when there was no banner at all", () => {
    const model = buildReportModel(null, [], meta({ consent_mechanism: "none", cmp_vendor: null, accept_interaction: "cmp_not_found", reject_interaction: "cmp_not_found", trackers_by_consent_state: { pre_consent: 0 } }));

    expect(model.consentIssues).toEqual(["No consent banner or CMP was detected on the pages scanned."]);
  });

  it("still reports a Reject control that was found but could not be used", () => {
    const model = buildReportModel(null, [], meta({ reject_interaction: "click_failed", trackers_by_consent_state: { pre_consent: 0 } }));

    expect(model.consentIssues.join(" ")).toContain("A 'Reject' control was found but could not be exercised");
  });
});

describe("headline and scope: a quiet report is not a clean one", () => {
  it("says 'findings to review', not that violations were identified", () => {
    const model = buildReportModel(null, [finding("high"), finding("low")], meta());

    expect(model.headline).toBe("2 findings to review.");
  });

  it("with no findings and all three states tested, says what was tested", () => {
    const model = buildReportModel(null, [], meta());

    expect(model.headline).toBe("No issues were observed in the tested scope (all three consent states were tested).");
    expect(model.scope).toBe("Scope: 3 pages scanned. Consent states: before consent tested; Accept tested; Reject tested.");
  });

  it("with no findings but Reject untested, refuses to call the result clean", () => {
    const model = buildReportModel(null, [], meta({ reject_interaction: "cmp_not_automatable" }));

    expect(model.headline).toBe("No findings were produced, but not every consent state could be tested: this is not a clean result.");
    expect(model.scope).toContain("Reject NOT tested");
  });

  it("with no findings and no scan metadata, never says clean", () => {
    const model = buildReportModel(null, [], null);

    expect(model.headline).toContain("not a clean result");
    expect(model.scope).toBe("Scope: not recorded for this scan.");
  });

  it("labels the count honestly", () => {
    const model = buildReportModel({ evidence_counts: { pages: 3, cookies: 2, trackers: 28, third_party_services: 8, policies: 2, forms: 0 } } as never, [], meta());

    expect(model.counts.map((c) => c.label)).toContain("Third-party requests");
    expect(model.counts.map((c) => c.label)).not.toContain("Trackers");
  });
});

describe("states in which no choice was made are not presented as tested", () => {
  const cookie = { id: "c1", name: "__cf_bm", domain: "calendly.com", category: "functional", vendor: "Cloudflare", is_first_party: false,
    source: "rule", consent_states: ["pre_consent", "post_accept", "post_reject"],
    observations: [{ consent_state: "post_accept", page_url: "https://smartians.ai/", observed_at: null, method: "single_page_load", source_request_url: null, candidate_page_urls: [] }] } as never;

  it("labels Accept and Reject in the cookie inventory when nothing was clicked", () => {
    const model = buildReportModel(null, [], meta({ accept_interaction: "cmp_not_found", reject_interaction: "cmp_not_found" }), [cookie]);

    const labels = model.cookies.byState.map((s) => s.label);
    expect(labels).toEqual(["Pre-Consent", "Post-Accept (no choice was made — not tested)", "Post-Reject (no choice was made — not tested)"]);
    expect(model.cookies.rows[0].sightings.find((x) => x.state === "post_accept")?.stateLabel).toContain("not tested");
    expect(model.cookies.rows[0].sightings.find((x) => x.state === "pre_consent")?.stateLabel).toBe("Pre-Consent");
  });

  it("leaves a state that was clicked unlabelled", () => {
    const model = buildReportModel(null, [], meta({ accept_interaction: "clicked", reject_interaction: "cmp_not_automatable" }), [cookie]);

    expect(model.cookies.byState.map((s) => s.label)).toEqual(["Pre-Consent", "Post-Accept", "Post-Reject (no choice was made — not tested)"]);
  });
});
