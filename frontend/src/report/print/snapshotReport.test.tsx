import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpScoreResponse, EvidenceCookieItem, ScanEvidenceResponse, ScanScreenshot, WebsiteScanMetadata } from "../../api/types";
import fixtures from "../../test/dpdpScoreFixtures.json";
import { buildReportModel } from "../buildReport";
import { buildScoreView } from "../dpdpScore";
import { buildTenChecks } from "../tenChecks";
import { SNAPSHOT_DISCLAIMER, SnapshotReport } from "./SnapshotReport";

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
    expect(p2.getByTestId("sn-consent")).toHaveTextContent("After Reject");
    expect(p2.getAllByTestId("sn-checkpoint").length).toBeGreaterThan(0);
    expect(p2.getByTestId("sn-pages")).toHaveTextContent("x.test/privacy");
    expect(p2.getByTestId("sn-page-summary")).toHaveTextContent("Pages scanned");
    expect(p2.getByTestId("sn-page-summary")).toHaveTextContent("Not recorded per page");   // the scan does not record the banner or tracking per page: nothing is invented
    expect(p2.getByTestId("sn-next").children.length).toBeGreaterThan(0);
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
    const ten = buildTenChecks(m);
    const marks = screen.getAllByTestId("sn-marks").map((e) => e.textContent);
    expect(marks).toEqual(ten.checks.map((c) => c.marksText));
    for (const x of marks) expect(x).toMatch(/^(1|0\.5|0) \/ 1$|^N\/A$|^Not assessed$/);
    expect(screen.getByTestId("sn-total-marks")).toHaveTextContent(`${ten.totalText}`);
    expect(screen.getByTestId("sn-total-row")).toHaveTextContent(ten.scoreText);
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
    const m = model();
    const bare = { ...m, policyAnalysis: null, evidence: { ...m.evidence!, policies: [] } } as typeof m;
    render(<SnapshotReport model={bare} />);
    expect(screen.getByTestId("sn-summary")).toHaveTextContent(/could not be assessed and are left out/);
  });

  it("shows each result's state under its pill (for example Not evidenced, Content extraction failed, Crawling restricted)", () => {
    const m = model();
    const bare = { ...m, policyAnalysis: null, evidence: { ...m.evidence!, policies: [] } } as typeof m;
    render(<SnapshotReport model={bare} />);
    const states = screen.getAllByTestId("sn-basis").map((e) => e.textContent);
    expect(states.length).toBeGreaterThan(0);
    for (const s of states) expect(s).toMatch(/^(Partially verified|Not evidenced|Not found in scanned pages|Content extraction failed|Partially extracted|Access failed|Crawling restricted|Not tested|Assessment incomplete|Evidence unavailable|Scan failed)$/);
  });

  it("page 1 shows the score only, with no breakdown; the statuses and marks stay on page 2", () => {
    const m = model();
    render(<SnapshotReport model={m} />);
    const ten = buildTenChecks(m);
    const card = within(screen.getByTestId("sn-score"));
    expect(card.getByTestId("pg-percent-snapshot")).toHaveTextContent(String(ten.score));
    for (const id of ["sn-tally", "sn-chip", "sn-total-line", "sn-score-text", "sn-counts", "sn-incomplete"]) expect(screen.queryByTestId(id)).toBeNull();
    const p1 = within(screen.getByTestId("sn-page-1")), p2 = within(screen.getByTestId("sn-page-2"));
    expect(p1.queryAllByTestId("sn-checkpoint")).toHaveLength(0);
    expect(p2.getAllByTestId("sn-checkpoint")).toHaveLength(10);
    expect(p2.getByTestId("sn-total-marks")).toHaveTextContent(ten.totalText);
  });

  it("the consent banner sits in the left column under the score, with a prominent status and only what was detected", () => {
    render(<SnapshotReport model={model()} />);
    const left = document.querySelector(".sn-left")!;
    const kids = [...left.children].map((e) => e.getAttribute("data-testid"));
    expect(kids).toEqual(["sn-score", "sn-banner"]);
    expect(within(screen.getByTestId("sn-banner")).getByTestId("sn-banner-status")).toHaveTextContent("Detected");
    expect(screen.getByTestId("sn-banner-facts")).toHaveTextContent("Consent management platform");
  });

  it("says what the score rests on and that it is not a legal determination, and what was and was not covered", () => {
    const m = model();
    render(<SnapshotReport model={m} />);
    const ten = buildTenChecks(m);
    expect(screen.getByTestId("sn-summary")).toHaveTextContent(ten.summary);
    expect(screen.getByTestId("sn-summary")).toHaveTextContent(/not a legal determination or certification/);
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

  it("recommendations name the check they come from and none is worded as a violation", () => {
    const m = model();
    const weak = { ...m, policyAnalysis: null } as typeof m;
    render(<SnapshotReport model={weak} />);
    const items = within(screen.getByTestId("sn-next")).getAllByRole("listitem").map((e) => e.textContent ?? "");
    expect(items.length).toBeGreaterThan(0);
    for (const x of items) expect(x).not.toMatch(/violat|illegal/i);
  });
});
