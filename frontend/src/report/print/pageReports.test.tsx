import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpScoreResponse, ScanEvidenceResponse, ScanScreenshot, WebsiteScanMetadata } from "../../api/types";
import fixtures from "../../test/dpdpScoreFixtures.json";
import { buildReportModel } from "../buildReport";
import { buildScoreView } from "../dpdpScore";
import { buildPageReport, FREE_PAGES, FULL_PAGES, reportIdFor } from "../pageReports";
import { ctaKeyFor, resolveCta } from "../reportCta";
import { FreeReport } from "./FreeReport";
import { FullReport } from "./FullReport";
import { ReportDocument } from "./ReportDocument";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
const SCAN = { id: "9bd5ccef-6909-4903-8d44-2b52a5668a06" as string, url: "https://x.test/", evidence_counts: { pages: 3, cookies: 2, trackers: 5, third_party_services: 1, policies: 1, forms: 1 } } as never;
const META = { consent_mechanism: "cmp", cmp_vendor: "Some CMP", accept_interaction: "clicked", reject_interaction: "clicked", pages_scanned: 3, pages_discovered: 40, page_limit: 5 } as unknown as WebsiteScanMetadata;
const tracker = (id: string, vendor: string | null, category: string | null, states: string[], src: string) => ({ id, script_src: src, vendor, category, source: "rule", consent_states: states, page_url: "https://x.test/" });
const EVIDENCE = {
  pages: [{ id: "p1", url: "https://x.test/", title: "Home", http_status: 200, discovered_via: "seed" }],
  forms: [{ id: "f1", selector: "form#c", purpose_guess: "contact_or_support", fields: [{ name: "email", field_type: "email", required: true }] }],
  cookies: [{ id: "c1", name: "_ga", domain: "x.test", category: "analytics", vendor: "Google Analytics", is_first_party: true, source: "rule", consent_states: ["post_accept"], provider: "GA", purpose: "p", purpose_source: "catalogue", is_session: false, lifetime_days: 400, expiry: "2027-11-26T00:00:00+00:00", observations: [] }],
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
const model = (o: { score?: keyof typeof F | null; shot?: ScanScreenshot | null; meta?: WebsiteScanMetadata; evidence?: ScanEvidenceResponse | null } = {}) =>
  buildReportModel(SCAN, [], o.meta ?? META, [], [], o.score === null ? null : buildScoreView(F[o.score ?? "mixed"]), o.evidence === undefined ? EVIDENCE : o.evidence, null, null, o.shot === undefined ? SHOT : o.shot);

afterEach(cleanup);

describe("which call to action is shown (scope 8.3)", () => {
  const key = (score: number | null, coverage: number | null, unassessed = 0, critical = false) => ctaKeyFor({ score, coverage, unassessed, critical });
  it("follows the score band", () => {
    expect(key(20, 80)).toBe("action_plan");
    expect(key(55, 80)).toBe("improve");
    expect(key(80, 80)).toBe("close_gaps");
  });
  it("shows monitoring only for a high score with high coverage and nothing left unassessed", () => {
    expect(key(95, 90)).toBe("monitor");
    expect(key(95, 30)).toBe("expand");
    expect(key(95, 90, 2)).toBe("complete");
    expect(key(100, 30, 5)).toBe("expand");
  });
  it("puts a confirmed critical finding ahead of any score", () => {
    expect(key(98, 95, 0, true)).toBe("fix_priority");
    expect(key(10, 50, 0, true)).toBe("fix_priority");
  });
  it("asks to complete the assessment when the score is withheld", () => { expect(key(null, null)).toBe("complete"); });
  it("gives a subscriber the operational action, never the acquisition one, and carries nothing from the scan in the address", () => {
    const sub = resolveCta({ score: 55, coverage: 80, unassessed: 0, critical: false, subscriber: true });
    expect(sub.label).toBe("Create Remediation Tasks");
    expect(resolveCta({ score: 55, coverage: 80, unassessed: 0, critical: false }).label).toBe("Improve My Score with Consiva AI");
    expect(sub.href).not.toMatch(/[?&=]/);
  });
});

describe("the page report view", () => {
  it("names a report from its kind, the scan's date and the start of its id, so it repeats", () => {
    expect(reportIdFor("free", "9bd5ccef-6909-4903-8d44", "22 Oct 2026, 20:08 UTC")).toBe("FR-20261022-9BD5");
    expect(reportIdFor("full", "9bd5ccef-6909", "22 Oct 2026, 20:08 UTC")).toBe("FS-20261022-9BD5");
    expect(reportIdFor("free", null, "not recorded")).toBe("FR");
  });

  it("counts the framework segments from the score and never counts an unknown as met", () => {
    const view = buildPageReport(model(), "free");
    const c = view.framework.counts;
    const v = buildScoreView(F.mixed);
    expect(c.met).toBe(v.assessed.filter((r) => r.statusKey === "met").length);
    expect(c.unverified).toBe(v.unableToVerify.length);
    expect(c.notApplicable).toBe(v.excluded.length);
    expect(view.framework.segments.reduce((n, s) => n + s.count, 0)).toBe(c.met + c.partly + c.notMet + c.unverified + c.notApplicable);
    expect(Math.round(view.framework.segments.reduce((n, s) => n + s.pct, 0))).toBe(100);
  });

  it("takes the consent and technology figures from the one visual builder, so the pages cannot count differently", () => {
    const m = model();
    const view = buildPageReport(m, "free");
    expect(view.consent).toBe(m.visual!.consent);
    expect(view.tiles.map((t) => t.value)).toContain(String(m.visual!.requests));
    expect(view.techCards.map((t) => t.name)).toContain("Google Ads");
  });

  it("takes a technology's category from the service catalogue and says Not classified when there is none", () => {
    const rows = buildPageReport(model(), "full").techRows;
    expect(rows.find((r) => r.name === "Google Ads")!.category).toBe("Advertising");
    expect(rows.find((r) => r.name === "Static CDN")!.category).toBe("Not classified");
  });

  it("counts each request once in every state it was seen, and marks a state that was not tested instead of drawing zero", () => {
    const view = buildPageReport(model(), "full");
    const [before, accept, reject] = view.series;
    expect(before).toMatchObject({ analytics: 0, functional: 1, other: 1 });
    expect(accept).toMatchObject({ analytics: 2, functional: 1, other: 0 });
    expect(reject).toMatchObject({ analytics: 0, functional: 1 });
    const untested = buildPageReport(model({ meta: { ...META, accept_interaction: "not_found" } as WebsiteScanMetadata }), "full").series[1];
    expect(untested.tested).toBe(false);
    expect(untested.analytics + untested.functional + untested.other).toBe(0);
  });

  it("states what the scan cannot say: pages, homepage-only consent tests and unassessed checks", () => {
    const limits = buildPageReport(model(), "free").limits.join(" ");
    expect(limits).toMatch(/not an assessment of the whole site/);
    expect(limits).toMatch(/homepage only/);
    expect(limits).toMatch(/not counted as passed/);
  });
});

describe("the Free Scan report", () => {
  it("is exactly two sheets, numbered Page 1 of 2 and Page 2 of 2", () => {
    render(<FreeReport model={model()} />);
    expect(document.querySelectorAll(".pg").length).toBe(FREE_PAGES);
    expect(screen.getByTestId("pg-free-1")).toHaveTextContent("Page 1 of 2");
    expect(screen.getByTestId("pg-free-2")).toHaveTextContent("Page 2 of 2");
  });

  it("puts the screenshot, the score with its coverage and the call to action on page 1, and consent behaviour on page 2", () => {
    render(<FreeReport model={model()} />);
    const p1 = screen.getByTestId("pg-free-1");
    const p2 = screen.getByTestId("pg-free-2");
    expect(within(p1).getByTestId("pg-hero-screenshot")).toBeInTheDocument();
    expect(within(p1).getByTestId("pg-hero")).toHaveTextContent(/Website Privacy\s*Readiness Report/);
    expect(within(p1).getByTestId("pg-score-free")).toHaveTextContent("Privacy Readiness Score");
    expect(within(p1).getByTestId("pg-score-free")).toHaveTextContent(/Coverage/);
    expect(within(p1).getByTestId("pg-cta")).toBeInTheDocument();
    expect(within(p2).getByTestId("pg-consent")).toHaveTextContent("After Reject");
    expect(within(p2).getByTestId("pg-takeaway")).toBeInTheDocument();
    expect(within(p2).getByTestId("pg-disclaimer")).toHaveTextContent("does not constitute legal advice");
  });

  it("says the preview is not available, in words, when there is no picture, and never draws an invented one", () => {
    render(<FreeReport model={model({ shot: null })} />);
    expect(screen.getByTestId("pg-hero-missing")).toBeInTheDocument();
    expect(screen.queryByTestId("pg-hero-screenshot")).toBeNull();
  });

  it("shows no score number when the score is withheld, and asks to complete the assessment", () => {
    render(<FreeReport model={model({ score: "insufficient" })} />);
    expect(screen.getByTestId("pg-percent-free").textContent).toBe("No score");
    expect(screen.getByTestId("pg-cta")).toHaveAttribute("data-cta", "complete");
  });

  it("still draws both sheets, with their words, when the evidence did not load", () => {
    render(<FreeReport model={model({ evidence: null })} />);
    expect(document.querySelectorAll(".pg").length).toBe(2);
    expect(screen.getByTestId("pg-free-2")).toHaveTextContent("not loaded");
  });
});

describe("the Full Scan report", () => {
  it("is eight sheets, each numbered out of 8, with the report id on the pages after the first", () => {
    render(<FullReport model={model()} />);
    expect(document.querySelectorAll(".pg").length).toBe(FULL_PAGES);
    for (let n = 1; n <= FULL_PAGES; n++) expect(screen.getByTestId(`pg-full-${n}`)).toHaveTextContent(`Page ${n} of 8`);
    expect(screen.getByTestId("pg-full-3")).toHaveTextContent(/Report ID: FS-\d{8}-9BD5/);
  });

  it("carries every section of the benchmark: summary, framework, areas, findings, consent, technologies, domains, chart, elements, checks, fixes, legal context", () => {
    render(<FullReport model={model()} />);
    for (const title of ["Quick Summary", "Framework Readiness", "Privacy Areas", "Top Findings", "Consent Behaviour", "Tracking Technologies Detected", "Third-party Domains", "Requests over Time", "Privacy Elements on the Site", "Every Check Behind the Score", "Prioritised Fixes", "DPDP Act and Rules", "What's Next"]) {
      expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
    }
    expect(screen.getByTestId("pg-chart")).toBeInTheDocument();
    expect(screen.getByTestId("pg-reportinfo")).toHaveTextContent("Scoring methodology");
  });

  it("numbers its sections 1 to 13 in order", () => {
    render(<FullReport model={model()} />);
    const badges = Array.from(document.querySelectorAll(".pg-badge")).map((b) => b.textContent);
    expect(badges.filter((b) => /^\d+$/.test(b ?? ""))).toEqual(Array.from({ length: 13 }, (_, i) => String(i + 1)));
  });

  it("gives the subscriber report an operational call to action and no acquisition one", () => {
    render(<FullReport model={model()} />);
    expect(screen.queryByText("Upgrade to Full Scan", { selector: "a" })).toBeNull();
    expect(screen.getByTestId("pg-cta").querySelector("a")!.textContent).not.toMatch(/Improve My Score|Get My DPDP Action Plan/);
  });

  it("states the legal dates from the notifications the score is built on", () => {
    render(<FullReport model={model()} />);
    const legal = screen.getByTestId("pg-legal");
    expect(legal).toHaveTextContent("13 Nov 2025");
    expect(legal).toHaveTextContent("G.S.R. 892(E)");
    expect(legal).toHaveTextContent("10 Dec 2025");
    expect(screen.getByTestId("pg-full-2")).toHaveTextContent("13 May 2027");
  });

  it("lists the policies and forms the scan found, and says plainly when there are none", () => {
    render(<FullReport model={model()} />);
    expect(screen.getByTestId("pg-policies")).toHaveTextContent("https://x.test/privacy");
    expect(screen.getByTestId("pg-forms")).toHaveTextContent("email");
    cleanup();
    render(<FullReport model={model({ evidence: { ...EVIDENCE, policies: [], forms: [] } as ScanEvidenceResponse })} />);
    expect(screen.getByTestId("pg-policies")).toHaveTextContent("found in the scanned scope");
  });

  it("marks a consent state that was not tested in the chart's axis", () => {
    render(<FullReport model={model({ meta: { ...META, reject_interaction: "not_found" } as WebsiteScanMetadata })} />);
    expect(screen.getByTestId("pg-chart")).toHaveTextContent("After Reject (not tested)");
  });
});

describe("the appendix that follows the fixed pages", () => {
  it("leaves out what the pages already carry and keeps the detail they cut", () => {
    render(<ReportDocument model={model()} appendixOnly />);
    expect(screen.getByTestId("rpt-appendix")).toBeInTheDocument();
    expect(screen.queryByTestId("rpt-hero")).toBeNull();
    expect(screen.queryByTestId("rpt-sec-summary")).toBeNull();
    expect(screen.queryByTestId("rpt-sec-consent")).toBeNull();
    expect(screen.getByTestId("rpt-sec-checks")).toBeInTheDocument();
    expect(screen.getByTestId("rpt-sec-cookies")).toBeInTheDocument();
    expect(screen.getByTestId("rpt-sec-requests")).toBeInTheDocument();
  });

  it("is still the whole long report when no variant is asked for", () => {
    render(<ReportDocument model={model()} />);
    expect(screen.getByTestId("rpt-hero")).toBeInTheDocument();
    expect(screen.getByTestId("rpt-sec-summary")).toBeInTheDocument();
  });
});

describe("the long report's front page", () => {
  it("is the Free report's first sheet with no page number, and the long report below it no longer repeats the title and hero", async () => {
    const { ReportCover } = await import("./FreeReport");
    render(<><ReportCover model={model()} /><ReportDocument model={model()} noFront /></>);
    const cover = screen.getByTestId("pg-free-1");
    expect(cover).toHaveTextContent(/Website Privacy\s*Readiness Report/);
    expect(cover).not.toHaveTextContent(/Page \d+ of/);
    expect(cover).toHaveTextContent("PRIVACY COMPLIANCE REPORT");
    expect(within(cover).getByTestId("pg-hero-screenshot")).toBeInTheDocument();
    expect(screen.queryByTestId("rpt-hero")).toBeNull();
    expect(screen.getByTestId("rpt-sec-checks")).toBeInTheDocument();     // every later section is still there
    expect(screen.getByTestId("rpt-about")).toBeInTheDocument();
  });
});

describe("report figures fixed by the 2026-10-26 audit", () => {
  const manyTrackers = Array.from({ length: 12 }, (_, i) => tracker(String(100 + i), `Vendor ${i}`, i % 2 ? "analytics" : "marketing", ["post_accept"], `https://v${i}.example.net/t.js`));
  const bigEvidence = { ...EVIDENCE, trackers: manyTrackers } as ScanEvidenceResponse;

  it("counts the technologies still unlisted against what each report actually shows", () => {
    const free = buildPageReport(model({ evidence: bigEvidence }), "free");
    const full = buildPageReport(model({ evidence: bigEvidence }), "full");
    expect(free.techCards.length).toBe(5);
    expect(free.techMore).toBe(12 - 5);                    // the Free cards show 5
    expect(full.techRows.length).toBe(8);
    expect(full.techRowsMore).toBe(12 - 8);                // the Full table shows 8, so 4 remain, not 7
  });

  it("does not call the page limit the Free limit on the Full report", () => {
    expect(buildPageReport(model(), "free").tiles[0].label).toContain("Free scan limit");
    expect(buildPageReport(model(), "full").tiles[0].label).not.toContain("Free");
  });

  it("puts a confirmed finding rated High ahead of a high score, since the score never rates a finding Critical", () => {
    for (const key of Object.keys(F) as (keyof typeof F)[]) {
      const m = model({ score: key });
      const hasHigh = !!m.dpdpScore?.groups.find((g) => g.key === "confirmed")?.entries.some((e) => /high/i.test(e.severity));
      expect(buildPageReport(m, "free").cta.key === "fix_priority", String(key)).toBe(hasHigh);
    }
  });
});
