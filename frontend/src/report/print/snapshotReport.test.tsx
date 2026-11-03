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
    expect(p1.getByTestId("sn-score")).toHaveTextContent("Compliance Score");
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
    for (const r of rows) expect(["Passed", "Partially Passed", "Failed", "Not Applicable"]).toContain(r.getAttribute("data-status"));
    expect(screen.queryByTestId("sn-formula")).toBeNull();   // the formula line is not printed in the report
    expect(document.body.textContent).not.toMatch(/unable to verify/i);
    expect(screen.getAllByTestId("sn-why").length).toBeGreaterThan(0);   // every Failed or Partially Passed check says why
  });

  it("every checkpoint is listed once, with its score", () => {
    render(<SnapshotReport model={model()} />);
    const rows = screen.getAllByTestId("sn-checkpoint");
    expect(new Set(rows.map((r) => r.textContent)).size).toBe(rows.length);
    for (const r of rows) expect(r.textContent).toMatch(/\d+(\.\d+)? \/ \d+(\.\d+)?|–/);
  });

  it("every mark is printed as obtained / maximum, and the total and the score come from the one engine result", () => {
    const m = model();
    render(<SnapshotReport model={m} />);
    const ten = buildTenChecks(m);
    const marks = screen.getAllByTestId("sn-marks").map((e) => e.textContent);
    expect(marks).toEqual(ten.checks.map((c) => c.marksText));
    for (const x of marks) expect(x).toMatch(/^(1|0\.5|0) \/ 1$|^N\/A$/);
    expect(screen.getByTestId("sn-total-marks")).toHaveTextContent(`${ten.totalText}`);
    expect(screen.getByTestId("sn-total-row")).toHaveTextContent(ten.scoreText);
    expect(screen.getByTestId("sn-total-line")).toHaveTextContent(`Total Weightage Awarded: ${ten.totalText}`);
    expect(screen.getByTestId("sn-score-text")).toHaveTextContent(`Privacy Readiness Score: ${ten.scoreText}`);
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
});
