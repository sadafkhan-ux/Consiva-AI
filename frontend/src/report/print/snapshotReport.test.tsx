import { ringNumber } from "../scoreFormat";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpRequirementsResponse, EvidenceCookieItem, ScanEvidenceResponse, ScanScreenshot, WebsiteScanMetadata } from "../../api/types";
import fixtures from "../../test/requirementsFixtures.json";
import { buildReportModel } from "../buildReport";
import { buildRequirementsView } from "../requirements";
import { A } from "../../test/assessments";
import type { TenAssessment } from "../tenChecks";
import { remediationCta, SNAPSHOT_DISCLAIMER, SnapshotReport } from "./SnapshotReport";
import { SIGNUP_URL } from "../summaryReport";

const F = fixtures as unknown as Record<string, DpdpRequirementsResponse>;
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
const model = (o: { score?: keyof typeof F; shot?: ScanScreenshot | null; meta?: WebsiteScanMetadata; assessment?: TenAssessment | null } = {}) =>
  buildReportModel(SCAN, [], o.meta ?? META, COOKIES, EVIDENCE.trackers, buildRequirementsView(F[o.score ?? "mixed"]), EVIDENCE, null, null, o.shot === undefined ? SHOT : o.shot, undefined, o.assessment === undefined ? A.mixed : o.assessment);
afterEach(cleanup);

describe("the two-page report (variant summary)", () => {
  it("has exactly two sheets; the header and the blue frame are on page 1 only", () => {
    render(<SnapshotReport model={model()} />);
    expect(document.querySelectorAll("section.pg")).toHaveLength(2);
    expect(within(screen.getByTestId("sn-page-1")).getByTestId("sn-logo")).toHaveAttribute("alt", "Consiva.ai");
    expect(screen.getByTestId("sn-page-2").querySelector("header, img")).toBeNull();
  });

  it("page 1: the score, the screenshot with the address, the consent banner, the privacy elements, the framework counts and the key metrics", () => {
    render(<SnapshotReport model={model()} />);
    const p1 = within(screen.getByTestId("sn-page-1"));
    expect(p1.getByTestId("sn-score")).toHaveTextContent("Privacy Readiness Score");
    expect(p1.getByTestId("pg-hero-preview")).toHaveTextContent("https://x.test");
    expect(p1.getByTestId("sn-banner")).toHaveTextContent("Consent Banner");
    expect(p1.getByTestId("sn-elements")).toHaveTextContent(/of 6 found/);
    expect(p1.getByTestId("sn-framework")).toHaveTextContent("India DPDP Act, 2023 and Rules, 2025");
    expect(p1.getByTestId("sn-metrics")).toHaveTextContent("Cookies set");
    expect(p1.getByTestId("sn-meta")).toHaveTextContent("Report ID");
    expect(p1.getAllByTestId("sn-contact")).toHaveLength(2);                                  // the DPO and the Grievance Officer are always shown
    expect(p1.getByTestId("sn-contacts")).toHaveTextContent("Data Protection Officer");
    expect(p1.getByTestId("sn-contacts")).toHaveTextContent("Grievance Officer");
    expect(p1.getByTestId("sn-banner-facts")).toHaveTextContent("Consent mechanism");          // what the scanner recorded about the banner
  });

  it("page 2: cookies (at most five), Forms & Consent with a status each, consent states, checkpoints, scanned URLs, page summary, recommendations and the disclaimer", () => {
    render(<SnapshotReport model={model()} />);
    const p2 = within(screen.getByTestId("sn-page-2"));
    expect(p2.getByTestId("sn-cookies").querySelectorAll("tbody tr").length).toBeLessThanOrEqual(5);
    expect(p2.getAllByTestId("sn-form-row")).toHaveLength(3);
    expect(within(screen.getByTestId("sn-page-1")).getByTestId("sn-consent")).toHaveTextContent("After Reject");     // the consent states are compared on page 1
    expect(p2.getAllByTestId("sn-checkpoint").length).toBeGreaterThan(0);
    const p1 = within(screen.getByTestId("sn-page-1"));                                                              // the scanned pages sit on page 1, giving page 2 room for the checks
    expect(p1.getByTestId("sn-pages")).toHaveTextContent("x.test/privacy");
    expect(p1.getByTestId("sn-pages")).not.toHaveTextContent("x.test/old");           // a page that failed to load is not listed
    expect(p1.getByTestId("sn-pages")).not.toHaveTextContent("Not scanned");
    expect(p2.queryByTestId("sn-page-summary")).toBeNull();                                                          // Page Scan Summary and Recommendations are not on page 2
    expect(p2.getByTestId("sn-cta")).toHaveTextContent("Unlock Your Complete Privacy Assessment");
    expect(p2.getByTestId("sn-cta")).toHaveTextContent("Go Beyond the Quick Scan");
    expect(p2.queryByTestId("sn-limits")).toBeNull();
    expect(p2.queryByTestId("sn-reco")).toBeNull();
    expect(p2.queryByText("Recommendations")).toBeNull();
    expect(p2.getByTestId("sn-disclaimer")).toHaveTextContent(SNAPSHOT_DISCLAIMER);
  });

  it("lists all ten checks with a result and marks, and never says Unable to Verify", () => {
    render(<SnapshotReport model={model()} />);
    const rows = screen.getAllByTestId("sn-checkpoint");
    expect(rows).toHaveLength(10);
    for (const r of rows) expect(["Passed", "Partially Passed", "Failed", "Not Assessed", "Not Applicable"]).toContain(r.getAttribute("data-status"));
    expect(screen.queryByTestId("sn-formula")).toBeNull();   // the formula line is not printed in the report
    expect(document.body.textContent).not.toMatch(/unable to verify/i);
    expect(screen.getAllByTestId("sn-why").length).toBeGreaterThan(0);   // every Failed or Partially Passed check says why
  });

  it("every checkpoint is listed once, with its score", () => {
    render(<SnapshotReport model={model()} />);
    const rows = screen.getAllByTestId("sn-checkpoint");
    expect(new Set(rows.map((r) => r.textContent)).size).toBe(rows.length);
    for (const r of rows) expect(r.textContent).toMatch(/\d+(\.\d+)? \/ \d+(\.\d+)?|Not assessed|N\/A/);
  });

  it("every mark is printed as obtained / maximum, and the total and the score come from the one engine result", () => {
    const m = model();
    render(<SnapshotReport model={m} />);
    const ten = m.assessment!;
    const marks = screen.getAllByTestId("sn-marks").map((e) => e.textContent);
    expect(marks).toEqual(ten.checks.map((c) => c.marksText));
    for (const x of marks) expect(x).toMatch(/^(1|0\.5|0) \/ 1$|^N\/A$|^Not assessed$/);
    expect(screen.getByTestId("sn-total-marks")).toHaveTextContent(`${ten.totalText}`);
    expect(screen.getByTestId("sn-total-row")).toHaveTextContent(ten.scoreText);          // one engine: the total row, the ring and the story print the same score
    expect(ten.totalText).toMatch(/^\d+(\.\d)? \/ \d+$/);
  });

  it("never prints Unable to Verify, even for banner buttons the scan could not see", () => {
    const m = model();
    const elements = m.visual!.elements.map((e) => (/accept|reject|per-purpose/i.test(e.title) ? { ...e, status: "unverified" as const } : e));
    const unseen = { ...m, visual: { ...m.visual!, elements } };
    const { container } = render(<SnapshotReport model={unseen} />);
    expect(container.textContent).not.toMatch(/unable to verify/i);
    expect(screen.getByTestId("sn-banner")).toHaveTextContent("Not tested");
  });

  it("a required checkbox with a generated name in a form that collects an e-mail address is not reported as Not found", () => {
    const m = model();
    const form = { id: "f2", selector: "form", purpose_guess: "newsletter_signup", page_url: "https://x.test/contact/", fields: [{ name: "email", field_type: "email", required: true }, { name: "_R_2apbsnpfiv5stbH1_", field_type: "checkbox", required: true }] };
    const withBox = { ...m, evidence: { ...m.evidence!, forms: [form] } } as typeof m;
    render(<SnapshotReport model={withBox} />);
    const row = screen.getAllByTestId("sn-form-row")[1];
    expect(row).toHaveTextContent("Checkbox found");
    expect(row).toHaveTextContent("wording not recorded");
    expect(row).not.toHaveTextContent("Not found");
    expect(row).toHaveTextContent("x.test/contact");
  });

  it("with no checkbox in any form, explicit consent is still Not found", () => {
    const m = model();
    const noBox = { ...m, evidence: { ...m.evidence!, forms: [{ id: "f3", selector: "form", purpose_guess: null, page_url: "https://x.test/", fields: [{ name: "q", field_type: "text", required: false }] }] } } as typeof m;
    render(<SnapshotReport model={noBox} />);
    expect(screen.getAllByTestId("sn-form-row")[1]).toHaveTextContent("Not found");
  });

  it("a checkbox whose recorded wording asks for agreement is Found, and the wording is printed", () => {
    const m = model();
    const form = { id: "f4", selector: "form", purpose_guess: "newsletter_signup", page_url: "https://x.test/contact/", fields: [{ name: "email", field_type: "email", required: true }, { name: "_r_1_", field_type: "checkbox", required: true, label: "I agree to receive newsletters from CookieYes and accept the Privacy Policy." }] };
    render(<SnapshotReport model={{ ...m, evidence: { ...m.evidence!, forms: [form] } } as typeof m} />);
    const row = screen.getAllByTestId("sn-form-row")[1];
    expect(row).toHaveTextContent("Found");
    expect(row).not.toHaveTextContent("Checkbox found");
    expect(screen.getByTestId("sn-checkbox-wording")).toHaveTextContent("I agree to receive newsletters from CookieYes and accept the Privacy Policy.");
    expect(row).not.toHaveTextContent("wording not recorded");
  });

  it("a checkbox whose recorded wording is not about agreement (for example Show password) is not taken for consent", () => {
    const m = model();
    const form = { id: "f5", selector: "form", purpose_guess: "login", page_url: "https://x.test/login", fields: [{ name: "email", field_type: "email", required: true }, { name: "show", field_type: "checkbox", required: false, label: "Show password" }] };
    render(<SnapshotReport model={{ ...m, evidence: { ...m.evidence!, forms: [form] } } as typeof m} />);
    expect(screen.getAllByTestId("sn-form-row")[1]).toHaveTextContent("Not found");
  });

  it("a source is a link to the real address, and a result with evidence but no address says so instead of showing one", () => {
    const m = model();
    render(<SnapshotReport model={m} />);
    const links = screen.getAllByTestId("sn-source");
    expect(links.length).toBeGreaterThan(0);
    for (const l of links.filter((x) => x.tagName === "A")) expect(l.getAttribute("href")).toMatch(/^https?:\/\//);
    for (const l of links) expect(l.textContent).not.toMatch(/^\[https?:/);        // shown short, linked whole
  });

  it("states how many checks could not be assessed in the score band, since page 1 no longer carries a breakdown", () => {
    const m = model({ assessment: A.policyMissing });
    render(<SnapshotReport model={m} />);
    expect(screen.queryByTestId("sn-summary")).toBeNull();                                                          // the long "how to read" block is gone
    expect(screen.getByTestId("sn-scope-checks")).toHaveTextContent(/of \d+ checks assessed/);
  });

  it("shows each result's state under its pill (for example Not evidenced, Content extraction failed, Crawling restricted)", () => {
    const m = model({ assessment: A.policyMissing });
    render(<SnapshotReport model={m} />);
    const states = screen.getAllByTestId("sn-basis").map((e) => e.textContent);
    expect(states.length).toBeGreaterThan(0);
    for (const s of states) expect(s).toMatch(/^(Partially verified|Not evidenced|Not found in scanned pages|Content extraction failed|Partially extracted|Access failed|Crawling restricted|Not tested|Assessment incomplete|Evidence unavailable|Scan failed)$/);
  });

  it("page 1 shows the score only, with no breakdown; the statuses and marks stay on page 2", () => {
    const m = model();
    render(<SnapshotReport model={m} />);
    const ten = m.assessment!;
    const card = within(screen.getByTestId("sn-score"));
    expect(card.getByTestId("pg-percent-snapshot")).toHaveTextContent(ringNumber(ten.scoreText));
    for (const id of ["sn-tally", "sn-chip", "sn-total-line", "sn-score-text", "sn-counts", "sn-incomplete"]) expect(screen.queryByTestId(id)).toBeNull();
    const p1 = within(screen.getByTestId("sn-page-1")), p2 = within(screen.getByTestId("sn-page-2"));
    expect(p1.queryAllByTestId("sn-checkpoint")).toHaveLength(0);
    expect(p2.getAllByTestId("sn-checkpoint")).toHaveLength(10);
    expect(p2.getByTestId("sn-total-marks")).toHaveTextContent(ten.totalText);
  });

  it("page 1 puts the result on the left (score with its three category totals, then the six elements) and the evidence on the right (the homepage, then a compact consent banner)", () => {
    render(<SnapshotReport model={model()} />);
    const hero = document.querySelector(".sn-hero2")!;
    const [main, side] = [...hero.children];
    expect([...main.children].map((e) => e.getAttribute("data-testid"))).toEqual(["sn-score", "sn-elements"]);       // the score first, then the six privacy elements
    expect([...side.children].map((e) => e.getAttribute("data-testid"))).toEqual(["pg-hero-preview", "sn-banner"]);   // the homepage, then the banner
    expect(within(screen.getByTestId("sn-banner")).getByTestId("sn-banner-status")).toHaveTextContent("Detected");
    expect(screen.getByTestId("sn-banner-facts")).toHaveTextContent("Consent management platform");
  });

  it("says what the score rests on and that it is not a legal determination, and what was and was not covered", () => {
    const m = model();
    render(<SnapshotReport model={m} />);
    const ten = m.assessment!;
    expect(ten.checks.length).toBeGreaterThan(0);
    expect(screen.getByTestId("sn-disclaimer")).toHaveTextContent(/do not constitute legal advice/i);
    expect(screen.getByTestId("sn-scope-pages")).toHaveTextContent(/of 40 pages scanned/);
    expect(screen.getByTestId("sn-scope-consent")).toHaveTextContent(/homepage only/);
  });

  it("each result says how good its evidence is, and a passed check with a limited scope says so", () => {
    const m = model();
    render(<SnapshotReport model={m} />);
    const q = screen.getAllByTestId("sn-quality").map((e) => e.textContent);
    expect(q.length).toBeGreaterThan(0);
    for (const x of q) expect(x).toMatch(/^Evidence: (Specific|General|Limited|None)$/);
  });


  it("the total row states the marks the score is made of, and only claims the percentage when it is the printed score", () => {
    const base = A.typical;
    const sc = { version: "3.3.0", mode: "quick", status: "completed", score: 53, scale: 100, band: "Partially addressed", bandNotes: [], coverage: 64, confidence: { level: "medium", label: "Medium", reasons: [] }, explanation: "x", disclaimer: "x", whatWasNotAssessed: [], beyondQuickScan: [], removed: [], findings: [], rules: {}, scannedAt: null, categories: [{ category: "Cookie Consent", marks: 48, awarded: 12, assessable: 12 }] } as unknown as TenAssessment["scanner"];
    const make = (awarded: number, applicableMax: number, score: number | null): TenAssessment => ({ ...base, engine: "scorecard", scanner: sc, awarded, applicableMax, score, scoreText: score === null ? "No meaningful score" : `${score}/100` });
    const { unmount } = render(<SnapshotReport model={model({ assessment: make(34, 64, 53) })} />);
    expect(screen.getByTestId("sn-score-basis")).toHaveTextContent("34 of 64 assessed marks = 53%");
    expect(screen.queryByTestId("sn-score-method")).toBeNull();     // the explanatory line under the score is not printed
    unmount();
    render(<SnapshotReport model={model({ assessment: make(64, 64, 99) })} />);
    expect(screen.getByTestId("sn-score-basis")).toHaveTextContent(/^64 of 64 assessed marks$/);
  });

  it("counts the checks assessed, never the points in play", () => {
    const base = A.typical;
    // a Scanner Score result: 13 checks, 70 points in play, 10 of the checks assessed
    const scored: TenAssessment = { ...base, applicableMax: 70, awarded: 65, counts: { passed: 8, partial: 2, failed: 0, notAssessed: 2, notApplicable: 1 }, checks: Array.from({ length: 13 }, (_, i) => ({ ...base.checks[0], id: i + 1 })) };
    render(<SnapshotReport model={model({ assessment: scored })} />);
    expect(screen.getByTestId("sn-scope-checks")).toHaveTextContent("10 of 13 checks assessed");
    expect(screen.getByTestId("sn-scope-checks")).not.toHaveTextContent("70");
  });

  it("names the page the choices per purpose were found on, from the scan's own record", () => {
    const withPage = { ...EVIDENCE, consent_signals: [{ ...EVIDENCE.consent_signals[0], evidence: { interaction_page_url: "https://x.test/" } }] } as unknown as ScanEvidenceResponse;
    const m = buildReportModel(SCAN, [], META, COOKIES, EVIDENCE.trackers, buildRequirementsView(F.mixed), withPage, null, null, SHOT, undefined, undefined);
    render(<SnapshotReport model={m} />);
    const row = screen.getByText("Consent purpose (choices per purpose)").closest("div") as HTMLElement;
    expect(row.parentElement).toHaveTextContent("x.test");
    expect(row.parentElement).not.toHaveTextContent("page not recorded");
  });

  it("itemises the parameters a Quick Scan cannot reach in one compact row, every one named, none scored", () => {
    const base = A.typical;
    const beyondNames = ["A5 Notice site-wide, other language", "B5 Withdrawal", "D1 No tracking of children"];
    const scored: TenAssessment = { ...base, checks: [...base.checks, ...beyondNames.map((name, i) => ({ ...base.checks[0], id: 11 + i, ref: name.split(" ")[0], name: `${name.split(" ")[0]} Long official name of the parameter`, result: "Not Assessed" as const, awarded: null, max: null, marksText: "Deep Scan only" }))] };
    render(<SnapshotReport model={model({ assessment: scored })} />);
    expect(screen.getAllByTestId("sn-checkpoint")).toHaveLength(base.checks.length);              // the table keeps its rows
    const row = screen.getByTestId("sn-beyond");
    expect(row).toHaveTextContent("3 more parameters, left out of this score");
    for (const n of beyondNames) expect(screen.getByTestId("sn-beyond-list")).toHaveTextContent(n);
  });

  it("shows each privacy element as a green check when found, a red cross when not found, and a grey mark when not tested, always with the words", () => {
    render(<SnapshotReport model={model()} />);
    const cards = screen.getAllByTestId("sn-element");
    expect(cards).toHaveLength(6);
    for (const c of cards) {
      const state = c.getAttribute("data-state")!;
      expect(["found", "missing", "untested"]).toContain(state);
      expect(c.textContent).toContain({ found: "Found", missing: "Not found", untested: "Not tested" }[state as "found"]);        // never colour alone
      expect(c.querySelector(".sn-el-st svg")).not.toBeNull();
    }
  });

  it("scores the three categories on page 1 and groups the checks under them, with no P1 to P10 codes in the names", () => {
    const base = A.typical;
    const sc = {
      version: "3.0.0", mode: "quick", status: "completed", score: 89, scale: 100, band: "Largely addressed", bandNotes: [], coverage: 92, confidence: { level: "medium", label: "Medium", reasons: [] }, explanation: "x",
      disclaimer: "x", whatWasNotAssessed: [], beyondQuickScan: [], removed: [], findings: [], rules: {}, scannedAt: null,
      categories: [{ category: "Cookie Consent", marks: 48, awarded: 40, assessable: 44 }, { category: "Privacy Notice", marks: 36, awarded: 36, assessable: 36 }],
    } as unknown as TenAssessment["scanner"];
    const named: TenAssessment = { ...base, engine: "scorecard", scanner: sc, checks: base.checks.map((c, i) => ({ ...c, category: i < 5 ? "Cookie Consent" : "Privacy Notice", name: `Check number ${i + 1}` })) };
    render(<SnapshotReport model={model({ assessment: named })} />);
    const cats = screen.getAllByTestId("sn-category");
    expect(cats).toHaveLength(2);
    expect(cats[0]).toHaveTextContent("Cookie Consent");
    expect(cats[0]).toHaveTextContent("40 of 44 marks");
    expect(screen.getAllByTestId("sn-group")).toHaveLength(2);
    expect(screen.getAllByTestId("sn-group")[0]).toHaveTextContent("Cookie Consent40 of 44 marks");
    expect(screen.getByTestId("sn-checkpoints").textContent).not.toMatch(/P(10|[1-9])/);
    expect(screen.getByTestId("sn-band")).toHaveTextContent("Largely addressed");
  });

  it("closes with a call to action about fixes and remediation, going to the same address as before", () => {
    const failing: TenAssessment = { ...A.typical, checks: A.typical.checks.map((c, i) => ({ ...c, result: i < 3 ? ("Failed" as const) : ("Passed" as const) })) };
    render(<SnapshotReport model={model({ assessment: failing })} />);
    const cta = screen.getByTestId("sn-cta");
    expect(cta).toHaveTextContent("Unlock Your Complete Privacy Assessment");
    expect(cta).toHaveTextContent("Run a Full Scan to gain a complete view");
    expect(cta.textContent).not.toMatch(/Want to run your own/);
    expect(screen.getByTestId("sn-signup")).toHaveTextContent("Apply Remediations");
    expect(screen.getByTestId("sn-signup")).toHaveAttribute("href", SIGNUP_URL);
  });

  it("the call to action follows the result: one gap reads in the singular, none offers keeping watch", () => {
    expect(remediationCta({ ...A.typical, checks: A.typical.checks.map((c, i) => (i === 0 ? { ...c, result: "Partially Passed" as const } : { ...c, result: "Passed" as const })) })).toMatchObject({ gaps: 1, title: "1 check needs fixes", button: "Apply Remediations" });
    const none = remediationCta({ ...A.typical, checks: A.typical.checks.map((c) => ({ ...c, result: "Passed" as const })) });
    expect(none).toMatchObject({ gaps: 0, title: "No gaps found in this scan", button: "Schedule Re-scans" });
    expect(none.sub).not.toMatch(/violation|penalty|fine|fear/i);
  });
});
