import { describe, expect, it } from "vitest";
import type { DpdpScoreResponse, EvidenceCookieItem, PolicyAnalysis, PolicyRequirementResult, ScanEvidenceResponse, ScanScreenshot, WebsiteScanMetadata } from "../api/types";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildReportModel, type ReportModel } from "./buildReport";
import { buildScoreView } from "./dpdpScore";
import { buildTenChecks, KIND_LABEL, marksLabel, roundScore, scoreChecks, type TenResult } from "./tenChecks";

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
const req = (id: string, status: string): PolicyRequirementResult => ({ id, title: id, group: "g", basis: "required", provisions: [], status, quote: status === "not_found" ? "" : id === "data_sharing" ? "We share personal data with analytics providers, payment processors and hosting providers." : id === "retention" ? "We keep personal information for 24 months after our last contact with you." : `quote for ${id}`, source_url: status === "not_found" ? null : "https://x.test/privacy", detail: "", fix: "" });
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
    for (const id of [2, 3, 4, 5, 6, 7]) { expect(t.checks[id - 1].result).toBe("Not Assessed"); expect(t.checks[id - 1].explanation).toMatch(/not a finding that the law was broken/); }
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
    const ga = (states: string[], url = "https://www.google-analytics.com/g/collect?v=2&tid=G-X") => ({ id: "t", script_src: url, vendor: "Google Analytics", category: "analytics", source: "rule", consent_states: states, page_url: "https://x.test/" });
    const ev = (trackers: unknown[]) => ({ ...QUIET_EV, trackers } as unknown as ScanEvidenceResponse);
    const both = buildTenChecks(consent(withPolicy(analysis(), { evidence: ev([ga(["pre_consent", "post_accept", "post_reject"])]) }), CLEAN)).checks[8];
    const preOnly = buildTenChecks(consent(withPolicy(analysis(), { evidence: ev([ga(["pre_consent", "post_accept"])]) }), CLEAN)).checks[8];
    expect(both.result).toBe("Failed");
    expect(preOnly.result).toBe("Partially Passed");
  });

  it("a tag loader before consent sends nothing about the visitor and is not counted as tracking", () => {
    const loader = { id: "l", script_src: "https://www.googletagmanager.com/gtm.js?id=GTM-X", vendor: "Google Tag Manager", category: "analytics", source: "rule", consent_states: ["pre_consent", "post_accept", "post_reject"], page_url: "https://x.test/" };
    const t = buildTenChecks(consent(withPolicy(analysis(), { evidence: { ...QUIET_EV, trackers: [loader] } as unknown as ScanEvidenceResponse }), CLEAN)).checks[8];
    expect(t.result).toBe("Passed");
  });

  it("Google requests made in consent-denied mode (gcs=G100) with no cookies set are Partially Passed, not Failed, and the explanation says they still reach Google (cookieyes.com's case)", () => {
    const ping = (u: string) => ({ id: u, script_src: u, vendor: "Google Analytics", category: "analytics", source: "rule", consent_states: ["pre_consent", "post_accept", "post_reject"], page_url: "https://x.test/" });
    const trackers = [ping("https://www.google-analytics.com/g/collect?v=2&gcs=G100&gcd=13q3q3q3q5l1"), ping("https://pagead2.googlesyndication.com/ccm/collect?rcb=10&gcs=G100")];
    const t = buildTenChecks(consent(withPolicy(analysis(), { evidence: { ...QUIET_EV, trackers } as unknown as ScanEvidenceResponse }), CLEAN)).checks[8];
    expect(t.result).toBe("Partially Passed");
    expect(t.explanation).toMatch(/consent-denied mode/);
    expect(t.explanation).toMatch(/still reach Google/);
    expect(t.explanation).not.toMatch(/violat|breach/i);
  });

  it("consent-denied pings do not excuse an analytics cookie set before consent, or a request sent with storage granted", () => {
    const ping = { id: "p", script_src: "https://www.google-analytics.com/g/collect?v=2&gcs=G100", vendor: "Google Analytics", category: "analytics", source: "rule", consent_states: ["pre_consent", "post_accept", "post_reject"], page_url: "https://x.test/" };
    const cookie = { ...(EVIDENCE.cookies[0] as object), category: "analytics", consent_states: ["pre_consent"] };
    const withCookie = buildTenChecks(consent(withPolicy(analysis(), { evidence: { ...QUIET_EV, trackers: [ping], cookies: [cookie] } as unknown as ScanEvidenceResponse }), CLEAN)).checks[8];
    expect(withCookie.result).not.toBe("Passed");
    expect(withCookie.explanation).not.toMatch(/consent-denied mode/);
    const granted = { ...ping, script_src: "https://www.google-analytics.com/g/collect?v=2&gcs=G111" };
    const g = buildTenChecks(consent(withPolicy(analysis(), { evidence: { ...QUIET_EV, trackers: [granted] } as unknown as ScanEvidenceResponse }), CLEAN)).checks[8];
    expect(g.result).toBe("Failed");
  });

  it("with no request list the summary counts still decide check 9", () => {
    const t = buildTenChecks(consent(withPolicy(analysis(), { evidence: { ...QUIET_EV, trackers: [], cookies: [] } as ScanEvidenceResponse }), { acceptTested: true, rejectTested: true, known: [2, 3, 2] })).checks[8];
    expect(t.result).toBe("Failed");
  });

  it("check 1 names the policy page itself, not a page whose address only contains the words", () => {
    const pol = (url: string) => ({ id: url, policy_type: "privacy_policy", url });
    const m = withPolicy(analysis({}, { documents: [{ url: "https://x.test/privacy-policy-generator/", policy_type: "privacy_policy", characters: 14079, source: "crawl", truncated: false }, { url: "https://x.test/privacy-policy/", policy_type: "privacy_policy", characters: 41974, source: "crawl", truncated: false }] }),
      { evidence: { ...QUIET_EV, policies: [pol("https://x.test/privacy-policy-generator/"), pol("https://x.test/privacy-policy/")] } as unknown as ScanEvidenceResponse });
    const c = buildTenChecks(consent(m, CLEAN)).checks[0];
    expect(c.sourceUrl).toBe("https://x.test/privacy-policy/");
    expect(c.evidence).toMatch(/41,974/);
  });

  it("Reject that could not be tested gives Partially Passed with the reason; neither tested fails as not completed", () => {
    const partial = buildTenChecks(consent(withPolicy(analysis()), { acceptTested: true, rejectTested: false, known: [0, 2, 0] })).checks[8];
    expect(partial.result).toBe("Partially Passed");
    expect(partial.limitation).toMatch(/Reject was not tested/);
    const none = buildTenChecks(consent(withPolicy(analysis()), { acceptTested: false, rejectTested: false, known: [0, 0, 0] })).checks[8];
    expect(none.result).toBe("Not Assessed");
    expect(none.completed).toBe(false);
  });

  it("a website that could not be accessed fails checks 1 and 8 as not completed, with the reason", () => {
    const m = withPolicy(null, { scanPages: { ...model().scanPages!, scannedCount: 0, rows: [], failed: [{ url: "https://x.test/", reason: "HTTP 500" }] }, visual: null, evidence: null });
    const t = buildTenChecks(m);
    expect(t.checks[0].completed).toBe(false);
    expect(t.checks[0].explanation).toMatch(/could not be accessed/);
    expect(t.checks[7].result).toBe("Not Assessed");
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
      for (const c of t.checks) expect(["Passed", "Partially Passed", "Failed", "Not Assessed", "Not Applicable"]).toContain(c.result);
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
      expect(c.result).toBe("Not Assessed");
      expect(c.explanation).toMatch(/page found, but its text could not be read/);
      expect(c.explanation).toMatch(/limit of the scan/);
      expect(c.sourceUrl).toBe("https://x.test/privacy");
    }
  });

  it("check 10 is not failed a second time for the same unreadable policy: it judges the checks that could be completed", () => {
    const m = consent(withPolicy(analysis({}, { status: "unavailable", requirements: [], documents: [] }), { evidence: { ...QUIET_EV, policies: EVIDENCE.policies }, scanPages: ALL_LOADED(model()) }), CLEAN);
    const t = buildTenChecks(m);
    expect(t.checks[9].result).not.toBe("Failed");
    expect(t.checks[9].explanation).toMatch(/could not be assessed/);
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
    expect(s.scoreText).toBe("77.8/100");
    expect(s.formula).toContain("7 ÷ 9 × 100 = 77.8");
  });

  it("all Passed is 10 / 10, 100%; all Failed is 0 / 10, 0%; many Partially Passed is half marks each", () => {
    expect(scoreChecks(mix(10, 0, 0, 0))).toMatchObject({ totalText: "10 / 10", scoreText: "100/100", score: 100 });
    expect(scoreChecks(mix(0, 0, 10, 0))).toMatchObject({ totalText: "0 / 10", scoreText: "0/100", score: 0 });
    expect(scoreChecks(mix(2, 6, 2, 0))).toMatchObject({ awarded: 5, totalText: "5 / 10", scoreText: "50/100" });
    expect(scoreChecks(mix(1, 3, 6, 0))).toMatchObject({ awarded: 2.5, totalText: "2.5 / 10", scoreText: "25/100" });
  });

  it("when every check is Not Applicable there is no score, not a misleading 0 or 100", () => {
    const s = scoreChecks(mix(0, 0, 0, 10));
    expect(s.score).toBeNull();
    expect(s.applicableMax).toBe(0);
    expect(s.totalText).toBe("No applicable check");
    expect(s.scoreText).toBe("No meaningful score");
  });

  it("rounds half up in the printed percentage", () => {
    expect(scoreChecks(mix(1, 0, 7, 0)).scoreText).toBe("12.5/100");        // 1 of 8
    expect(scoreChecks(mix(5, 0, 3, 0)).scoreText).toBe("62.5/100");
    expect(scoreChecks(mix(1, 0, 2, 0)).scoreText).toBe("33.3/100");
  });

  it("the engine's own total equals the sum of its rows, and the printed figures come from that one result", () => {
    for (const m of [consent(withPolicy(analysis()), CLEAN), withPolicy(null), consent(withPolicy(analysis({ retention: "not_found", notice_purposes: "partial" })), CLEAN)]) {
      const t = buildTenChecks(m);
      const sum = t.checks.reduce((n, c) => n + (c.awarded ?? 0), 0);
      const max = t.checks.reduce((n, c) => n + (c.max ?? 0), 0);
      expect(t.awarded).toBe(sum);
      expect(t.applicableMax).toBe(max);
      expect(t.totalText).toBe(`${sum} / ${max}`);
      expect(t.scoreText).toBe(`${roundScore((sum / max) * 100)}/100`);
      expect(t.counts.passed + t.counts.partial + t.counts.failed + t.counts.notAssessed + t.counts.notApplicable).toBe(10);
    }
  });
});

describe("the scoring scenarios and boundaries", () => {
  it("scenario A to F: 10 passed, 10 failed, 5+5, 6/2/1/1, 4/3/2/1, and nothing applicable", () => {
    expect(scoreChecks(mix(10, 0, 0, 0))).toMatchObject({ totalText: "10 / 10", scoreText: "100/100" });
    expect(scoreChecks(mix(0, 0, 10, 0))).toMatchObject({ totalText: "0 / 10", scoreText: "0/100" });
    expect(scoreChecks(mix(5, 5, 0, 0))).toMatchObject({ totalText: "7.5 / 10", scoreText: "75/100" });
    expect(scoreChecks(mix(6, 2, 1, 1))).toMatchObject({ totalText: "7 / 9", scoreText: "77.8/100" });
    expect(scoreChecks(mix(4, 3, 2, 1))).toMatchObject({ totalText: "5.5 / 9", scoreText: "61.1/100" });
    expect(scoreChecks(mix(0, 0, 0, 10))).toMatchObject({ score: null, scoreText: "No meaningful score" });
    expect(scoreChecks([])).toMatchObject({ score: null });   // no checks at all: no division by zero
  });

  it("floating-point and rounding boundaries", () => {
    expect(scoreChecks(mix(2, 0, 1, 0)).scoreText).toBe("66.7/100");
    expect(scoreChecks(mix(1, 0, 2, 0)).scoreText).toBe("33.3/100");
    expect(scoreChecks(mix(0, 3, 0, 0)).scoreText).toBe("50/100");
    expect(scoreChecks(mix(0, 1, 7, 2)).scoreText).toBe("6.3/100");   // 0.5 of 8 is 6.25, rounded half up
  });

  it("every check carries a kind, and technical checks only say they relate to a section", () => {
    const t = buildTenChecks(consent(withPolicy(analysis()), CLEAN));
    expect(t.checks.map((c) => c.kind)).toEqual(["dpdp", "dpdp", "dpdp", "practice", "practice", "dpdp", "dpdp", "technical", "technical", "transparency"]);
    expect(t.checks[7].legal).toMatch(/^Relates to/);
    expect(t.checks[8].legal).toMatch(/^Relates to/);
    expect(t.checks[9].legal).toBeNull();
    expect(KIND_LABEL.practice).toBe("Best practice");
  });

  it("not found, not tested, evidence unavailable and scan failed are kept apart", () => {
    const base = model();
    const noPolicy = buildTenChecks(consent(withPolicy(analysis({}, { status: "unavailable", requirements: [], documents: [] }), { evidence: { ...QUIET_EV, policies: [] }, scanPages: ALL_LOADED(base) }), CLEAN));
    expect(noPolicy.checks[0].basis).toBe("Not found in scanned pages");
    expect(noPolicy.checks[1].basis).toBe("Not evidenced");
    const unreadable = buildTenChecks(consent(withPolicy(analysis({}, { status: "unavailable", requirements: [], documents: [] }), { scanPages: ALL_LOADED(base) }), CLEAN));
    expect(unreadable.checks[1].basis).toBe("Content extraction failed");
    const untested = buildTenChecks(consent(withPolicy(analysis()), { acceptTested: false, rejectTested: false, known: [0, 0, 0] }));
    expect(untested.checks[8].basis).toBe("Not tested");
    const down = buildTenChecks(withPolicy(null, { evidence: null, visual: null, scanPages: { ...base.scanPages!, scannedCount: 0, rows: [], failed: [] } }));
    expect(down.checks[0].basis).toBe("Scan failed");
    expect(down.checks[7].basis).toBe("Scan failed");
    expect(down.checks[9].basis).toBe("Evidence unavailable");
    expect(buildTenChecks(consent(withPolicy(analysis()), CLEAN)).checks[0].basis).toBe("Verified");
  });

  it("a privacy page the scan found a link to but could not load is not reported as a missing policy", () => {
    const base = model();
    const m = consent(withPolicy(analysis({}, { status: "unavailable", requirements: [], documents: [] }), { evidence: { ...QUIET_EV, policies: [] }, scanPages: { ...base.scanPages!, failed: [{ url: "https://x.test/privacy-policy", reason: "HTTP 403" }] } }), CLEAN);
    const t = buildTenChecks(m);
    expect(t.checks[0].result).toBe("Not Assessed");
    expect(t.checks[0].basis).toBe("Crawling restricted");     // a 403 is the site refusing access, which is not a missing policy
    expect(t.checks[0].explanation).toMatch(/does not allow it to be scanned \(HTTP 403\)/);
    expect(t.checks[0].explanation).toMatch(/not a finding that the policy is missing/);
    expect(t.checks[0].explanation).not.toMatch(/No privacy policy page was found/);
    expect(t.checks[1].sourceUrl).toBe("https://x.test/privacy-policy");
    expect(t.checks[1].basis).toBe("Crawling restricted");       // the checks that read the policy carry the same reason
  });

  it("invalid evidence: a requirement marked verified with no quoted passage is not counted as a pass", () => {
    const a = analysis();
    a.requirements = a.requirements.map((r) => (r.id === "retention" || r.id === "notice_purposes" ? { ...r, quote: "  " } : r));
    const t = buildTenChecks(consent(withPolicy(a), CLEAN));
    expect(t.checks[2].result).toBe("Not Assessed");
    expect(t.checks[4].result).toBe("Not Assessed");
    expect(t.checks[4].basis).toBe("Evidence unavailable");
    expect(t.checks[1].result).toBe("Passed");
  });

  it("an analysis that ran but returned no requirements is a failure to analyse, never a pass", () => {
    const t = buildTenChecks(consent(withPolicy(analysis({}, { requirements: [] })), CLEAN));
    for (const id of [2, 3, 4, 5, 6, 7]) expect(t.checks[id - 1].result).toBe("Not Assessed");
  });

  it("the same passage quoted for every requirement does not raise the total above the maximum", () => {
    const a = analysis();
    a.requirements = a.requirements.map((r) => ({ ...r, quote: "one identical passage", source_url: "https://x.test/privacy" }));
    const t = buildTenChecks(consent(withPolicy(a), CLEAN));
    expect(t.awarded).toBeLessThanOrEqual(t.applicableMax);
    expect(t.applicableMax).toBe(10);
  });

  it("a missing policy analysis (null) fails checks 2 to 7 with a reason and does not throw", () => {
    const t = buildTenChecks(withPolicy(null));
    for (const id of [2, 3, 4, 5, 6, 7]) { expect(t.checks[id - 1].result).toBe("Not Assessed"); expect(t.checks[id - 1].explanation.length).toBeGreaterThan(10); }
  });

  it("failed pages: check 8 is Passed with none and Partially Passed with one (boundary)", () => {
    const base = model();
    const none = buildTenChecks(withPolicy(analysis(), { evidence: QUIET_EV, scanPages: { ...base.scanPages!, failed: [] } })).checks[7];
    const one = buildTenChecks(withPolicy(analysis(), { evidence: QUIET_EV, scanPages: { ...base.scanPages!, failed: [{ url: "https://x.test/a", reason: "HTTP 404" }] } })).checks[7];
    expect(none.result).toBe("Passed");
    expect(one.result).toBe("Partially Passed");
  });

  it("a failed page is never described as proof that a disclosure does not exist", () => {
    const base = model();
    const t = buildTenChecks(withPolicy(analysis(), { evidence: QUIET_EV, scanPages: { ...base.scanPages!, failed: [{ url: "https://x.test/a", reason: "HTTP 404" }] } }));
    expect(JSON.stringify(t)).not.toMatch(/proves|proof that the (policy|disclosure)|non-compliant|violat(es|ion of)/i);
  });
});

const CMP = [{ mechanism_type: "cmp", cmp_vendor: "Some CMP", has_reject_all: true, has_granular_choices: true, evidence: {}, banner_displayed: true }];
const NOT_READ = () => analysis({}, { status: "unavailable", requirements: [], documents: [] });
const failedAt = (url: string, reason: string) => ({ ...model().scanPages!, failed: [{ url, reason }] });
const noPolicyEv = { ...QUIET_EV, policies: [] } as ScanEvidenceResponse;

describe("discovery and extraction states, one per situation", () => {
  it("found and extracted: Passed, Verified, with the readable length and the page", () => {
    const c = buildTenChecks(consent(withPolicy(analysis()), CLEAN)).checks[0];
    expect([c.result, c.basis]).toEqual(["Passed", "Verified"]);
    expect(c.evidence).toMatch(/characters of text read/);
    expect(c.sourceUrl).toBe("https://x.test/privacy");
  });

  it("found but the text was cut at the storage limit: Passed with Partially extracted and a limit stated", () => {
    const a = analysis({}, { documents: [{ url: "https://x.test/privacy", policy_type: "privacy_policy", characters: 60000, source: "crawl", truncated: true }] });
    const c = buildTenChecks(consent(withPolicy(a), CLEAN)).checks[0];
    expect([c.result, c.basis]).toEqual(["Passed", "Partially extracted"]);
    expect(c.limitation).toMatch(/first part of a very long policy/);
  });

  it("a disclosure not found in a policy page that was cut is Not Assessed, never Failed: what came after the cut was not read", () => {
    const a = analysis({ retention: "not_found" }, { documents: [{ url: "https://x.test/privacy", policy_type: "privacy_policy", characters: 60000, source: "crawl", truncated: true }] });
    const c = buildTenChecks(consent(withPolicy(a), CLEAN)).checks[4];
    expect([c.result, c.basis, c.awarded, c.max]).toEqual(["Not Assessed", "Partially extracted", null, null]);
    expect(c.explanation).toMatch(/first 60,000 characters of x\.test\/privacy/);
    expect(c.explanation).toMatch(/the rest was not reviewed, so this is not assessed/);
    expect(c.explanation).not.toMatch(/storage limit/);
  });

  it("a long page that only mentions a policy being cut does not make a policy disclosure Not Assessed (the 7,808-character policy that was reported as cut)", () => {
    const a = analysis({ data_sharing: "not_found", retention: "not_found" }, { documents: [
      { url: "https://x.test/privacy", policy_type: "privacy_policy", characters: 7808, source: "crawl", truncated: false },
      { url: "https://x.test/training/dpdp", policy_type: "privacy_policy", characters: 30000, source: "crawl", truncated: true },
    ] });
    const t = buildTenChecks(consent(withPolicy(a), CLEAN));
    for (const i of [3, 4]) {
      expect(t.checks[i].result).toBe("Failed");                        // a plain not-found: the policy was read in full
      expect(t.checks[i].basis).toBe("Not evidenced");
      expect(t.checks[i].explanation).not.toMatch(/cut|storage limit|first .* characters/);
    }
    expect(t.counts.notAssessed).toBe(0);
  });

  it("the same applies to the rights and contact checks: a cut policy page makes a missing statement Not Assessed", () => {
    const docs = [{ url: "https://x.test/privacy-policy", policy_type: "privacy_policy", characters: 60000, source: "crawl", truncated: true }];
    const a = analysis({ rights_means: "not_found", rights_access: "not_found", rights_correction_erasure: "not_found", rights_nominate: "not_found", grievance_contact: "not_found", dpo_contact: "not_found", grievance_timeline: "not_found" }, { documents: docs });
    const t = buildTenChecks(consent(withPolicy(a), CLEAN));
    expect([t.checks[5].result, t.checks[6].result]).toEqual(["Not Assessed", "Not Assessed"]);
    expect(t.checks[5].sourceUrl).toBe("https://x.test/privacy-policy");
  });

  it("a cookie policy page that was cut counts the same as a privacy policy page", () => {
    const a = analysis({ data_sharing: "not_found" }, { documents: [{ url: "https://x.test/cookie-policy", policy_type: "cookie_policy", characters: 60000, source: "crawl", truncated: true }] });
    expect(buildTenChecks(consent(withPolicy(a), CLEAN)).checks[3].result).toBe("Not Assessed");
  });

  it("a verified requirement is unaffected by a cut policy page (what was found stays found)", () => {
    const a = analysis({}, { documents: [{ url: "https://x.test/privacy", policy_type: "privacy_policy", characters: 60000, source: "crawl", truncated: true }] });
    expect(buildTenChecks(consent(withPolicy(a), CLEAN)).checks[4].result).toBe("Passed");
  });

  it("found but no readable text: Partially Passed with Content extraction failed, never Not found", () => {
    const c = buildTenChecks(consent(withPolicy(NOT_READ()), CLEAN)).checks[0];
    expect([c.result, c.basis]).toEqual(["Partially Passed", "Content extraction failed"]);
    expect(c.explanation).toMatch(/could not be read/);
  });

  it("found but the analysis never ran: Assessment incomplete, and the policy checks do not say the policy lacks anything", () => {
    const t = buildTenChecks(consent(withPolicy(analysis({}, { status: "not_run", requirements: [], documents: [] })), CLEAN));
    expect(t.checks[0].basis).toBe("Assessment incomplete");
    for (const id of [2, 3, 4, 5, 6, 7]) { expect(t.checks[id - 1].basis).toBe("Assessment incomplete"); expect(t.checks[id - 1].explanation).toMatch(/has not run/); expect(t.checks[id - 1].explanation).not.toMatch(/not found|could not be read/i); }
    expect(t.incomplete).toBeGreaterThanOrEqual(6);
  });

  it("URL discovered but the page failed with a server error: Access failed", () => {
    const m = consent(withPolicy(NOT_READ(), { evidence: noPolicyEv, scanPages: failedAt("https://x.test/privacy-policy", "HTTP 500") }), CLEAN);
    const c = buildTenChecks(m).checks[0];
    expect([c.result, c.basis]).toEqual(["Not Assessed", "Access failed"]);
    expect(c.explanation).toMatch(/did not load \(HTTP 500\)/);
  });

  it("URL discovered but blocked by robots.txt (from the failed list or the page record): Crawling restricted, not a missing policy", () => {
    const fromList = buildTenChecks(consent(withPolicy(NOT_READ(), { evidence: noPolicyEv, scanPages: failedAt("https://x.test/privacy", "Blocked by robots.txt") }), CLEAN)).checks[0];
    expect(fromList.basis).toBe("Crawling restricted");
    const pages = [...(noPolicyEv.pages ?? []), { id: "r", url: "https://x.test/legal/privacy", title: null, http_status: null, discovered_via: "robots_disallowed" }];
    const fromPage = buildTenChecks(consent(withPolicy(NOT_READ(), { evidence: { ...noPolicyEv, pages } as ScanEvidenceResponse, scanPages: { ...model().scanPages!, failed: [] } }), CLEAN)).checks[0];
    expect(fromPage.basis).toBe("Crawling restricted");
    expect(fromPage.explanation).toMatch(/not a finding that the policy is missing/);
  });

  it("no candidate in the pages searched: Not found in scanned pages, said to be limited to those pages", () => {
    const m = consent(withPolicy(NOT_READ(), { evidence: noPolicyEv, scanPages: { ...model().scanPages!, failed: [] } }), CLEAN);
    const c = buildTenChecks(m).checks[0];
    expect([c.result, c.basis]).toEqual(["Failed", "Not found in scanned pages"]);
    expect(c.explanation).toMatch(/limited to the pages actually searched/);
  });

  it("the site could not be reached at all: Scan failed, not a missing policy", () => {
    const m = withPolicy(null, { evidence: null, visual: null, scanPages: { ...model().scanPages!, scannedCount: 0, rows: [], failed: [] } });
    expect(buildTenChecks(m).checks[0].basis).toBe("Scan failed");
  });

  it("policy text read and the disclosure not in it: Not evidenced, and it is a completed check, not an incomplete one", () => {
    const t = buildTenChecks(consent(withPolicy(analysis({ retention: "not_found" })), CLEAN));
    expect([t.checks[4].result, t.checks[4].basis, t.checks[4].completed]).toEqual(["Failed", "Not evidenced", true]);
    expect(t.incomplete).toBe(0);
  });

  it("the number of checks that scored 0 only because they could not be completed is counted apart from disclosures that were not found", () => {
    const t = buildTenChecks(consent(withPolicy(NOT_READ(), { evidence: noPolicyEv, scanPages: { ...model().scanPages!, failed: [] } }), CLEAN));
    expect(t.incomplete).toBe(t.checks.filter((c) => c.result === "Not Assessed").length);
    expect(t.incomplete).toBeGreaterThan(0);
  });
});

// ---- 100 varied scenarios through the whole engine: invariants, not one example repeated ------------------------------------------
function rng(seed: number) { let s = seed >>> 0; return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; }; }

describe("100 varied scenarios keep every invariant", () => {
  const STATUSES = ["verified", "partial", "not_found", "verified"];
  it("runs 100 different combinations of policy, extraction, scan, tracker and consent states", () => {
    const base = model();
    for (let i = 0; i < 100; i++) {
      const r = rng(1000 + i), pick = <T,>(xs: T[]) => xs[Math.floor(r() * xs.length)];
      const policyKind = pick(["read", "read", "not_run", "unavailable", "empty", "null"]);
      const status: Record<string, string> = {};
      for (const id of REQ_IDS) status[id] = pick(STATUSES);
      const a = policyKind === "null" ? null : policyKind === "read" ? analysis(status, { documents: [{ url: "https://x.test/privacy", policy_type: "privacy_policy", characters: 5000 + Math.floor(r() * 60000), source: "crawl", truncated: r() < 0.25 }] })
        : policyKind === "empty" ? analysis(status, { requirements: [] }) : analysis({}, { status: policyKind, requirements: [], documents: [] });
      if (a && policyKind === "read" && r() < 0.2) a.requirements = a.requirements.map((q) => (r() < 0.5 ? { ...q, quote: "" } : q));
      const failed = Array.from({ length: Math.floor(r() * 3) }, (_, k) => ({ url: pick(["https://x.test/privacy", "https://x.test/a", "https://x.test/b"]) + k, reason: pick(["HTTP 404", "HTTP 500", "HTTP 403", "Blocked by robots.txt", "timeout"]) }));
      const scanned = r() < 0.1 ? 0 : 1 + Math.floor(r() * 5);
      const cookies = r() < 0.5 ? [] : [{ ...(EVIDENCE.cookies[0] as object), category: pick(["analytics", "marketing", "functional", null]), consent_states: pick([["pre_consent"], ["post_accept"], ["pre_consent", "post_reject"]]) }];
      const trackers = r() < 0.5 ? [] : [{ id: "t", script_src: pick(["https://www.google-analytics.com/g/collect?gcs=G100", "https://www.google-analytics.com/g/collect", "https://www.googletagmanager.com/gtm.js?id=X", "https://cdn.x.test/a.js"]), vendor: "V", category: pick(["analytics", "marketing", null]), source: "rule", consent_states: pick([["pre_consent", "post_reject"], ["post_accept"], ["pre_consent", "post_accept", "post_reject"]]), page_url: r() < 0.5 ? "https://x.test/" : null }];
      const ev = r() < 0.08 ? null : { ...EVIDENCE, cookies, trackers, policies: r() < 0.7 ? EVIDENCE.policies : [], consent_signals: r() < 0.7 ? CMP : [] } as unknown as ScanEvidenceResponse;
      const m0 = { ...base, policyAnalysis: a, evidence: ev, scanPages: { ...base.scanPages!, scannedCount: scanned, failed } } as ReportModel;
      const m = r() < 0.1 ? { ...m0, visual: null } as ReportModel : consent(m0, { acceptTested: r() < 0.7, rejectTested: r() < 0.7, known: [Math.floor(r() * 3), Math.floor(r() * 5), Math.floor(r() * 3)] as [number, number, number] });
      const t = buildTenChecks(m);

      expect(t.checks.map((c) => c.id), `run ${i}`).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
      for (const c of t.checks) {
        expect(["Passed", "Partially Passed", "Failed", "Not Assessed", "Not Applicable"]).toContain(c.result);
        expect(c.marksText).toBe(c.result === "Not Applicable" ? "N/A" : c.result === "Not Assessed" ? "Not assessed" : `${c.awarded} / 1`);
        expect(c.explanation.length).toBeGreaterThan(5);
        if (c.result === "Passed") { expect(c.basis).toMatch(/^(Verified|Partially extracted)$/); expect(c.evidence !== "" || c.sourceUrl !== null).toBe(true); }
        if (c.result === "Not Assessed") expect(["Evidence unavailable", "Scan failed", "Not evidenced", "Assessment incomplete", "Content extraction failed", "Access failed", "Crawling restricted", "Not tested", "Not found in scanned pages"]).toContain(c.basis);
        if (c.basis === "Verified") expect(c.result).toBe("Passed");
      }
      const sum = t.checks.reduce((n, c) => n + (c.awarded ?? 0), 0), max = t.checks.reduce((n, c) => n + (c.max ?? 0), 0);
      expect(t.awarded).toBe(sum);
      expect(t.applicableMax).toBe(max);
      expect(t.applicableMax).toBe(10 - t.counts.notAssessed - t.counts.notApplicable);   // a check that could not be assessed, or does not apply, is left out of the maximum
      expect(t.totalText).toBe(max === 0 ? "No applicable check" : `${sum} / ${max}`);
      expect(t.score).toBe(max === 0 ? null : roundScore((sum / max) * 100));
      expect(t.counts.passed + t.counts.partial + t.counts.failed + t.counts.notAssessed + t.counts.notApplicable).toBe(10);
      expect(t.incomplete).toBe(t.counts.notAssessed);
      expect(t.summary.length).toBeGreaterThan(20);
      const text = JSON.stringify(t);
      expect(text).not.toMatch(/unable to verify/i);
      expect(text).not.toMatch(/is a violation|violates|in violation|breach of the law|non-compliant|proof that the (policy|disclosure)/i);
      // an extraction failure or an access restriction is never worded as a missing disclosure
      for (const c of t.checks) if (["Content extraction failed", "Access failed", "Crawling restricted", "Assessment incomplete", "Scan failed"].includes(c.basis)) expect(c.explanation).not.toMatch(/was not found in the policy text that was read/);
    }
  });

  it("gives the same answer when run again on the same input (deterministic, no state kept between runs)", () => {
    const m = consent(withPolicy(analysis({ retention: "not_found", notice_purposes: "partial" })), CLEAN);
    const first = JSON.stringify(buildTenChecks(m));
    for (let i = 0; i < 100; i++) expect(JSON.stringify(buildTenChecks(m))).toBe(first);
  });
});

describe("a blocked or failed policy page is carried through to the checks that read it", () => {
  it("checks 2 to 7 say Crawling restricted when the policy page is blocked, and Access failed when it did not load", () => {
    const blocked = buildTenChecks(consent(withPolicy(NOT_READ(), { evidence: noPolicyEv, scanPages: failedAt("https://x.test/privacy", "Blocked by robots.txt") }), CLEAN));
    const failed = buildTenChecks(consent(withPolicy(NOT_READ(), { evidence: noPolicyEv, scanPages: failedAt("https://x.test/privacy", "HTTP 500") }), CLEAN));
    for (const id of [2, 3, 4, 5, 6, 7]) {
      expect(blocked.checks[id - 1].basis).toBe("Crawling restricted");
      expect(blocked.checks[id - 1].explanation).toMatch(/does not allow it to be scanned/);
      expect(failed.checks[id - 1].basis).toBe("Access failed");
      expect(failed.checks[id - 1].explanation).toMatch(/did not load/);
    }
  });
});

// ---- evidence quality, Not Assessed, traceability and recommendations ---------------------------------------------------------------
describe("evidence quality: a passage that exists is not always a disclosure", () => {
  const quoteFor = (reqId: string, quote: string) => { const a = analysis(); a.requirements = a.requirements.map((r) => (r.id === reqId ? { ...r, quote } : r)); return a; };
  it("a retention period for one kind of data only is Partially Passed, with the gap stated; a period for personal data is Passed", () => {
    const one = buildTenChecks(consent(withPolicy(quoteFor("retention", "Contact form submissions are retained for 24 months.")), CLEAN)).checks[4];
    expect([one.result, one.quality, one.coverage]).toEqual(["Partially Passed", "Limited", "One kind of data only"]);
    expect(one.explanation).toMatch(/one kind of data only/);
    const all = buildTenChecks(consent(withPolicy(quoteFor("retention", "We keep your personal data only as long as needed, and for no more than 24 months after the last contact.")), CLEAN)).checks[4];
    expect(all.result).toBe("Passed");
  });
  it("a sharing statement that names no kind of recipient is Partially Passed; one that names recipients is Passed", () => {
    const vague = buildTenChecks(consent(withPolicy(quoteFor("data_sharing", "We may share information with trusted service providers who assist us in operating our website, subject to confidentiality.")), CLEAN)).checks[3];
    expect([vague.result, vague.quality, vague.coverage]).toEqual(["Partially Passed", "General", "Recipients not named"]);
    expect(vague.explanation).toMatch(/names no kind of recipient/);
    const named = buildTenChecks(consent(withPolicy(quoteFor("data_sharing", "We share data with analytics and advertising partners and with payment processors.")), CLEAN)).checks[3];
    expect(named.result).toBe("Passed");
  });
  it("when trackers were observed, the vague sharing finding names them", () => {
    const vague = quoteFor("data_sharing", "We may share information with trusted service providers.");
    const c = buildTenChecks(consent(withPolicy(vague), CLEAN)).checks[3];
    expect(c.explanation).toMatch(/The scan observed .*Google/);
  });
  it("user rights are Passed only when at least two rights are named with the means; one right and the means is Partially Passed", () => {
    expect(buildTenChecks(consent(withPolicy(analysis()), CLEAN)).checks[5].result).toBe("Passed");
    const one = buildTenChecks(consent(withPolicy(analysis({ rights_correction_erasure: "not_found", rights_nominate: "not_found" })), CLEAN)).checks[5];
    expect([one.result, one.coverage]).toEqual(["Partially Passed", "1 of 3 rights named"]);
    const means = buildTenChecks(consent(withPolicy(analysis({ rights_access: "not_found", rights_correction_erasure: "not_found", rights_nominate: "not_found" })), CLEAN)).checks[5];
    expect(means.result).toBe("Partially Passed");
  });
  it("evidence shown for rights is the passage that names a right, not the contact line", () => {
    const a = analysis();
    a.requirements = a.requirements.map((r) => (r.id === "rights_means" ? { ...r, quote: "To exercise your rights, write to us." } : r.id === "rights_access" ? { ...r, quote: "You may ask for a copy of your personal data." } : r));
    expect(buildTenChecks(consent(withPolicy(a), CLEAN)).checks[5].evidence).toMatch(/copy of your personal data/);
  });
  it("consent behaviour that Passed says it was tested on the homepage only; detection says how many pages it covers", () => {
    const t = buildTenChecks(consent(withPolicy(analysis(), { evidence: QUIET_EV }), CLEAN));
    expect(t.checks[8].result).toBe("Passed");
    expect([t.checks[8].quality, t.checks[8].coverage]).toEqual(["Limited", "Homepage only"]);
    expect(t.checks[8].limitation).toMatch(/tested once, on the homepage/);
    expect(t.checks[7].limitation).toMatch(/3 of 40 pages/);
  });
});

describe("Not Assessed is left out of the score; it is never scored 0", () => {
  it("a check that could not be run is Not Assessed with no marks, and the maximum shrinks by one", () => {
    const t = buildTenChecks(withPolicy(null));
    const na = t.checks.filter((c) => c.result === "Not Assessed");
    expect(na.length).toBeGreaterThan(0);
    for (const c of na) { expect([c.awarded, c.max, c.marksText, c.completed]).toEqual([null, null, "Not assessed", false]); }
    expect(t.applicableMax).toBe(10 - na.length - t.counts.notApplicable);
    expect(t.formula).toMatch(/could not be assessed, left out of the maximum/);
  });
  it("all ten Not Assessed is no meaningful score, never 0%", () => {
    const m = withPolicy(null, { evidence: null, visual: null, scanPages: { ...model().scanPages!, scannedCount: 0, rows: [], failed: [] } });
    const t = buildTenChecks(m);
    expect(t.counts.notAssessed).toBe(10);
    expect([t.score, t.scoreText, t.totalText]).toEqual([null, "No meaningful score", "No applicable check"]);
    expect(t.summary).toMatch(/No meaningful score/);
  });
  it("a policy page that could not be read does not lower the score: the checks that read it are left out, and the summary says so", () => {
    const m = consent(withPolicy(NOT_READ(), { evidence: noPolicyEv, scanPages: { ...model().scanPages!, failed: [] } }), CLEAN);
    const t = buildTenChecks(m);
    expect(t.counts.notAssessed).toBeGreaterThanOrEqual(6);
    expect(t.summary).toMatch(/could not be assessed/);
    for (const c of t.checks) if (c.result === "Not Assessed") expect(c.awarded).toBeNull();
  });
  it("fewer than half the checks assessed makes the score provisional", () => {
    const t = buildTenChecks(consent(withPolicy(NOT_READ(), { evidence: noPolicyEv, scanPages: { ...model().scanPages!, failed: [] } }), CLEAN));
    expect(t.limitedBasis).toBe(t.applicableMax < 5);
    if (t.limitedBasis) expect(t.summary).toMatch(/provisional/);
  });
  it("scoreChecks counts Not Assessed apart and leaves it out of the maximum", () => {
    const rows = [...mix(6, 2, 1, 0), { result: "Not Assessed" as TenResult, awarded: null, max: null }];
    const s = scoreChecks(rows);
    expect([s.awarded, s.applicableMax, s.scoreText, s.counts.notAssessed]).toEqual([7, 9, "77.8/100", 1]);
  });
});

describe("check 10: traceability that is not circular", () => {
  it("counts only the findings of checks 1 to 9, says so, and names a source that was read", () => {
    const t = buildTenChecks(consent(withPolicy(analysis(), { evidence: QUIET_EV, scanPages: ALL_LOADED(model()) }), CLEAN));
    expect(t.checks[9].result).toBe("Passed");
    expect(t.checks[9].evidence).toMatch(/of \d+ findings \(checks 1 to 9; this check is not counted in its own evidence\)/);
  });
  it("a finding whose source page the scan never read is not traceable", () => {
    const a = analysis();
    a.requirements = a.requirements.map((r) => ({ ...r, source_url: "https://elsewhere.example/page" }));
    const t = buildTenChecks(consent(withPolicy(a), CLEAN));
    expect(t.checks[9].result).not.toBe("Passed");
  });
  it("a completed 'not found' result is not counted as evidence", () => {
    const t = buildTenChecks(consent(withPolicy(analysis({ retention: "not_found" })), CLEAN));
    expect(t.checks[9].explanation).toMatch(/records only what was searched for and not found/);
  });
});

describe("recommendations are tied to the checks that did not pass", () => {
  it("one per check that is Partially Passed or Failed, and none for a check that passed", () => {
    const t = buildTenChecks(consent(withPolicy(analysis({ retention: "not_found" })), CLEAN));
    const ids = t.recommendations.map((r) => r.id);
    expect(ids).toContain(5);
    for (const c of t.checks) if (c.result === "Passed") expect(ids).not.toContain(c.id);
    for (const r of t.recommendations) expect(r.text).not.toMatch(/violat|illegal|breach of the law/i);
  });
  it("a check left out for want of a readable page is answered by one recommendation to scan again", () => {
    const t = buildTenChecks(withPolicy(null));
    expect(t.recommendations.some((r) => r.id === 0 && /Run the scan again/.test(r.text))).toBe(true);
  });
});
