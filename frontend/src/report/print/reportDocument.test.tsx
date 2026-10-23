import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DpdpScoreResponse, PolicyAnalysis, ScanEvidenceResponse, ScanScreenshot, WebsiteScanMetadata } from "../../api/types";
import fixtures from "../../test/dpdpScoreFixtures.json";
import { buildReportModel } from "../buildReport";
import { buildScoreView } from "../dpdpScore";
import { REPORT_DISCLAIMER } from "../reportScope";
import { checkContribution, stripId, topFindingRows } from "../reportText";
import { ReportDocument } from "./ReportDocument";
import { isReportCategory } from "../reportScope";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
const SCAN = { id: "11111111-1111-1111-1111-111111111111", url: "https://x.test/", evidence_counts: { pages: 3, cookies: 2, trackers: 5, third_party_services: 1, policies: 1, forms: 1 } } as never;
const META = { consent_mechanism: "cmp", cmp_vendor: "Some CMP", accept_interaction: "clicked", reject_interaction: "clicked", pages_scanned: 3 } as unknown as WebsiteScanMetadata;
const LONG_URL = "https://tracker.example/collect?v=1&tid=UA-1234567-1&cid=" + "a".repeat(180);
const tracker = (id: string, category: string | null, states: string[], src = `https://svc${id}.example/t.js`) => ({ id, script_src: src, vendor: category ? `Vendor ${id}` : null, category, source: "rule", consent_states: states, page_url: "https://x.test/" });
const EVIDENCE = {
  pages: [{ id: "p1", url: "https://x.test/", title: "Home", http_status: 200, discovered_via: "seed" }, { id: "p2", url: "https://x.test/privacy", title: "Privacy", http_status: 200, discovered_via: "link" }],
  forms: [{ id: "f1", selector: "form#contact", purpose_guess: "contact_or_support", fields: [{ name: "email", field_type: "email", required: true }, { name: "message", field_type: "textarea", required: false }] }],
  cookies: [{ id: "c1", name: "_ga", domain: "x.test", category: "analytics", vendor: "Google Analytics", is_first_party: true, source: "rule", consent_states: ["pre_consent", "post_accept"], provider: "Google Analytics", purpose: "Counts visitors.", purpose_source: "catalogue", is_session: false, lifetime_days: 400, expiry: "2027-11-26T00:00:00+00:00", observations: [] }],
  trackers: [tracker("1", "marketing", ["pre_consent", "post_accept"]), tracker("2", "analytics", ["post_accept"]), tracker("3", null, ["pre_consent"], LONG_URL)],
  third_party_services: [{ id: "s1", service_name: "Stripe", category: "payments", domains: ["js.stripe.com"] }],
  policies: [{ id: "pol1", url: "https://x.test/privacy", policy_type: "privacy_policy" }],
  consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "Some CMP", has_reject_all: true, has_granular_choices: true, evidence: { accept_interaction: "clicked" } }],
} as unknown as ScanEvidenceResponse;
const POLICY = {
  status: "analysed", reason: null, sector: null, documents: [], notes: [], limitations: ["Only the policy text was read."], model: null,
  summary: { applicable: 2, verified: 1, partial: 0, not_found: 1, not_applicable: 0, quotes_rejected: 0, completeness_percent: 50 },
  requirements: [
    { id: "notice_data_itemised", title: "Itemised data", group: "Notice", basis: "required", provisions: ["DPDP Act s.5(1)(i)"], status: "verified", quote: "We collect your name and email address.", source_url: "https://x.test/privacy", detail: "d", fix: "f", contacts: null, response_days: null },
    { id: "retention", title: "Retention periods", group: "Transparency", basis: "practice", provisions: ["DPDP Act s.8(7)"], status: "not_found", quote: "", source_url: null, detail: "d", fix: "Say how long data is kept.", contacts: null, response_days: null },
  ],
} as unknown as PolicyAnalysis;
const SHOT: ScanScreenshot = { scan_id: "11111111-1111-1111-1111-111111111111", domain: "x.test", status: "ok", available: true, message: null, page_url: "https://x.test/", mime: "image/jpeg", width: 1280, height: 720, captured_at: "2026-10-22T16:43:00Z", data: "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/" };

const model = (over: { score?: DpdpScoreResponse | null; evidence?: ScanEvidenceResponse | null; policy?: PolicyAnalysis | null; shot?: ScanScreenshot | null; meta?: WebsiteScanMetadata } = {}) =>
  buildReportModel(SCAN, [], over.meta ?? META, [], [], over.score === null ? null : buildScoreView(over.score ?? F.mixed), over.evidence === undefined ? EVIDENCE : over.evidence, null, over.policy === undefined ? POLICY : over.policy, over.shot === undefined ? SHOT : over.shot);

const text = () => document.body.textContent ?? "";
const flat = (x: string) => x.replace(/\s+/g, "");

describe("the printable report: the hero", () => {
  it("puts the score, what it means and the website preview in one container, in that order, side by side", async () => {
    render(<ReportDocument model={model()} />);
    const hero = screen.getByTestId("rpt-hero");
    expect(Array.from(hero.children).map((c) => c.getAttribute("data-testid") ?? c.className)).toEqual(["rpt-hero-score", "r-hero-text", "rpt-hero-preview"]);
    expect(within(hero).getByTestId("rpt-hero-screenshot")).toBeInTheDocument();
    expect(within(hero).getByTestId("rpt-hero-preview")).toHaveTextContent("x.test");           // the domain heads the preview
    const nodeFs = "node:fs";   // read the stylesheet itself; kept out of the type checker, which has no Node types
    const { readFileSync } = await import(/* @vite-ignore */ nodeFs);
    const css: string = readFileSync("src/report/print/report.css", "utf8");
    expect(css).toMatch(/\.r-hero\s*\{[^}]*grid-template-columns:\s*150px minmax\(0, 1fr\) 232px/);   // three areas in one row
    const printBlock = css.slice(css.indexOf("@media print"));
    expect(printBlock).not.toMatch(/\.r-hero\s*\{[^}]*grid-template-columns/);                    // and printing never stacks them
  });

  it("shows the backend's compliance score in the ring and no coverage anywhere in the hero", () => {
    render(<ReportDocument model={model()} />);
    expect(screen.getByTestId("hero-percent").textContent).toBe("56.5%");
    expect(screen.getByTestId("rpt-hero").textContent ?? "").not.toMatch(/coverage/i);
    expect(screen.getByTestId("rpt-hero-headline")).toHaveTextContent("we could test scored 56.5%");
  });

  it("says the score is unavailable, with no number and no coverage, when too few requirements could be assessed", () => {
    render(<ReportDocument model={model({ score: F.insufficient })} />);
    const hero = screen.getByTestId("rpt-hero");
    expect(screen.getByTestId("hero-percent").textContent).toBe("No score");
    expect(hero.textContent).toContain("Compliance score unavailable");
    expect(hero.textContent ?? "").not.toMatch(/\d+(\.\d+)?%|coverage/i);
  });

  it("says the preview is not available, in plain words, when there is no picture", () => {
    render(<ReportDocument model={model({ shot: null })} />);
    expect(screen.getByTestId("rpt-hero-missing")).toHaveTextContent("Homepage preview not available");
    expect(screen.queryByTestId("rpt-hero-screenshot")).not.toBeInTheDocument();
  });
});

describe("the printable report: every section of the PDF, in the PDF's order", () => {
  const TITLES = [
    "Scan summary", "Framework readiness", "Privacy areas", "Top findings", "Consent behaviour", "Tracking technologies detected", "Privacy elements on the site",
    "Every check behind the score", "Prioritised fixes", "Grievance officer", "Data Protection Officer", "Indian-language support", "Children's data and age gate",
    "Observable security signals", "Five-page DPDP readiness review", "Website Pages Scanned", "Sector and applicability",
    "Privacy policy content analysis", "Consent banner", "Forms found", "Cookie inventory", "Third-party requests and scripts", "Policies found", "Third-party services",
    "Compliance findings in full",
  ];

  it("numbers the sections 1, 2, 3 ... without a gap, and lists each in the contents", () => {
    render(<ReportDocument model={model()} />);
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent ?? "");
    const titles = headings.map((h) => h.replace(/^\d+/, "").replace(/\d+ (cookies?|findings?|forms?|requests?|policies|policy|services?|mechanisms?).*$/, ""));
    for (const t of TITLES.filter((x) => x !== "Sector and applicability")) expect(titles.some((h) => h.startsWith(t)), t).toBe(true);
    headings.forEach((h, i) => expect(h.startsWith(String(i + 1)), `heading ${i + 1}: ${h}`).toBe(true));
    const contents = within(screen.getByRole("navigation", { name: "Contents" })).getAllByRole("link");
    expect(contents).toHaveLength(headings.length);
    const order = TITLES.filter((t) => headings.some((h) => h.replace(/^\d+/, "").startsWith(t))).map((t) => headings.findIndex((h) => h.replace(/^\d+/, "").startsWith(t)));
    expect(order).toEqual([...order].sort((a, b) => a - b));
  });

  it("states before consent, after Accept and after Reject, and Not tested where a state was not exercised", () => {
    render(<ReportDocument model={model({ meta: { ...META, accept_interaction: "cmp_not_found", reject_interaction: "cmp_not_found" } as WebsiteScanMetadata })} />);
    const sec = screen.getByTestId("rpt-sec-consent");
    for (const w of ["Before consent", "After Accept", "After Reject"]) expect(sec.textContent).toContain(w);
    expect(sec.textContent).toContain("Not tested");
    expect(screen.getByTestId("rpt-sec-cookies").textContent).toMatch(/Not tested/);
  });

  it("prints every cookie, request, form field, policy and service from the evidence, and long URLs may break", () => {
    const { container } = render(<ReportDocument model={model()} />);
    const t = flat(text());
    expect(t).toContain(flat("_ga"));
    expect(t).toContain(flat(LONG_URL));
    expect(t).toContain(flat("Stripe")); expect(t).toContain(flat("js.stripe.com"));
    expect(t).toContain(flat("form#contact")); expect(t).toContain(flat("message"));
    expect(t).toContain(flat("We collect your name and email address."));
    expect(container.querySelector(".r-break")).not.toBeNull();
    expect(flat(text())).not.toContain("…");                                                    // nothing is cut short with an ellipsis (apart from the page's own loading dots)
  });

  it("closes with the informational disclaimer", () => {
    render(<ReportDocument model={model()} />);
    expect(screen.getByTestId("rpt-about")).toHaveTextContent(REPORT_DISCLAIMER);
  });
});

describe("the printable report and the PDF drawn in the browser read the same values", () => {
  it("carries every check, finding row and tile the shared view builds", () => {
    const m = model();
    render(<ReportDocument model={m} />);
    const t = flat(text());
    for (const r of m.dpdpScore!.allChecks.filter((c) => c.statusKey !== "not_applicable" && isReportCategory(c.category))) {
      expect(t, r.id).toContain(flat(r.title));
      expect(t, `${r.id} contribution`).toContain(flat(checkContribution(r)));
      expect(t, `${r.id} verdict`).toContain(flat(r.verdict));
    }
    for (const f of topFindingRows(m).slice(0, 3)) expect(t).toContain(flat(stripId(f.title)));
    for (const tile of ["Third-party requests", "Cookies set", "Consent states tested"]) expect(t).toContain(flat(tile));
    expect(t).toContain(flat(m.dpdpScore!.headline));
    for (const s of m.insights) { expect(t).toContain(flat(s.status)); expect(t).toContain(flat(s.summary)); }
    expect(t).toContain(flat(m.readiness.scoreLine));
    for (const step of m.dpdpScore!.remediationPlan) expect(t).toContain(flat(step.title));
  });
});

describe("missing optional data", () => {
  it("draws without the evidence, the policy analysis or the score, and says what is missing", () => {
    render(<ReportDocument model={model({ evidence: null, policy: null, score: null, shot: null })} />);
    expect(screen.getByTestId("hero-percent").textContent).toBe("No score");
    expect(text()).toContain("The scan evidence could not be loaded");
    expect(text()).toContain("The score was not loaded");
    expect(screen.queryByTestId("rpt-sec-cookies")).not.toBeInTheDocument();                    // no evidence appendix, and no "none detected" over evidence that was not read
    expect(screen.getByTestId("rpt-about")).toBeInTheDocument();
  });

  it("prints the provisional and the analysis-incomplete notes beside the score", () => {
    const m = { ...model(), provisional: true, analysisNote: "The analysis did not complete for this scan." };
    render(<ReportDocument model={m} />);
    expect(screen.getByTestId("rpt-analysis-note")).toHaveTextContent("The analysis did not complete");
    expect(text()).toContain("Provisional");
  });
});
