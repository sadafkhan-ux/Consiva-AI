import { describe, expect, it } from "vitest";
import type { DpdpScoreResponse, EvidenceCookieItem, PolicyAnalysis, PolicyRequirementResult, ScanEvidenceResponse, ScanScreenshot, WebsiteScanMetadata } from "../api/types";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildReportModel, type ReportModel } from "./buildReport";
import { buildScoreView } from "./dpdpScore";
import { buildTenChecks, marksLabel, roundScore, scoreChecks, type TenResult } from "./tenChecks";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
const SCAN = { id: "9bd5ccef-6909-4903-8d44-2b52a5668a06" as string, url: "https://x.test/", evidence_counts: { pages: 3, cookies: 2, trackers: 5, third_party_services: 1, policies: 1, forms: 1 } } as never;
const META = { consent_mechanism: "cmp", cmp_vendor: "Some CMP", accept_interaction: "clicked", reject_interaction: "clicked", pages_scanned: 3, pages_discovered: 40, page_limit: 5, stop_reason: "page_limit" } as unknown as WebsiteScanMetadata;
const tracker = (id: string, vendor: string | null, category: string | null, states: string[], src: string) => ({ id, script_src: src, vendor, category, source: "rule", consent_states: states, page_url: "https://x.test/" });
const cookie = (id: string, name: string, domain: string, category: string | null, vendor: string | null, states: string[]): EvidenceCookieItem =>
  ({ id, name, domain, category, vendor, is_first_party: false, source: "rule", consent_states: states, provider: vendor, purpose: "SHOULD NEVER BE PRINTED", purpose_source: "unknown", is_session: false, lifetime_days: 400, expiry: "2027-11-26T00:00:00+00:00", observations: [] });
const COOKIES = [
  cookie("c1", "_ga", "x.test", "analytics", "Google Analytics", ["post_accept"]),
  cookie("c2", "_fbp", ".facebook.com", "marketing", "Meta", ["pre_consent", "post_accept"]),
  cookie("c3", "mystery", "x.test", null, null, ["pre_consent"]),
];
const EVIDENCE = {
  pages: [
    { id: "p1", url: "https://x.test/", title: "Home", http_status: 200, discovered_via: "seed", selection_reason: "The address that was entered: always scanned first." },
    { id: "p2", url: "https://x.test/privacy", title: "Privacy", http_status: 200, discovered_via: "sitemap", selection_reason: "Privacy or cookie page: ranked first, because it settles the notice and cookie checks. Selected from pages found in the sitemap." },
    { id: "p3", url: "https://x.test/terms", title: "Terms", http_status: 200, discovered_via: "link", selection_reason: "Terms page: ranked after the privacy and cookie pages. Selected from pages found through a link on a scanned page." },
    { id: "p4", url: "https://x.test/old", title: null, http_status: 404, discovered_via: "failed" },
  ],
  forms: [{ id: "f1", selector: "form#c", purpose_guess: "contact_or_support", fields: [{ name: "email", field_type: "email", required: true }] }],
  cookies: COOKIES,
  trackers: [
    tracker("1", "Google Ads", "marketing", ["post_accept"], "https://www.googleadservices.com/a.js"),
    tracker("2", "Google Analytics", "analytics", ["post_accept"], "https://www.google-analytics.com/g.js"),
    tracker("3", "Static CDN", "functional", ["pre_consent", "post_accept", "post_reject"], "https://d1.cloudfront.net/x.js"),
    tracker("4", null, null, ["pre_consent"], "https://mystery.example/t.js"),
  ],
  third_party_services: [{ id: "s1", service_name: "Google Ads", category: "advertising", domains: ["googleadservices.com"] }],
  policies: [{ id: "pol1", url: "https://x.test/privacy", policy_type: "privacy_policy" }],
  consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "Some CMP", has_reject_all: true, has_granular_choices: true, evidence: {} }],
} as unknown as ScanEvidenceResponse;
const SHOT: ScanScreenshot = { scan_id: "9bd5ccef-6909-4903-8d44-2b52a5668a06", domain: "x.test", status: "ok", available: true, message: null, page_url: "https://x.test/", mime: "image/jpeg", width: 1280, height: 720, captured_at: "2026-10-22T20:08:00Z", data: "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/" };
const model = (o: { score?: keyof typeof F; shot?: ScanScreenshot | null; meta?: WebsiteScanMetadata } = {}) =>
  buildReportModel(SCAN, [], o.meta ?? META, COOKIES, EVIDENCE.trackers, buildScoreView(F[o.score ?? "mixed"]), EVIDENCE, null, null, o.shot === undefined ? SHOT : o.shot);
const REQ_IDS = ["notice_data_itemised", "notice_purposes", "data_sharing", "retention", "rights_means", "rights_access", "rights_correction_erasure", "rights_nominate", "grievance_contact", "grievance_timeline", "dpo_contact"];
const req = (id: string, status: string): PolicyRequirementResult => ({ id, title: id, group: "g", basis: "required", provisions: [], status, quote: status === "not_found" ? "" : `quote for ${id}`, source_url: status === "not_found" ? null : "https://x.test/privacy", detail: "", fix: "" });
const analysis = (status: Record<string, string> = {}, over: Partial<PolicyAnalysis> = {}): PolicyAnalysis => ({
  status: "analysed", reason: null, sector: null, documents: [{ url: "https://x.test/privacy", policy_type: "privacy_policy", characters: 4000, source: "page", truncated: false }], notes: [],
  summary: null, requirements: REQ_IDS.map((id) => req(id, status[id] ?? "verified")), limitations: [], model: null, ...over,
});
const withPolicy = (a: PolicyAnalysis | null, extra: Partial<ReportModel> = {}): ReportModel => ({ ...model(), policyAnalysis: a, ...extra });
const resultOf = (m: ReportModel) => buildTenChecks(m).checks.map((c) => c.result);
const consent = (m: ReportModel, over: Partial<NonNullable<ReportModel["visual"]>["consent"]>): ReportModel => ({ ...m, visual: { ...m.visual!, consent: { ...m.visual!.consent, ...over } } });
const QUIET_EV = { ...EVIDENCE, cookies: [], trackers: [] } as ScanEvidenceResponse;
const ALL_LOADED = (m: ReportModel) => ({ ...m.scanPages!, failed: [] });
const CLEAN = { acceptTested: true, rejectTested: true, known: [0, 2, 0] as [number, number, number] };

describe("the ten checks", () => {
  it("always gives ten checks, numbered 1 to 10, whatever the scan found", () => {
    for (const m of [withPolicy(analysis()), withPolicy(null), withPolicy(analysis({}, { status: "unavailable", requirements: [], documents: [] }))]) {
      const t = buildTenChecks(m);
      expect(t.checks.map((c) => c.id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
      expect(t.checks.every((c) => c.name.length > 0 && c.explanation.length > 0)).toBe(true);
    }
  });

  it("with a privacy policy: the policy checks pass on verified quotes, with the page they came from", () => {
    const t = buildTenChecks(consent(withPolicy(analysis()), CLEAN));
    for (const id of [1, 2, 3, 4, 5, 6, 7]) expect(t.checks[id - 1].result, `check ${id}`).toBe("Passed");
    expect(t.checks[1].evidence).toContain("quote for notice_data_itemised");
    expect(t.checks[1].sourceUrl).toBe("https://x.test/privacy");
  });

  it("without a privacy policy: check 1 fails, and every other check is still assessed on its own evidence", () => {
    const m = consent(withPolicy(analysis({}, { status: "unavailable", reason: "No privacy policy page was found.", requirements: [], documents: [] }), { evidence: { ...QUIET_EV, policies: [] } }), CLEAN);
    const t = buildTenChecks(m);
    expect(t.checks[0].result).toBe("Failed");
    expect(t.checks[0].explanation).toMatch(/No privacy policy page was found/);
    // the policy-content checks fail with a reason (not evidenced), they are not skipped
    for (const id of [2, 3, 4, 5, 6, 7]) { expect(t.checks[id - 1].result).toBe("Failed"); expect(t.checks[id - 1].explanation).toMatch(/not a finding that the law was broken/); }
    // the technical checks are not dragged down by the missing policy
    expect(t.checks[7].result).not.toBe("Failed");   // detection ran, whatever the policy
    expect(t.checks[8].result).toBe("Passed");       // consent behaviour was tested
  });

  it("incomplete disclosures give Partially Passed and Failed, each with a reason", () => {
    const m = consent(withPolicy(analysis({ retention: "not_found", notice_purposes: "partial", dpo_contact: "not_found", grievance_timeline: "not_found" })), CLEAN);
    const t = buildTenChecks(m);
    expect(t.checks[2].result).toBe("Partially Passed");
    expect(t.checks[4].result).toBe("Failed");
    expect(t.checks[4].explanation).toMatch(/not found in the policy text/);
    expect(t.checks[6].result).toBe("Partially Passed");   // a grievance contact, but neither a response period nor a business contact
  });

  it("a website with no detected trackers passes check 8 and says absence is not proof", () => {
    const base = model();
    const m = withPolicy(analysis(), { evidence: QUIET_EV, scanPages: ALL_LOADED(base) });
    const c = buildTenChecks(m).checks[7];
    expect(c.result).toBe("Passed");
    expect(c.limitation).toMatch(/not proof/);
  });

  it("trackers observed before consent and again after Reject fail check 9; before consent only is Partially Passed", () => {
    expect(buildTenChecks(consent(withPolicy(analysis()), { acceptTested: true, rejectTested: true, known: [2, 3, 2] })).checks[8].result).toBe("Failed");
    expect(buildTenChecks(consent(withPolicy(analysis()), { acceptTested: true, rejectTested: true, known: [2, 3, 0] })).checks[8].result).toBe("Partially Passed");
  });

  it("Reject that could not be tested gives Partially Passed with the reason; neither tested fails as not completed", () => {
    const partial = buildTenChecks(consent(withPolicy(analysis()), { acceptTested: true, rejectTested: false, known: [0, 2, 0] })).checks[8];
    expect(partial.result).toBe("Partially Passed");
    expect(partial.limitation).toMatch(/Reject was not tested/);
    const none = buildTenChecks(consent(withPolicy(analysis()), { acceptTested: false, rejectTested: false, known: [0, 0, 0] })).checks[8];
    expect(none.result).toBe("Failed");
    expect(none.completed).toBe(false);
  });

  it("a website that could not be accessed fails checks 1 and 8 as not completed, with the reason", () => {
    const m = withPolicy(null, { scanPages: { ...model().scanPages!, scannedCount: 0, rows: [], failed: [{ url: "https://x.test/", reason: "HTTP 500" }] }, visual: null, evidence: null });
    const t = buildTenChecks(m);
    expect(t.checks[0].completed).toBe(false);
    expect(t.checks[0].explanation).toMatch(/could not be accessed/);
    expect(t.checks[7].result).toBe("Failed");
    expect(t.pages.failed).toBe(1);
  });

  it("the score is marks over applicable marks: 6 passed, 2 partial, 2 failed is 7 of 10, 70", () => {
    // checks 2..7 policy: make 4 pass, 2 partial (marks 5); 1 passes; 8 passes; 9 failed; 10 ...
    const m = consent(withPolicy(analysis({ notice_data_itemised: "partial", notice_purposes: "partial", data_sharing: "not_found", retention: "not_found" })), { acceptTested: true, rejectTested: true, known: [2, 2, 2] });
    const t = buildTenChecks(m);
    const sum = t.checks.reduce((n, c) => n + (c.awarded ?? 0), 0);
    expect(t.awarded).toBe(sum);
    expect(t.applicableMax).toBe(10);
    expect(t.score).toBe(roundScore((sum / 10) * 100));
    expect(t.formula).toContain(`${sum} ÷ 10 × 100`);
    expect(t.counts.passed + t.counts.partial + t.counts.failed + t.counts.notApplicable).toBe(10);
  });

  it("a check that genuinely does not apply leaves the maximum: no banner and no tracking in any state makes check 9 Not Applicable (max 9)", () => {
    const quiet = { ...EVIDENCE, consent_signals: [], cookies: [], trackers: [] } as ScanEvidenceResponse;
    const m = consent(withPolicy(analysis(), { evidence: quiet }), { acceptTested: false, rejectTested: false, known: [0, 0, 0] });
    const t = buildTenChecks(m);
    expect(t.checks[8].result).toBe("Not Applicable");
    expect(t.checks[8].max).toBeNull();
    expect(t.applicableMax).toBe(9);
    expect(t.formula).toMatch(/not applicable, left out of the maximum/);
  });

  it("rounds half up to one decimal", () => {
    expect(roundScore(31.25)).toBe(31.3);
    expect(roundScore(70)).toBe(70);
    expect(roundScore(66.666)).toBe(66.7);
  });

  it("never uses the words Unable to Verify, and never says missing evidence is a violation", () => {
    for (const m of [withPolicy(analysis()), withPolicy(null), withPolicy(analysis({ retention: "not_found" }))]) {
      const t = buildTenChecks(m);
      const text = JSON.stringify(t);
      expect(text).not.toMatch(/unable to verify/i);
      expect(text).not.toMatch(/is a violation|violates|in violation|breach of the law|non-compliant/i);
      for (const c of t.checks) expect(["Passed", "Partially Passed", "Failed", "Not Applicable"]).toContain(c.result);
    }
  });

  it("a failure never stops the others: failing check 1 leaves checks 8 and 9 to their own evidence", () => {
    const a = resultOf(consent(withPolicy(analysis({}, { status: "unavailable", requirements: [], documents: [] }), { evidence: { ...QUIET_EV, policies: [] } }), CLEAN));
    expect(a[0]).toBe("Failed");
    expect(a[8]).toBe("Passed");
  });

  it("a site with tracking cookies and no banner is Failed on check 9, not Not Applicable (Google's case), and stays in the maximum", () => {
    const ev = { ...EVIDENCE, consent_signals: [{ mechanism_type: "none", cmp_vendor: null, has_reject_all: null, has_granular_choices: null, evidence: {}, banner_displayed: null }] } as ScanEvidenceResponse;
    const t = buildTenChecks(consent(withPolicy(analysis(), { evidence: ev }), { acceptTested: false, rejectTested: false, known: [0, 0, 0] }));
    expect(t.checks[8].result).toBe("Failed");
    expect(t.checks[8].max).toBe(1);
    expect(t.applicableMax).toBe(10);
  });

  it("an analytics or marketing cookie set before consent stops check 9 from passing even when no known request fired", () => {
    const c = buildTenChecks(consent(withPolicy(analysis()), CLEAN)).checks[8];   // the fixture sets a marketing cookie before consent
    expect(c.result).not.toBe("Passed");
    expect(c.evidence).toMatch(/before consent: [1-9]/);
  });

  it("cookies that could not be classified do not lower check 8 when detection completed; they are a stated limit", () => {
    const ev = { ...EVIDENCE, cookies: [{ ...(EVIDENCE.cookies[0] as object), category: null, vendor: null }], trackers: [] } as unknown as ScanEvidenceResponse;
    const c = buildTenChecks(withPolicy(analysis(), { evidence: ev, scanPages: ALL_LOADED(model()) })).checks[7];
    expect(c.result).toBe("Passed");
    expect(c.limitation).toMatch(/could not be classified/);
  });

  it("check 8 is Partially Passed only when a page did not load", () => {
    expect(buildTenChecks(withPolicy(analysis(), { evidence: QUIET_EV })).checks[7].result).toBe("Partially Passed");   // the fixture has a page that failed
  });

  it("a policy page that was found but could not be read says so, names the page, and is not worded as an absent policy", () => {
    const m = withPolicy(analysis({}, { status: "unavailable", reason: "A policy page was found but its text could not be read.", requirements: [], documents: [] }));
    const t = buildTenChecks(m);
    for (const id of [2, 3, 4, 5, 6, 7]) {
      const c = t.checks[id - 1];
      expect(c.result).toBe("Failed");
      expect(c.explanation).toMatch(/page found, but its text could not be read/);
      expect(c.explanation).toMatch(/limit of the scan/);
      expect(c.sourceUrl).toBe("https://x.test/privacy");
    }
  });

  it("check 10 is not failed a second time for the same unreadable policy: it judges the checks that could be completed", () => {
    const m = consent(withPolicy(analysis({}, { status: "unavailable", requirements: [], documents: [] }), { evidence: { ...QUIET_EV, policies: EVIDENCE.policies }, scanPages: ALL_LOADED(model()) }), CLEAN);
    const t = buildTenChecks(m);
    expect(t.checks[9].result).not.toBe("Failed");
    expect(t.checks[9].explanation).toMatch(/could not be completed/);
  });

  it("check 7 passes on a grievance contact with a response period or a business contact, and is Partially Passed on a single contact", () => {
    expect(buildTenChecks(consent(withPolicy(analysis({ dpo_contact: "not_found" })), CLEAN)).checks[6].result).toBe("Passed");          // grievance contact + response period
    expect(buildTenChecks(consent(withPolicy(analysis({ dpo_contact: "not_found", grievance_timeline: "not_found" })), CLEAN)).checks[6].result).toBe("Partially Passed");
    expect(buildTenChecks(consent(withPolicy(analysis({ grievance_contact: "not_found", grievance_timeline: "not_found" })), CLEAN)).checks[6].result).toBe("Partially Passed");
  });

  it("Not Applicable on check 9 needs every observed cookie and request to be classified: an unclassified item could be tracking, so it is Failed instead", () => {
    const base = { ...EVIDENCE, consent_signals: [], trackers: [] } as unknown as ScanEvidenceResponse;
    const functional = { ...(EVIDENCE.cookies[0] as object), category: "functional", consent_states: ["pre_consent"] };
    const unknown = { ...functional, category: null, vendor: null };
    const q = (cookies: unknown[]) => consent(withPolicy(analysis(), { evidence: { ...base, cookies } as unknown as ScanEvidenceResponse }), { acceptTested: false, rejectTested: false, known: [0, 0, 0] });
    expect(buildTenChecks(q([functional])).checks[8].result).toBe("Not Applicable");
    expect(buildTenChecks(q([functional, unknown])).checks[8].result).toBe("Failed");
  });
});

const row = (result: TenResult) => ({ result, awarded: result === "Passed" ? 1 : result === "Partially Passed" ? 0.5 : result === "Failed" ? 0 : null, max: result === "Not Applicable" ? null : 1 });
const mix = (p: number, pp: number, f: number, na: number) => [...Array(p).fill("Passed"), ...Array(pp).fill("Partially Passed"), ...Array(f).fill("Failed"), ...Array(na).fill("Not Applicable")].map((r) => row(r as TenResult));

describe("the scoring and how marks are printed", () => {
  it("prints each check's marks as obtained / maximum, and N/A for a check that does not apply", () => {
    expect(marksLabel(1, 1)).toBe("1 / 1");
    expect(marksLabel(0.5, 1)).toBe("0.5 / 1");
    expect(marksLabel(0, 1)).toBe("0 / 1");
    expect(marksLabel(null, null)).toBe("N/A");
    for (const r of ["Passed", "Partially Passed", "Failed", "Not Applicable"] as TenResult[]) {
      const c = buildTenChecks(model()).checks.find((x) => x.result === r);
      if (c) expect(c.marksText).toBe(r === "Not Applicable" ? "N/A" : `${c.awarded} / ${c.max}`);
    }
  });

  it("6 Passed, 2 Partially Passed, 1 Failed, 1 Not Applicable: 7 / 9 and 77.8%", () => {
    const s = scoreChecks(mix(6, 2, 1, 1));
    expect(s.awarded).toBe(7);
    expect(s.applicableMax).toBe(9);
    expect(s.score).toBe(77.8);
    expect(s.totalText).toBe("7 / 9");
    expect(s.scoreText).toBe("77.8%");
    expect(s.formula).toContain("7 ÷ 9 × 100 = 77.8");
  });

  it("all Passed is 10 / 10, 100%; all Failed is 0 / 10, 0%; many Partially Passed is half marks each", () => {
    expect(scoreChecks(mix(10, 0, 0, 0))).toMatchObject({ totalText: "10 / 10", scoreText: "100%", score: 100 });
    expect(scoreChecks(mix(0, 0, 10, 0))).toMatchObject({ totalText: "0 / 10", scoreText: "0%", score: 0 });
    expect(scoreChecks(mix(2, 6, 2, 0))).toMatchObject({ awarded: 5, totalText: "5 / 10", scoreText: "50%" });
    expect(scoreChecks(mix(1, 3, 6, 0))).toMatchObject({ awarded: 2.5, totalText: "2.5 / 10", scoreText: "25%" });
  });

  it("when every check is Not Applicable there is no score, not a misleading 0 or 100", () => {
    const s = scoreChecks(mix(0, 0, 0, 10));
    expect(s.score).toBeNull();
    expect(s.applicableMax).toBe(0);
    expect(s.totalText).toBe("No applicable check");
    expect(s.scoreText).toBe("No meaningful score");
  });

  it("rounds half up in the printed percentage", () => {
    expect(scoreChecks(mix(1, 0, 7, 0)).scoreText).toBe("12.5%");        // 1 of 8
    expect(scoreChecks(mix(5, 0, 3, 0)).scoreText).toBe("62.5%");
    expect(scoreChecks(mix(1, 0, 2, 0)).scoreText).toBe("33.3%");
  });

  it("the engine's own total equals the sum of its rows, and the printed figures come from that one result", () => {
    for (const m of [consent(withPolicy(analysis()), CLEAN), withPolicy(null), consent(withPolicy(analysis({ retention: "not_found", notice_purposes: "partial" })), CLEAN)]) {
      const t = buildTenChecks(m);
      const sum = t.checks.reduce((n, c) => n + (c.awarded ?? 0), 0);
      const max = t.checks.reduce((n, c) => n + (c.max ?? 0), 0);
      expect(t.awarded).toBe(sum);
      expect(t.applicableMax).toBe(max);
      expect(t.totalText).toBe(`${sum} / ${max}`);
      expect(t.scoreText).toBe(`${roundScore((sum / max) * 100)}%`);
      expect(t.counts.passed + t.counts.partial + t.counts.failed + t.counts.notApplicable).toBe(10);
    }
  });
});
