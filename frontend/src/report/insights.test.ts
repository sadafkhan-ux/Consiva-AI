import { describe, expect, it } from "vitest";
import type { PolicyAnalysis, PolicyRequirementResult, ScanEvidenceResponse, SectorAssessment, WebsiteScanMetadata } from "../api/types";
import { buildChildrenSection, buildDpoSection, buildGrievanceSection, buildInsightSections, buildLanguageSection, buildSecuritySection } from "./insights";

const req = (id: string, status: string, over: Partial<PolicyRequirementResult> = {}): PolicyRequirementResult => ({
  id, title: `Title ${id}`, group: "Grievance", basis: "required", provisions: ["DPDP Act s.13(1)"], status, quote: status === "verified" || status === "partial" ? `A quoted passage for ${id} that is long enough.` : "",
  source_url: status === "verified" || status === "partial" ? "https://x.test/privacy" : null, detail: "d", fix: `Fix ${id}`, contacts: null, response_days: null, ...over,
});
const policy = (rows: PolicyRequirementResult[], status = "analysed"): PolicyAnalysis => ({
  status, reason: status === "analysed" ? null : "No privacy or cookie policy page was found on the scanned pages, so there was nothing to read.", sector: null, documents: [], notes: [],
  summary: status === "analysed" ? { applicable: rows.length, verified: 0, partial: 0, not_found: 0, not_applicable: 0, quotes_rejected: 0, completeness_percent: 50 } : null,
  requirements: status === "analysed" ? rows : [], limitations: [], model: null,
});
const CONTACTS = { emails: ["saurabhk@swaransoft.com"], phones: ["+91 9818 600 650"], urls: [] };

describe("grievance officer", () => {
  it("is confirmed, with the contact details, the response period and the Board escalation, when the policy says all three", () => {
    const s = buildGrievanceSection(policy([
      req("grievance_contact", "verified", { contacts: CONTACTS }), req("grievance_timeline", "verified", { response_days: 30, provisions: ["DPDP Rules r.14(3)"] }),
      req("complaint_to_board", "verified", { provisions: ["DPDP Act s.13(3)"] }),
    ]));
    expect(s.key).toBe("grievance");
    expect(s.status).toBe("Grievance contact found");
    expect(s.tone).toBe("good");
    expect(s.knowledge).toBe("Confirmed");
    const f = Object.fromEntries(s.facts.map((x) => [x.label, x.value]));
    expect(f["Grievance contact"]).toBe("saurabhk@swaransoft.com · +91 9818 600 650");
    expect(f["Response period"]).toBe("Within about 30 days; the Rules allow up to ninety");
    expect(f["Complaint to the Data Protection Board"]).toBe("Explained in the policy");
    expect(s.quote).toEqual({ text: "A quoted passage for grievance_contact that is long enough.", source: "https://x.test/privacy" });
    expect(s.legal).toBe("DPDP Act, 2023, Section 13(1) · DPDP Rules, 2025, Rule 14(3) · DPDP Act, 2023, Section 13(3)");
  });

  it("says a named officer with nothing to contact them by is only partly stated", () => {
    const s = buildGrievanceSection(policy([req("grievance_contact", "partial"), req("grievance_timeline", "partial", { detail: "A period is mentioned but no response period is stated in the passage." }), req("complaint_to_board", "not_found")]));
    expect(s.status).toBe("Grievance officer named, no contact beside the name");
    expect(s.tone).toBe("warn");
    expect(s.knowledge).toBe("Confirmed");                       // confirmed as far as the words go
    expect(s.facts[0].value).toContain("no email, phone number or form");
    expect(s.facts[1].value).toContain("no response period");
    expect(s.facts[2]).toMatchObject({ value: "Not found in the policy text", tone: "warn" });
  });

  it("calls a point that is not in the policy not found, not a violation, and not confirmed", () => {
    const s = buildGrievanceSection(policy([req("grievance_contact", "not_found"), req("grievance_timeline", "not_found"), req("complaint_to_board", "not_found")]));
    expect(s.status).toBe("No grievance contact found in the policy text");
    expect(s.knowledge).toBe("Not verified");
    expect(s.quote).toBeNull();
    expect(JSON.stringify(s)).not.toMatch(/violat/i);
  });

  it("is not assessed, with the reason, when there is no policy to read", () => {
    const s = buildGrievanceSection(policy([], "unavailable"));
    expect(s).toMatchObject({ status: "Not assessed", tone: "mute", knowledge: "Not verified", facts: [] });
    expect(s.summary).toContain("nothing to read");
    expect(buildGrievanceSection(null).summary).toBe("The privacy policy analysis was not run for this scan.");
  });

  it("rejects a response period longer than the Rules allow", () => {
    const s = buildGrievanceSection(policy([req("grievance_contact", "verified", { contacts: CONTACTS }), req("grievance_timeline", "partial", { response_days: 120, detail: "The stated period is about 120 days; the Rules allow a reasonable period not exceeding ninety days." })]));
    expect(s.facts[1]).toMatchObject({ label: "Response period", tone: "warn" });
    expect(s.facts[1].value).toContain("ninety days");
  });
});

describe("Data Protection Officer", () => {
  it("shows the business contact found, and says when a DPO is actually required", () => {
    const s = buildDpoSection(policy([req("dpo_contact", "verified", { contacts: { emails: ["dpo@x.test"], phones: [], urls: [] }, provisions: ["DPDP Act s.8(9)"] })]));
    expect(s.status).toBe("Contact for a Data Protection Officer or answering person found");
    expect(s.facts[0].value).toBe("dpo@x.test");
    expect(s.legal).toBe("DPDP Act, 2023, Section 8(9) · DPDP Act, 2023, Section 10(2)(a)");
    expect(s.notes[0]).toContain("only of a Significant Data Fiduciary");
    expect(s.notes[0]).toContain("Section 8(9)");
    expect(s.notes[1]).toContain("cannot be seen from a website");
  });

  it("reports no DPO found as not found in the text, never as a missing duty", () => {
    const s = buildDpoSection(policy([req("dpo_contact", "not_found")]));
    expect(s.status).toBe("No Data Protection Officer or answering person found in the policy text");
    expect(s.knowledge).toBe("Not verified");
    expect(JSON.stringify(s)).not.toMatch(/violat/i);
  });

  it("is not assessed without a policy", () => {
    expect(buildDpoSection(policy([], "unavailable"))).toMatchObject({ status: "Not assessed", knowledge: "Not verified" });
  });
});

describe("Indian-language support", () => {
  const lang = (over = {}): WebsiteScanMetadata => ({ language_signals: { pages_checked: 5, pages_with_indic_script: 0, indic_scripts: {}, switcher_scripts: [], hreflang: [], html_lang: "en", ...over } });

  it("is observed when pages carry Indian-script text, and says seeing it is not proof the notice is offered", () => {
    const s = buildLanguageSection(lang({ pages_with_indic_script: 2, indic_scripts: { Tamil: 120, "Devanagari (Hindi)": 400 } }), null);
    expect(s.status).toBe("Indian-language content observed");
    expect(s.knowledge).toBe("Observed");
    expect(s.tone).toBe("good");
    const f = Object.fromEntries(s.facts.map((x) => [x.label, x.value]));
    expect(f["Pages with text in an Indian script"]).toBe("2 of 5");
    expect(f["Scripts seen"]).toBe("Devanagari (Hindi) (400 characters); Tamil (120 characters)");   // the more used first
    expect(s.summary).toContain("does not show that the privacy notice itself is offered");
    expect(s.legal).toBe("DPDP Act, 2023, Section 5(3)");
  });

  it("counts a language chooser written in an Indian script, and declared Eighth Schedule alternates, but not English or x-default", () => {
    expect(buildLanguageSection(lang({ switcher_scripts: ["Tamil"] }), null).status).toBe("Indian-language content observed");
    expect(buildLanguageSection(lang({ hreflang: ["hi-IN", "en", "x-default"] }), null).status).toBe("Indian-language content observed");
    expect(buildLanguageSection(lang({ hreflang: ["en", "x-default", "fr-FR"] }), null).status).toBe("No Indian-language content observed");
  });

  it("calls an absence not observed and not verified, and says it is not a finding", () => {
    const s = buildLanguageSection(lang(), null);
    expect(s).toMatchObject({ status: "No Indian-language content observed", tone: "mute", knowledge: "Not verified" });
    expect(s.notes[0]).toContain("is not evidence that the option is missing");
    expect(s.notes[0]).toContain("does not change the score");
    expect(JSON.stringify(s)).not.toMatch(/violat|non-complian/i);
  });

  it("adds what the policy itself says about languages", () => {
    const s = buildLanguageSection(lang(), policy([req("notice_language", "verified", { group: "Notice" })]));
    expect(s.facts.at(-1)).toMatchObject({ label: "The policy says the notice is available in other languages", value: "Yes (see the quotation)", tone: "good" });
    expect(s.quote?.text).toContain("notice_language");
  });

  it("says nothing can be said for a scan made before languages were recorded", () => {
    const s = buildLanguageSection({}, null);
    expect(s).toMatchObject({ status: "Not recorded for this scan", knowledge: "Not verified" });
    expect(buildLanguageSection(null, null).status).toBe("Not recorded for this scan");
  });
});

const sector = (over: Partial<SectorAssessment> = {}): SectorAssessment => ({
  sector: "education", label: "Education", confidence: "medium", summary: "s", signals: [], also_possible: [], notes: [], limitations: [], declarations: [{ flag: "children", requirement_id: "DPDP-X10", label: "l", why: "w", declared: null }], ...over,
});
const forms = (...names: string[]) => ({ pages: [], forms: [{ id: "f", selector: "form", purpose_guess: null, fields: names.map((n) => ({ name: n, field_type: "text", required: false })) }], cookies: [], trackers: [], third_party_services: [], policies: [], consent_signals: [] }) as unknown as ScanEvidenceResponse;

describe("children's data and the age gate", () => {
  it("applies where the sector suggests children, and lists the form fields that point to them", () => {
    const s = buildChildrenSection(sector(), forms("student name", "parent email", "email"), null);
    expect(s.status).toBe("Children's data may be involved");
    expect(s.tone).toBe("warn");
    expect(s.facts.find((f) => f.label === "Form fields that suggest children's data")?.value).toBe("student name, parent email");
    expect(s.legal).toContain("DPDP Act, 2023, Section 9(1)");
    expect(s.legal).toContain("DPDP Rules, 2025, Rule 10");
    expect(s.notes[0]).toContain("Fourth Schedule");
  });

  it("applies on the form fields alone, even in a sector that does not suggest children", () => {
    const s = buildChildrenSection(sector({ sector: "it", label: "IT services", declarations: [] }), forms("date of birth"), null);
    expect(s.status).toBe("Children's data may be involved");
  });

  it("is not applicable when nothing points to children, and says what that does and does not mean", () => {
    const s = buildChildrenSection(sector({ sector: "it", label: "IT services", declarations: [] }), forms("email", "company"), null);
    expect(s).toMatchObject({ status: "Not applicable on the evidence", tone: "mute", knowledge: "Not verified" });
    expect(s.summary).toContain("cannot see who actually uses a service");
  });

  it("reports an age gate the scan recognised, and says plainly when it recognised none", () => {
    const withGate = { ...forms("email"), consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "Consiva Consent SDK", has_reject_all: true, has_granular_choices: true,
      evidence: { consent_gate: { post_accept: { kind: "age_gate", cmp: "Consiva Consent SDK", answer: "adult", outcome: "passed" }, post_reject: null } } }] } as unknown as ScanEvidenceResponse;
    const a = buildChildrenSection(sector(), withGate, null).facts.find((f) => f.label === "Age gate before the consent banner")!;
    expect(a.value).toBe("age gate from Consiva Consent SDK; the scanner answered as an adult visitor (outcome: passed)");
    expect(a.tone).toBe("good");
    const none = buildChildrenSection(sector(), forms("email"), null).facts.find((f) => f.label === "Age gate before the consent banner")!;
    expect(none.value).toContain("None recognised");
    expect(none.value).toContain("not proof there is none");
  });

  it("brings in what the policy says about children when the point applies", () => {
    const s = buildChildrenSection(sector(), forms("student name"), policy([req("children_data", "verified", { group: "Children" })]));
    expect(s.facts.at(-1)).toMatchObject({ label: "The policy's statement on children's data", value: "Stated (see the quotation)", tone: "good" });
    expect(s.quote?.text).toContain("children_data");
    const na = buildChildrenSection(sector({ declarations: [] }), forms("email"), policy([req("children_data", "not_applicable", { group: "Children" })]));
    expect(na.facts.at(-1)?.value).toContain("no sign the site is aimed at children");
  });
});

describe("observable security signals", () => {
  const headers = { hsts: "max-age=31536000", csp: "default-src 'self'", x_frame_options: null, x_content_type_options: "nosniff", referrer_policy: null, permissions_policy: null };
  const meta = (over = {}): WebsiteScanMetadata => ({ security_signals: { pages_total: 5, pages_https: 5, homepage_https: true, homepage_headers: headers, ...over } });
  const fact = (s: ReturnType<typeof buildSecuritySection>, label: string) => s.facts.find((f) => f.label.startsWith(label))!;

  it("reports each signal as observed or not observed, and counts them", () => {
    const s = buildSecuritySection(meta(), null);
    expect(fact(s, "Scanned pages served over HTTPS")).toMatchObject({ value: "5 of 5", tone: "good" });
    expect(fact(s, "Homepage served over HTTPS").value).toBe("Yes");
    expect(fact(s, "Strict-Transport-Security")).toMatchObject({ value: "Observed on the homepage", tone: "good" });
    expect(fact(s, "Content-Security-Policy").tone).toBe("good");
    expect(fact(s, "Referrer-Policy")).toMatchObject({ value: "Not observed on the homepage", tone: "warn" });
    expect(s.status).toBe("5 of 8 signals observed");                   // https twice, HSTS, CSP and nosniff; no clickjacking header, referrer or permissions policy
  });

  it("counts clickjacking protection from X-Frame-Options or a CSP frame-ancestors directive", () => {
    expect(fact(buildSecuritySection(meta({ homepage_headers: { ...headers, x_frame_options: "DENY" } }), null), "Clickjacking").tone).toBe("good");
    expect(fact(buildSecuritySection(meta({ homepage_headers: { ...headers, csp: "frame-ancestors 'none'" } }), null), "Clickjacking").tone).toBe("good");
    expect(fact(buildSecuritySection(meta(), null), "Clickjacking").tone).toBe("warn");
  });

  it("is all observed only when every signal is", () => {
    const all = { hsts: "x", csp: "y", x_frame_options: "DENY", x_content_type_options: "nosniff", referrer_policy: "same-origin", permissions_policy: "camera=()" };
    const s = buildSecuritySection(meta({ homepage_headers: all }), null);
    expect(s).toMatchObject({ status: "8 of 8 signals observed", tone: "good", knowledge: "Observed" });
  });

  it("flags pages that were not served over HTTPS", () => {
    const s = buildSecuritySection(meta({ pages_https: 3 }), null);
    expect(fact(s, "Scanned pages served over HTTPS")).toMatchObject({ value: "3 of 5: not all", tone: "warn" });
    expect(buildSecuritySection(meta({ homepage_https: false }), null).facts.find((f) => f.label === "Homepage served over HTTPS")?.value).toBe("No");
  });

  it("summarises the cookie attributes, and says why a lower HttpOnly count is expected", () => {
    const ev = { pages: [], forms: [], trackers: [], third_party_services: [], policies: [], consent_signals: [],
      cookies: [{ id: "1", name: "a", secure: true, http_only: false, same_site: "Lax" }, { id: "2", name: "b", secure: false, http_only: true, same_site: null }, { id: "3", name: "c", secure: null, http_only: null, same_site: null }] } as unknown as ScanEvidenceResponse;
    const v = fact(buildSecuritySection(meta(), ev), "Cookie attributes").value;
    expect(v).toContain("Secure: 1 of 2");
    expect(v).toContain("HttpOnly: 1 of 2");
    expect(v).toContain("SameSite set: 1 of 1");              // only one cookie had a SameSite value recorded
    expect(v).toContain("cannot be HttpOnly");
  });

  it("flags a form that posts to an unencrypted address, and says when where forms post to was not recorded", () => {
    const mk = (urls: (string | null | undefined)[]) => ({ pages: [], cookies: [], trackers: [], third_party_services: [], policies: [], consent_signals: [],
      forms: urls.map((u, i) => ({ id: String(i), selector: null, fields: [], purpose_guess: null, ...(u === undefined ? {} : { submit_url: u }) })) }) as unknown as ScanEvidenceResponse;
    expect(fact(buildSecuritySection(meta(), mk(["https://x.test/send", null])), "Forms post over HTTPS")).toMatchObject({ value: "All 2 post over HTTPS or to their own page", tone: "good" });
    expect(fact(buildSecuritySection(meta(), mk(["http://x.test/send", "https://x.test/ok"])), "Forms post over HTTPS")).toMatchObject({ value: "1 of 2 post to an unencrypted address", tone: "warn" });
    expect(fact(buildSecuritySection(meta(), mk([undefined])), "Forms post over HTTPS").value).toBe("Not recorded for this scan");
  });

  it("says nothing can be said for a scan made before the signals were recorded", () => {
    const s = buildSecuritySection({}, null);
    expect(s).toMatchObject({ status: "Not recorded for this scan", tone: "mute", knowledge: "Not verified" });
    expect(s.facts.every((f) => f.value === "Not recorded for this scan")).toBe(true);
  });

  it("never claims the safeguards are adequate or missing, and never touches the score", () => {
    const s = buildSecuritySection(meta(), null);
    expect(s.notes.join(" ")).toContain("cannot be seen from outside");
    expect(s.notes.join(" ")).toContain("not a finding that the safeguard is missing");
    expect(s.notes.join(" ")).toContain("does not change the score");
    expect(s.legal).toBe("DPDP Act, 2023, Section 8(5) · DPDP Rules, 2025, Rule 6");
  });
});

describe("all five together", () => {
  it("come in a fixed order, each with its own key, and always exist even when there is nothing to read", () => {
    const sections = buildInsightSections({ policy: null, meta: null, evidence: null, sector: null });
    expect(sections.map((s) => s.key)).toEqual(["grievance", "dpo", "language", "children", "security"]);
    expect(sections.every((s) => s.title && s.status && s.legal && ["Confirmed", "Observed", "Not verified"].includes(s.knowledge))).toBe(true);
  });
});
