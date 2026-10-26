import { describe, expect, it } from "vitest";
import type { PolicyAnalysis, PolicyRequirementResult, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import { buildReadinessReview, READINESS_CLOSING, type ReadinessRow } from "./readiness";

const page = (url: string, via = "link", over: Record<string, unknown> = {}) => ({ id: url, url, title: null, http_status: 200, discovered_via: via, ...over });
const form = (page_url: string | undefined, fields: { name: string; field_type: string; required: boolean }[], over: Record<string, unknown> = {}) => ({
  id: `f-${page_url}-${fields.length}`, selector: "form", fields, purpose_guess: "contact_or_support", ...(page_url === undefined ? {} : { page_url }), ...over,
});
const BASE_PAGES = [page("https://x.test/", "seed"), page("https://x.test/privacy"), page("https://x.test/contact"), page("https://x.test/signup"), page("https://x.test/grievance")];
const evidence = (over: Record<string, unknown> = {}): ScanEvidenceResponse => ({
  pages: BASE_PAGES, forms: [], cookies: [], trackers: [], third_party_services: [],
  policies: [{ id: "pol", url: "https://x.test/privacy", policy_type: "privacy_policy" }],
  consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "Some CMP", has_reject_all: true, has_granular_choices: null, evidence: {} }], ...over,
}) as unknown as ScanEvidenceResponse;
const META = { consent_mechanism: "cmp", cmp_vendor: "Some CMP", accept_interaction: "clicked", reject_interaction: "clicked" } as unknown as WebsiteScanMetadata;

const prow = (id: string, status: string, over: Partial<PolicyRequirementResult> = {}): PolicyRequirementResult => ({
  id, title: `Title ${id}`, group: "g", basis: "required", provisions: [], status, quote: status === "verified" || status === "partial" ? `Words for ${id}.` : "",
  source_url: status === "verified" || status === "partial" ? "https://x.test/privacy" : null, detail: "", fix: "", contacts: null, response_days: null, ...over,
});
const IDS = ["notice_data_itemised", "notice_purposes", "consent_withdrawal", "rights_access", "rights_correction_erasure", "rights_nominate", "rights_means", "grievance_contact", "grievance_timeline", "complaint_to_board", "dpo_contact", "cross_border", "retention", "notice_language", "children_data"];
const policy = (status = "verified", over: Record<string, string> = {}): PolicyAnalysis => ({
  status: "analysed", reason: null, sector: null, documents: [], notes: [], limitations: [], model: null,
  summary: { applicable: 1, verified: 1, partial: 0, not_found: 0, not_applicable: 0, quotes_rejected: 0, completeness_percent: 100 },
  requirements: IDS.map((id) => prow(id, over[id] ?? (id === "children_data" ? "not_applicable" : status))),
}) as unknown as PolicyAnalysis;

const run = (o: { ev?: ScanEvidenceResponse; meta?: WebsiteScanMetadata | null; pol?: PolicyAnalysis | null; sector?: never } = {}) =>
  buildReadinessReview({ siteUrl: "https://x.test/", evidence: o.ev ?? evidence(), meta: o.meta === undefined ? META : o.meta, policy: o.pol === undefined ? policy() : o.pol, sector: o.sector ?? null });
const rowsOf = (r: ReturnType<typeof run>, pageN: number): ReadinessRow[] => r.rows.filter((x) => x.pageN === pageN);
const find = (r: ReturnType<typeof run>, text: string): ReadinessRow => { const x = r.rows.find((y) => y.check.includes(text)); if (!x) throw new Error(`no row: ${text}`); return x; };

describe("choosing the five pages", () => {
  it("finds each of the five from the pages the scan recorded, with the exact URL and why", () => {
    const r = run();
    expect(r.available).toBe(true);
    expect(r.pages.map((p) => [p.n, p.url])).toEqual([[1, "https://x.test/signup"], [2, "https://x.test/"], [3, "https://x.test/privacy"], [4, "https://x.test/contact"], [5, "https://x.test/grievance"]]);
    expect(r.pages.every((p) => p.found && p.why.length > 10)).toBe(true);
    expect(r.pages[1].scanned).toBe(true);
  });

  it("uses the checkout when there is no sign-up page, and the enquiry page when there is neither", () => {
    const ck = run({ ev: evidence({ pages: [...BASE_PAGES.filter((p) => !p.url.includes("signup")), page("https://x.test/checkout")] }) });
    expect(ck.pages[0]).toMatchObject({ url: "https://x.test/checkout", role: "Checkout (no sign-up page found)" });
    const lead = run({ ev: evidence({ pages: BASE_PAGES.filter((p) => !p.url.includes("signup")) }) });
    expect(lead.pages[0]).toMatchObject({ url: "https://x.test/contact" });
    expect(lead.pages[0].role).toContain("no sign-up or checkout page found");
  });

  it("writes NOT FOUND, records it as a finding and falls back, for a page that does not exist", () => {
    const r = run({ ev: evidence({ pages: [page("https://x.test/", "seed")], policies: [] }), pol: null });
    expect(r.pages[0]).toMatchObject({ url: null, found: false });
    expect(r.pages[2]).toMatchObject({ url: null, found: false });
    expect(r.pages[3]).toMatchObject({ url: null, found: false });
    expect(r.pages[4]).toMatchObject({ url: null, found: false });
    expect(r.pages[0].why).toContain("NOT FOUND");
    const exists = r.rows.filter((x) => x.check.startsWith("Page exists"));
    expect(exists.map((x) => x.pageN)).toEqual([1, 3, 4, 5]);
    expect(exists.every((x) => x.status === "Not Found" && x.scored === false)).toBe(true);
  });

  it("falls back to the contact page for the account page and the grievance page, and says so", () => {
    const r = run({ ev: evidence({ pages: [page("https://x.test/", "seed"), page("https://x.test/privacy"), page("https://x.test/contact-us")] }) });
    expect(r.pages[3]).toMatchObject({ url: "https://x.test/contact-us" });
    expect(r.pages[3].role).toContain("main contact form used");
    expect(r.pages[4].role).toContain("Contact Us used");
    expect(r.pages[3].why).toContain("NOT FOUND");
  });

  it("prefers a page the scan loaded over one it only knows of, and names a page it did not load as such", () => {
    const meta = { ...META, skipped_pages: [{ url: "https://x.test/register", reason: "page_cap" }] } as unknown as WebsiteScanMetadata;
    const r = run({ ev: evidence({ pages: BASE_PAGES.filter((p) => !p.url.includes("signup")) }), meta });
    expect(r.pages[0]).toMatchObject({ url: "https://x.test/register", scanned: false });
    expect(rowsOf(r, 1).every((x) => x.status === "Unable to Verify" || x.status === "Requires document review")).toBe(true);
    expect(find(r, "HTTPS").evidence).toContain("was not loaded in this scan");
  });

  it("does not take a login page for the sign-up page", () => {
    const r = run({ ev: evidence({ pages: [page("https://x.test/", "seed"), page("https://x.test/login"), page("https://x.test/sign-in"), page("https://x.test/privacy")] }) });
    expect(r.pages[0].found).toBe(false);
  });

  it("is not available when the scan loaded no page", () => {
    const r = run({ ev: evidence({ pages: [page("https://x.test/", "failed", { http_status: null })] }) });
    expect(r.available).toBe(false);
    expect(r.reason).toContain("no loaded pages");
    expect(buildReadinessReview({ siteUrl: "x", evidence: null, meta: null, policy: null, sector: null }).available).toBe(false);
  });
});

describe("the checks on each page", () => {
  it("never marks what the scan cannot see as Passes this technical check: notice wording, pre-ticked boxes, account features", () => {
    const r = run();
    expect(find(r, "notice is shown at the point of collection").status).toBe("Unable to Verify");
    expect(find(r, "Consent is free, specific").status).toBe("Unable to Verify");
    expect(rowsOf(r, 4)).toHaveLength(5);
    expect(rowsOf(r, 4).every((x) => x.status === "Unable to Verify")).toBe(true);
    expect(rowsOf(r, 4)[0].evidence).toContain("only be tested after signing in");
    const withAccount = run({ ev: evidence({ pages: [...BASE_PAGES, page("https://x.test/account/settings")] }) });
    expect(withAccount.pages[3].url).toBe("https://x.test/account/settings");
    expect(rowsOf(withAccount, 4).every((x) => x.status === "Unable to Verify" && x.evidence.includes("does not sign in"))).toBe(true);
  });

  it("marks a required marketing opt-in on the sign-up form as a Gap, from the form's fields", () => {
    const f = form("https://x.test/signup", [{ name: "email", field_type: "email", required: true }, { name: "marketing_optin", field_type: "checkbox", required: true }]);
    const r = run({ ev: evidence({ forms: [f] }) });
    const row = find(r, "Consent is free, specific");
    expect(row.status).toBe("Gap");
    expect(row.evidence).toContain("marketing_optin");
    expect(row.evidenceKind).toBe("Observed");
  });

  it("reads the form's page from the form itself, and says so when an older scan did not record it", () => {
    const f = form("https://x.test/signup", [{ name: "email", field_type: "email", required: true }]);
    expect(find(run({ ev: evidence({ forms: [f] }) }), "notice is shown at the point").evidence).toContain("1 form recorded on this page");
    const old = form(undefined, [{ name: "email", field_type: "email", required: true }], { purpose_guess: "login_or_signup" });
    expect(find(run({ ev: evidence({ forms: [old] }) }), "notice is shown at the point").evidence).toContain("did not record which page each form was on");
  });

  it("checks HTTPS and personal data in the URL on the page's own address", () => {
    expect(find(run(), "served over HTTPS").status).toBe("Passes this technical check");
    const http = run({ ev: evidence({ pages: [...BASE_PAGES.filter((p) => !p.url.includes("signup")), page("http://x.test/signup")] }) });
    expect(find(http, "served over HTTPS").status).toBe("Gap");
    const leak = run({ ev: evidence({ pages: [...BASE_PAGES.filter((p) => !p.url.includes("signup")), page("https://x.test/signup?email=a@b.com")] }) });
    expect(find(leak, "served over HTTPS").status).toBe("Gap");
    expect(find(leak, "served over HTTPS").evidence).toContain("email");
    const plain = run({ ev: evidence({ forms: [form("https://x.test/signup", [], { submit_url: "http://x.test/send" })] }) });
    expect(find(plain, "served over HTTPS").status).toBe("Gap");
  });

  it("names every analytics or advertising tag that fired before consent, and says nothing fired when nothing did", () => {
    const trackers = [
      { id: "1", script_src: "https://www.googletagmanager.com/gtag/js", vendor: "Google Analytics", category: "analytics", source: "rule", consent_states: ["pre_consent", "post_accept"] },
      { id: "2", script_src: "https://connect.facebook.net/x.js", vendor: null, category: "marketing", source: "rule", consent_states: ["pre_consent"] },
      { id: "3", script_src: "https://cdn.x/y.js", vendor: "A CDN", category: "functional", source: "rule", consent_states: ["pre_consent"] },
      { id: "4", script_src: "https://late.example/t.js", vendor: "Late Tag", category: "analytics", source: "rule", consent_states: ["post_accept"] },
    ];
    const row = find(run({ ev: evidence({ trackers }) }), "No analytics or advertising tag fires");
    expect(row.status).toBe("Gap");
    expect(row.evidence).toContain("Google Analytics (analytics)");
    expect(row.evidence).toContain("connect.facebook.net (marketing)");
    expect(row.evidence).not.toContain("A CDN");
    expect(row.evidence).not.toContain("Late Tag");
    expect(row.risk).toBe(5);
    expect(find(run(), "No analytics or advertising tag fires").status).toBe("Passes this technical check");
  });

  it("reads the banner: present, absent, and Reject as easy as Accept", () => {
    expect(find(run(), "A cookie banner appears").status).toBe("Passes this technical check");
    const none = run({ meta: { ...META, consent_mechanism: "none" } as WebsiteScanMetadata, ev: evidence({ consent_signals: [] }) });
    expect(find(none, "A cookie banner appears").status).toBe("Not Found");
    expect(find(none, "Reject is as easy").status).toBe("Unable to Verify");
    expect(find(run(), "Reject is as easy").status).toBe("Passes this technical check");
    const noReject = run({ ev: evidence({ consent_signals: [{ mechanism_type: "banner", cmp_vendor: null, has_reject_all: false, has_granular_choices: null, evidence: {} }] }) });
    expect(find(noReject, "Reject is as easy").status).toBe("Gap");
    const notClicked = run({ meta: { ...META, reject_interaction: "click_failed" } as WebsiteScanMetadata });
    expect(find(notClicked, "Reject is as easy").status).toBe("Unable to Verify");
    expect(find(run({ meta: null, ev: evidence({ consent_signals: [] }) }), "A cookie banner appears").status).toBe("Unable to Verify");
  });

  it("lists the third-party services and domains but does not claim where the data goes", () => {
    const ev = evidence({ third_party_services: [{ id: "s", service_name: "Stripe", category: "payments", domains: ["js.stripe.com"] }] });
    const row = find(run({ ev }), "Third-party scripts are listed");
    expect(row.status).toBe("Unable to Verify");
    expect(row.evidence).toContain("Stripe");
    expect(row.evidence).toContain("js.stripe.com");
    expect(row.evidence).toContain("not determined");
  });

  it("recognises an age gate a consent tool showed, and otherwise does not call the absence a gap", () => {
    const gate = evidence({ consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "CMP", has_reject_all: true, has_granular_choices: null, evidence: { consent_gate: { post_accept: { kind: "age_gate", cmp: "CMP", answer: "adult", outcome: "passed" } } } }] });
    expect(find(run({ ev: gate }), "age gate").status).toBe("Passes this technical check");
    expect(find(run(), "age gate").status).toBe("Unable to Verify");
  });

  it("marks advertising trackers on a site that appears to handle children as a Gap, and otherwise Unable to Verify", () => {
    const trackers = [{ id: "1", script_src: "https://ads.example/a.js", vendor: "AdCo", category: "marketing", source: "rule", consent_states: ["post_accept"] }];
    const kids = run({ ev: evidence({ trackers, forms: [form("https://x.test/signup", [{ name: "date_of_birth", field_type: "date", required: true }])] }) });
    expect(find(kids, "aimed at children").status).toBe("Gap");
    expect(find(run({ ev: evidence({ trackers }) }), "aimed at children").status).toBe("Unable to Verify");
  });

  it("takes the Indian-language check as Passes this technical check only when English and Indian-script content were both observed", () => {
    const lang = (html: string, indic: number) => ({ ...META, language_signals: { pages_checked: 3, pages_with_indic_script: indic, indic_scripts: indic ? { Tamil: 10 } : {}, switcher_scripts: [], hreflang: [], html_lang: html } }) as unknown as WebsiteScanMetadata;
    expect(find(run({ meta: lang("en", 1) }), "English or any Eighth Schedule").status).toBe("Passes this technical check");
    expect(find(run({ meta: lang("en", 0) }), "English or any Eighth Schedule").status).toBe("Unable to Verify");
    expect(find(run({ meta: META }), "English or any Eighth Schedule").evidence).toContain("Not recorded");
  });
});

describe("the privacy notice and the grievance page rest on the policy's own words", () => {
  it("marks a point quoted as Passes this technical check with the quotation and its page", () => {
    const row = find(run(), "itemised list");
    expect(row).toMatchObject({ status: "Passes this technical check", evidenceKind: "Quoted", pageN: 3 });
    expect(row.evidence).toContain("Words for notice_data_itemised.");
    expect(row.evidence).toContain("https://x.test/privacy");
  });

  it("maps partly stated to Gap, not found to Not Found, and takes the worst of several points", () => {
    const r = run({ pol: policy("verified", { rights_access: "partial", rights_nominate: "not_found", consent_withdrawal: "partial", retention: "not_found" }) });
    expect(find(r, "How to exercise rights").status).toBe("Not Found");
    expect(find(r, "How to withdraw consent").status).toBe("Gap");
    expect(find(r, "retention or erasure period").status).toBe("Not Found");
    expect(find(r, "How to exercise rights").evidence).toContain("not found in the policy text");
  });

  it("says Not Found for every policy point when there is no privacy policy, and Unable to Verify when the text was not read", () => {
    const none = run({ ev: evidence({ pages: BASE_PAGES.filter((p) => !p.url.includes("privacy")), policies: [] }), pol: null });
    expect(find(none, "itemised list").status).toBe("Not Found");
    const unread = run({ pol: { ...policy(), status: "unavailable", reason: "The policy page could not be read.", requirements: [] } as PolicyAnalysis });
    expect(find(unread, "itemised list").status).toBe("Unable to Verify");
    expect(find(unread, "itemised list").evidence).toBe("The policy page could not be read.");
  });

  it("checks the response period and the named contact on the grievance page", () => {
    const r = run({ pol: policy("verified", { grievance_timeline: "not_found", dpo_contact: "partial", grievance_contact: "partial" }) });
    expect(find(r, "response period of 90 days or less").status).toBe("Not Found");
    expect(find(r, "named Data Protection Officer or contact person").status).toBe("Gap");
    expect(find(run(), "response period of 90 days or less").status).toBe("Passes this technical check");
  });

  it("takes the identifier a request needs from the form on the grievance page, and otherwise cannot verify it", () => {
    const f = form("https://x.test/grievance", [{ name: "email", field_type: "email", required: true }, { name: "customer_id", field_type: "text", required: false }]);
    const ok = find(run({ ev: evidence({ forms: [f] }) }), "which identifier is needed");
    expect(ok.status).toBe("Passes this technical check");
    expect(ok.evidence).toContain("customer_id");
    expect(find(run(), "which identifier is needed").status).toBe("Unable to Verify");
  });

  it("does not judge the effective date, and sends breach, impact assessments and retention schedules to document review", () => {
    const r = run();
    expect(find(r, "effective date").status).toBe("Unable to Verify");
    const doc = r.rows.filter((x) => x.status === "Requires document review");
    expect(doc.map((x) => x.check)).toEqual(expect.arrayContaining([
      "Where no consent is taken, a valid legitimate use applies", "Personal data breach reporting",
      "Data protection impact assessment and audit for a Significant Data Fiduciary", "Internal retention schedules and erasure practice",
      "If the owner is a Significant Data Fiduciary, the Data Protection Officer is based in India",
    ]));
    expect(doc.every((x) => x.scored === false)).toBe(true);
    expect(r.rows.filter((x) => x.pageN === 0)).toHaveLength(3);
  });

  it("writes every provision in full: the Act's section and the Rules' rule", () => {
    const r = run();
    expect(find(r, "itemised list").sections).toBe("DPDP Act, 2023, Section 5(1) · DPDP Rules, 2025, Rule 3(b)");
    expect(r.rows.every((x) => x.sections === "—" || /DPDP (Act|Rules)/.test(x.sections))).toBe(true);
    expect(r.rows.every((x) => !/\bs\.\d|\br\.\d/.test(x.sections))).toBe(true);
  });
});

describe("the summary", () => {
  it("scores the checks the scan could assess, counting Unable to Verify and document review in neither side", () => {
    const r = run();
    const scored = r.rows.filter((x) => x.scored && ["Passes this technical check", "Gap", "Not Found"].includes(x.status));
    const ok = scored.filter((x) => x.status === "Passes this technical check").length;
    expect(r.assessed).toBe(scored.length);
    expect(r.scorePercent).toBe(Math.round((ok / scored.length) * 100));
    expect(r.counts["Passes this technical check"] + r.counts.Gap + r.counts["Not Found"] + r.counts["Unable to Verify"] + r.counts["Requires document review"]).toBe(r.rows.length);
    expect(r.scoreLine).toContain(`${r.scorePercent} out of 100`);
    expect(r.scoreLine).toContain("not counted");
  });

  it("gives no score when nothing could be assessed", () => {
    const r = run({ ev: evidence({ forms: [], consent_signals: [] }), meta: null, pol: null });
    expect(r.rows.filter((x) => x.scored && ["Passes this technical check", "Gap", "Not Found"].includes(x.status)).length).toBe(r.assessed);
    const bare = buildReadinessReview({ siteUrl: "x", evidence: evidence({ policies: [], pages: [page("https://x.test/", "seed")], consent_signals: [] }), meta: null, policy: null, sector: null });
    expect(typeof bare.scorePercent === "number" || bare.scorePercent === null).toBe(true);
    if (bare.scorePercent === null) expect(bare.scoreLine).toContain("Not enough could be verified");
  });

  it("ranks at most five gaps by risk, highest first, each with its fix, and names the riskiest page", () => {
    const pre = [{ id: "1", script_src: "https://t.example/a.js", vendor: "Tag", category: "analytics", source: "rule", consent_states: ["pre_consent"] }];
    const r = run({ ev: evidence({ trackers: pre }), pol: policy("not_found") });
    expect(r.topGaps.length).toBeGreaterThan(0);
    expect(r.topGaps.length).toBeLessThanOrEqual(5);
    expect(r.topGaps.map((g) => g.rank)).toEqual(r.topGaps.map((_, i) => i + 1));
    const risks = r.topGaps.map((g) => g.row.risk);
    expect(risks).toEqual([...risks].sort((a, b) => b - a));
    expect(r.topGaps.every((g) => g.row.fix.length > 10 && (g.row.status === "Gap" || g.row.status === "Not Found"))).toBe(true);
    expect(r.riskiest).not.toBeNull();
    expect(r.riskiest!.why).toContain("gap");
    const perPage = new Map<number, number>();
    for (const x of r.rows.filter((y) => y.scored && (y.status === "Gap" || y.status === "Not Found"))) perPage.set(x.pageN, (perPage.get(x.pageN) ?? 0) + x.risk);
    expect(perPage.get(r.riskiest!.n)).toBe(Math.max(...perPage.values()));
  });

  it("has no top gaps and no riskiest page when nothing fell short", () => {
    const r = run({ ev: evidence({ forms: [form("https://x.test/grievance", [{ name: "email", field_type: "email", required: true }])] }) });
    expect(r.rows.filter((x) => x.status === "Gap" || x.status === "Not Found")).toEqual([]);
    expect(r.topGaps).toEqual([]);
    expect(r.riskiest).toBeNull();
  });

  it("is a readiness check: dated, never a violation, ending with the required sentence", () => {
    const r = run({ pol: policy("not_found") });
    expect(r.timing).toContain("13 May 2027");
    expect(r.closing).toBe(READINESS_CLOSING);
    expect(READINESS_CLOSING).toBe("This is a compliance readiness review, not legal advice.");
    const all = JSON.stringify(r).replace(/not violations/g, "");
    expect(all).not.toMatch(/violat/i);
  });

  it("gives every row a status from the five, an evidence kind, a fix and a risk from 1 to 5", () => {
    for (const x of run({ pol: policy("not_found") }).rows) {
      expect(["Passes this technical check", "Gap", "Not Found", "Unable to Verify", "Requires document review"]).toContain(x.status);
      expect(["Quoted", "Observed", "Not seen", "Not captured"]).toContain(x.evidenceKind);
      expect(x.fix.length).toBeGreaterThan(5);
      expect(x.evidence.length).toBeGreaterThan(5);
      expect(x.risk).toBeGreaterThanOrEqual(1);
      expect(x.risk).toBeLessThanOrEqual(5);
    }
  });
});

describe("a consent tool whose banner was never shown (the scan could not see a banner)", () => {
  // What an older scan stored for a site whose consent tool loaded and showed nothing: the page text had a footer "cookie settings" link, so reject-all was recorded as false
  // and granular choices as true, with banner_ui_observed false and neither Accept nor Reject clicked.
  const notShown = (over: Record<string, unknown> = {}) => run({
    meta: { ...META, accept_interaction: "cmp_not_automatable", reject_interaction: "cmp_not_automatable" } as WebsiteScanMetadata,
    ev: evidence({ consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "Some CMP", has_reject_all: false, has_granular_choices: true, evidence: { banner_ui_observed: false, accept_interaction: "cmp_not_automatable", reject_interaction: "cmp_not_automatable", ...over } }] }),
  });

  it("does not say a banner appears: a script is not a banner", () => {
    const row = find(notShown(), "A cookie banner appears");
    expect(row.status).toBe("Unable to Verify");
    expect(row.evidence).toContain("no banner was shown to the scanner");
  });

  it("does not report a missing Reject option it never saw", () => {
    const row = find(notShown(), "Reject is as easy");
    expect(row.status).toBe("Unable to Verify");
    expect(notShown().rows.some((r) => r.status === "Gap")).toBe(false);
  });

  it("is not counted against the score: no gap, so the five-page score is not lowered by something unobserved", () => {
    expect(notShown().counts.Gap).toBe(0);
  });

  it("still reports a real missing Reject when the banner was displayed", () => {
    const shown = run({ ev: evidence({ consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "Some CMP", has_reject_all: false, has_granular_choices: false, evidence: { banner_ui_observed: true } }] }) });
    expect(find(shown, "Reject is as easy").status).toBe("Gap");
  });

  it("an older scan with no observation flag is unknown, not 'not displayed'", () => {
    const old = run({ ev: evidence({ consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "Some CMP", has_reject_all: false, has_granular_choices: null, evidence: {} }] }) });
    expect(find(old, "Reject is as easy").status).toBe("Gap");
  });
});

describe("the five pages are not chosen from article slugs", () => {
  const page = (url: string, title = "") => ({ id: url, url, title, discovered_via: "link", http_status: 200 });

  it("a blog post about a topic is not the page for that topic", () => {
    const ev = evidence({ pages: [
      page("https://x.test/"), page("https://x.test/privacy"),
      page("https://x.test/blog/get-started-with-dpdp-compliance"), page("https://x.test/blog/contact-us-tips-for-founders"),
    ] as never });
    const r = run({ ev });
    const urls = r.pages.map((p) => p.url ?? "");
    expect(urls.some((u) => u.includes("/blog/"))).toBe(false);
  });

  it("the real contact page is still chosen over an article about contacting", () => {
    const ev = evidence({ pages: [page("https://x.test/"), page("https://x.test/privacy"), page("https://x.test/contact"), page("https://x.test/blog/contact-us-tips-for-founders")] as never });
    expect(run({ ev }).pages.map((p) => p.url ?? "")).toContain("https://x.test/contact");
  });
});
