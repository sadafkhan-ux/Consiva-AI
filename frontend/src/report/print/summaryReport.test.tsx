import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpScoreResponse, EvidenceCookieItem, ScanEvidenceResponse, ScanScreenshot, WebsiteScanMetadata } from "../../api/types";
import fixtures from "../../test/dpdpScoreFixtures.json";
import { buildReportModel } from "../buildReport";
import { buildScoreView } from "../dpdpScore";
import type { InsightSection } from "../insights";
import {
  buildSummaryReport, COOKIE_ROWS, classificationOf, pagesGlance, pointsEarned, provisionLines, shortLine, shortReason, SIGNUP_URL, stateWord, SUMMARY_PAGES, weightage,
} from "../summaryReport";
import { COOKIE_MIN, cookieRowsAt, MAX_FIT, MAX_LAYOUT_FIT, MIN_ON_PAGE_1, splitCheckpoints, SummaryReport } from "./SummaryReport";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
/** The checkpoints this report covers: everything except what rests only on the organisation's own records, which a website scan cannot check. */
const covered = <T extends { methodKey: string }>(rows: T[]) => rows.filter((r) => r.methodKey !== "organisation_records");
const SCAN = { id: "9bd5ccef-6909-4903-8d44-2b52a5668a06" as string, url: "https://x.test/", evidence_counts: { pages: 3, cookies: 2, trackers: 5, third_party_services: 1, policies: 1, forms: 1 } } as never;
const META = { consent_mechanism: "cmp", cmp_vendor: "Some CMP", accept_interaction: "clicked", reject_interaction: "clicked", pages_scanned: 3, pages_discovered: 40, page_limit: 5, stop_reason: "page_limit" } as unknown as WebsiteScanMetadata;
const META_UNTESTED = { ...META, accept_interaction: "not_found", reject_interaction: "not_found" } as unknown as WebsiteScanMetadata;
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

describe("the figures come from the score's own arithmetic", () => {
  it("reads weightage from the sentence the score gives, and writes it without trailing zeros", () => {
    expect(pointsEarned("4 of 5 points")).toBe(4);
    expect(pointsEarned("2.5 of 4 points")).toBe(2.5);
    expect(pointsEarned("not scored")).toBeNull();
    expect(weightage(25)).toBe("25");
    expect(weightage(4.5)).toBe("4.5");
  });

  for (const key of ["mixed", "clean_in_force", "confirmed", "thin"] as const) {
    it(`${key}: Total Weightage is the score's numerator and denominator, no awarded figure exceeds its maximum, and unassessed checks are never counted`, () => {
      const v = buildScoreView(F[key]);
      const s = buildSummaryReport(model({ score: key }));
      expect(s.assessedRows).toHaveLength(v.assessed.length);
      expect(s.unassessedRows).toHaveLength(covered(v.unableToVerify).length);
      expect(s.notApplicableRows).toHaveLength(v.excluded.length);
      for (const r of s.assessedRows) expect(r.awarded!).toBeLessThanOrEqual(r.max!);
      expect(s.totalMax).toBeCloseTo(v.assessed.reduce((n, r) => n + r.weight, 0));
      if (v.scoreValue !== null && s.totalMax > 0) expect((s.totalAwarded / s.totalMax) * 100).toBeCloseTo(v.scoreValue, 0);
      for (const r of s.unassessedRows) { expect(r.awarded).toBeNull(); expect(r.max).toBeNull(); expect(r.status.label).toBe("Unable to Verify"); }
    });
  }

  it("uses only the report's status words", () => {
    const allowed = new Set(["Passed", "Requires Review", "Unable to Verify", "Not Tested", "Not applicable"]);
    for (const key of ["mixed", "confirmed", "clean_in_force", "thin", "review"] as const) {
      const s = buildSummaryReport(model({ score: key }));
      for (const r of [...s.assessedRows, ...s.unassessedRows, ...s.notApplicableRows]) expect(allowed.has(r.status.label)).toBe(true);
    }
  });

  it("shortens a long line at a word, marked, and leaves a short one whole", () => {
    expect(shortLine("A privacy policy page was found.")).toBe("A privacy policy page was found.");
    const s = shortLine(`start ${"word ".repeat(80)}`);
    expect(s.length).toBeLessThanOrEqual(111);
    expect(s.endsWith("…")).toBe(true);
  });

  it("writes the Act and the Rules of a check as two short lines, naming each section once and dropping nothing", () => {
    expect(provisionLines("DPDP Act, 2023 s.6(1); DPDP Act, 2023 s.6(4); DPDP Act, 2023 s.6(1); DPDP Rules, 2025 r.3(c)(i)")).toEqual([{ label: "Act", text: "Section 6(1), 6(4)" }, { label: "Rules", text: "Rule 3(c)(i)" }]);
    expect(provisionLines("Act, Section 8(5); General duty")).toEqual([{ label: "Act", text: "Section 8(5)" }, { label: "", text: "General duty" }]);
    expect(provisionLines("")).toEqual([]);
  });

  it("writes the pages-scanned figure compactly, and only that figure", () => {
    expect(pagesGlance("5 (of 112 found)")).toBe("5 of 112");
    expect(pagesGlance("Not recorded")).toBe("Not recorded");
  });
});

describe("the two printed pages", () => {
  it("draws exactly two sheets, numbered 1 and 2 of 2", () => {
    const { container } = render(<SummaryReport model={model()} />);
    expect(SUMMARY_PAGES).toBe(2);
    expect(container.querySelectorAll("section.pg")).toHaveLength(2);
    for (const n of [1, 2]) expect(within(screen.getByTestId(`sm-page-${n}`)).getByText(`Page ${n} of 2`)).toBeTruthy();
  });

  it("carries the logo in the header of both pages and nowhere else, and the only other picture is the homepage preview", () => {
    const { container } = render(<SummaryReport model={model()} />);
    for (const n of [1, 2]) {
      const head = within(screen.getByTestId(`sm-page-${n}`)).getByTestId(`sm-head-${n}`);
      const logo = head.querySelector("img")!;
      expect(logo).toHaveAttribute("alt", "Consiva.ai");
      expect(logo.getAttribute("src")).toMatch(/^data:image\/png;base64,/);
      expect(head.querySelectorAll("img")).toHaveLength(1);
    }
    expect([...container.querySelectorAll("img")].map((i) => i.getAttribute("data-testid"))).toEqual(["sm-logo", "pg-hero-screenshot", "sm-logo"]);
    expect(container.querySelectorAll("footer")).toHaveLength(0);
  });

  it("keeps the domain, the scan time and the report id on both pages", () => {
    render(<SummaryReport model={model()} />);
    for (const n of [1, 2]) {
      const p = screen.getByTestId(`sm-page-${n}`);
      expect(p).toHaveTextContent("x.test");
      expect(p).toHaveTextContent(/Scanned .* UTC/);
      expect(p).toHaveTextContent(/Report ID: FR-\d{8}-9BD5/);
    }
  });

  it("uses Weightage Awarded, Maximum Weightage and Total Weightage, and none of the old words", () => {
    const { container } = render(<SummaryReport model={model()} />);
    const text = container.textContent ?? "";
    expect(text).toContain("Weightage Awarded");
    expect(text).toContain("Maximum Weightage");
    expect(text).toContain("Total Weightage");
    expect(text).not.toMatch(/Marks Obtained|Max Marks|Total marks|Total Marks/);
  });

  it("states the totals on page 1 from the same numbers as the table", () => {
    render(<SummaryReport model={model()} />);
    const s = buildSummaryReport(model());
    const p1 = screen.getByTestId("sm-page-1");
    expect(within(p1).getByTestId("sm-total-line")).toHaveTextContent(`Total Weightage: ${weightage(s.totalAwarded)} / ${weightage(s.totalMax)}`);
    const counts = within(p1).getByTestId("sm-counts");
    expect(counts).toHaveTextContent(`${s.assessedRows.length} of ${s.assessedRows.length + s.unassessedRows.length} applicable checkpoints assessed`);
    expect(within(p1).getByTestId("sm-checkpoints")).toHaveTextContent(`Total Weightage (assessed checkpoints)${weightage(s.totalAwarded)}${weightage(s.totalMax)}`);
    expect(within(p1).getByTestId("pg-hero-screenshot")).toHaveAttribute("src", expect.stringContaining("data:image/jpeg"));
  });

  it("says so in words when the homepage picture is not available", () => {
    render(<SummaryReport model={model({ shot: null })} />);
    expect(screen.getByTestId("pg-hero-missing")).toHaveTextContent("Homepage preview not available");
  });

  it("has no footer: nothing repeats at the foot of a page, and the page number is in the top line", () => {
    const { container } = render(<SummaryReport model={model()} />);
    expect(container.querySelectorAll("footer")).toHaveLength(0);
    for (const n of [1, 2]) expect(within(screen.getByTestId(`sm-page-${n}`)).getByText(`Page ${n} of 2`).closest("header")).not.toBeNull();
  });
});

describe("page 1 follows the agreed order", () => {
  it("shows the site and its first view, framework readiness directly below, then the one score beside the key metrics, then the applicable checkpoints", () => {
    render(<SummaryReport model={model()} />);
    const body = screen.getByTestId("sm-page-1").querySelector(".pg-body")!;
    const order = [...body.children].map((el) => el.getAttribute("data-testid") ?? el.className);
    const at = (t: string) => order.findIndex((o) => o.includes(t));
    expect(at("sm-hero")).toBe(0);
    expect(at("sm-framework")).toBe(at("sm-hero") + 1);               // directly below the screenshot
    expect(at("sm-framework")).toBeLessThan(at("sm-top"));             // before the score and the key metrics
    expect(at("sm-top")).toBeLessThan(at("sm-checkpoints"));           // then the checkpoints
    const top = within(body.children[at("sm-top")] as HTMLElement);
    expect(top.getByTestId("sm-score")).toBeTruthy();                  // the score and the metrics are one row
    expect(body.children[at("sm-top")].querySelector(".pg-tiles")).not.toBeNull();
    const fw = within(screen.getByTestId("sm-framework"));
    expect(fw.getByRole("img", { name: "India" })).toBeTruthy();
    expect(fw.getByTestId("pg-framework")).toHaveTextContent("India DPDP Act, 2023 and Rules, 2025");
    expect(within(body.children[0] as HTMLElement).getByTestId("pg-hero-screenshot")).toBeTruthy();
  });
});

describe("the header and the one score", () => {
  it("has a solid black header (the logo's own near-black) with white text on both pages, from the stylesheet that prints them", () => {
    const { container } = render(<SummaryReport model={model()} />);
    for (const n of [1, 2]) {
      const head = within(screen.getByTestId(`sm-page-${n}`)).getByTestId(`sm-head-${n}`);
      expect(head.tagName).toBe("HEADER");
      const css = getComputedStyle(head);
      expect(css.backgroundColor).toBe("rgb(3, 7, 15)");       // the logo's own near-black, so the logo sits in the band without an edge
      expect(css.color).toBe("rgb(255, 255, 255)");
      expect(getComputedStyle(head.querySelector(".sm-head-r")!).color).toBe("rgb(255, 255, 255)");
      expect(head).toHaveTextContent("x.test");
      expect(head).toHaveTextContent(/Scanned .* UTC/);
      expect(head).toHaveTextContent(/Report ID: FR-\d{8}-9BD5/);
    }
    expect(container.querySelectorAll("header")).toHaveLength(2);
  });

  it("shows the Compliance Score once, as the score the assessment returned, and no coverage score, area scores or framework percentage", () => {
    const { container } = render(<SummaryReport model={model()} />);
    const v = buildScoreView(F.mixed);
    const score = screen.getByTestId("sm-score");
    expect(score).toHaveTextContent("Compliance Score");
    expect(within(score).getByRole("img", { name: new RegExp(`Privacy Readiness Score ${v.headline.replace(".", "\\.")} out of 100`) })).toBeTruthy();
    expect(container.querySelectorAll("svg.pg-ring")).toHaveLength(1);                 // one ring: the one score
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/Coverage/);
    expect(container.querySelector("[data-testid=pg-framework-score]")).toBeNull();    // the framework block carries no percentage
    expect(container.querySelector(".pg-areas, .pg-area")).toBeNull();                 // no privacy-area score cards
    expect(text).not.toMatch(/Privacy areas/i);
  });
});

describe("the applicable checkpoints", () => {
  it("lists every applicable checkpoint once, with its status and the evidence it rests on in full", () => {
    render(<SummaryReport model={model()} />);
    const v = buildScoreView(F.mixed);
    const rows = [...screen.getByTestId("summary-report").querySelectorAll("tr[data-testid=sm-checkpoint]")];
    expect(rows).toHaveLength(covered(v.assessed).length + covered(v.unableToVerify).length);
    const byTitle = new Map([...covered(v.assessed), ...covered(v.unableToVerify)].map((r) => [r.title, r]));
    expect(new Set(rows.map((tr) => tr.querySelector(".sm-cp > b")!.textContent)).size).toBe(rows.length);
    for (const tr of rows) {
      const req = byTitle.get(tr.querySelector(".sm-cp > b")!.textContent!)!;
      const want = (req.statusKey === "unable_to_verify" ? (req.reason || req.evidenceLine) : req.evidenceLine).replace(/\s+/g, " ").trim();
      const shown = tr.querySelector("[data-testid=sm-evidence]")?.textContent ?? "";
      expect(shown.length).toBeGreaterThan(0);
      expect(shown).not.toMatch(/…$/);                                 // never cut short
      expect(shown.startsWith("Same evidence as checkpoint") || shown.includes(want.replace(/^[^:]*says: /, "").slice(0, 40)) || want.includes(shown.slice(0, 40))).toBe(true);
    }
  });

  it("says once, and refers back, when the same evidence supports a second checkpoint, instead of cutting it", () => {
    const rows = buildSummaryReport(model()).assessedRows;
    const twice = rows.filter((r, i) => r.note.length > 80 && rows.findIndex((x) => x.note === r.note) !== i);
    render(<SummaryReport model={model()} />);
    const shown = [...screen.getByTestId("summary-report").querySelectorAll("[data-testid=sm-evidence]")].map((e) => e.textContent!);
    expect(shown.filter((t) => t.startsWith("Same evidence as checkpoint")).length).toBe(twice.length);
  });

  it("uses Passed, Requires Review, Unable to Verify and Not Tested, and never Passed for a checkpoint that was not assessed", () => {
    for (const key of ["mixed", "confirmed", "clean_in_force", "thin", "review", "insufficient"] as const) {
      const s = buildSummaryReport(model({ score: key }));
      const v = buildScoreView(F[key]);
      for (const r of s.unassessedRows) { expect(r.status.label).not.toBe("Passed"); expect(["Unable to Verify", "Not Tested"]).toContain(r.status.label); expect(r.awarded).toBeNull(); }
      for (const r of s.assessedRows) expect(["Passed", "Requires Review"]).toContain(r.status.label);
      // a check the website scan could have tested but did not is Not Tested; one that rests on the policy's wording or the organisation's records is Unable to Verify
      for (const q of covered(v.unableToVerify)) {
        const row = s.unassessedRows.find((r) => r.title === q.title)!;
        expect(row.status.label).toBe(q.methodKey === "website_scan" ? "Not Tested" : "Unable to Verify");
      }
    }
  });

  it("marks the status in the table with the words, one row per checkpoint, and unassessed rows carry no weightage", () => {
    render(<SummaryReport model={model()} />);
    for (const tr of screen.getByTestId("sm-checkpoints").querySelectorAll("tr[data-testid=sm-checkpoint]")) {
      const status = tr.getAttribute("data-status")!;
      expect(tr.querySelectorAll("td")[2]).toHaveTextContent(status);
      if (status !== "Passed" && status !== "Requires Review") expect(tr.querySelectorAll("td")[3].textContent).toBe("–");
    }
  });
});

describe("the disclaimer", () => {
  it("is the agreed text, whole, in a card of its own, and the last thing on page 2", () => {
    render(<SummaryReport model={model()} />);
    const p2 = screen.getByTestId("sm-page-2");
    const d = within(p2).getByTestId("sm-disclaimer");
    expect(d.textContent).toBe("This report reflects publicly accessible website content and technical behaviour observed during the scan. Results are limited to the pages and consent states tested and do not constitute a legal opinion, certification, or guarantee of regulatory compliance.");
    const card = d.closest(".sm-disc")!;
    expect(card).toHaveTextContent(/^Disclaimer/);
    expect(p2.querySelector(".pg-body")!.lastElementChild).toBe(card);
    expect(screen.getByTestId("sm-page-1").querySelector("[data-testid=sm-disclaimer]")).toBeNull();
    expect(getComputedStyle(card).marginTop).toBe("auto");                                // it sits at the foot, the free space above it
  });
});

describe("page 2 shows only what the website and the scan support", () => {
  it("compares the three consent states in the agreed table", () => {
    render(<SummaryReport model={model()} />);
    const t = screen.getByTestId("sm-consent");
    expect([...t.querySelectorAll("thead th")].map((th) => th.textContent)).toEqual(["Metric", "Before Consent", "After Accept", "After Reject"]);
    expect([...t.querySelectorAll("tbody tr")].map((tr) => tr.querySelector("td")!.textContent)).toEqual(["Cookies observed", "Analytics/marketing requests", "Total third-party requests"]);
  });

  it("does not list a checkpoint that rests only on the organisation's own records: the agent cannot check it, so it is not asked of the website", () => {
    render(<SummaryReport model={model()} />);
    const v = buildScoreView(F.mixed);
    const internal = v.unableToVerify.filter((x) => x.methodKey === "organisation_records");
    expect(internal.length).toBeGreaterThan(0);                                          // the fixture has some, so this test means something
    const all = screen.getByTestId("summary-report");
    const listed = [...all.querySelectorAll("tr[data-testid=sm-checkpoint]")].map((t) => t.querySelector(".sm-cp > b")!.textContent);
    for (const r of internal) expect(listed).not.toContain(r.title);
    for (const r of internal) expect(all.textContent).not.toContain(r.title);
    expect(all.textContent).not.toMatch(/unable to access|could not access|access denied/i);
  });

  it("counts the framework legend over the same checkpoints the table lists, and changes neither the score nor its Total Weightage", () => {
    for (const key of ["mixed", "clean_in_force", "thin", "confirmed"] as const) {
      const s = buildSummaryReport(model({ score: key }));
      const v = buildScoreView(F[key]);
      const c = s.framework.counts;
      expect(c.met + c.partly + c.notMet).toBe(s.assessedRows.length);
      expect(c.unverified).toBe(s.unassessedRows.length);
      expect(s.framework.segments.reduce((n, x) => n + x.count, 0)).toBe(s.assessedRows.length + s.unassessedRows.length + c.notApplicable);
      expect(s.totalMax).toBeCloseTo(v.assessed.reduce((n, r) => n + r.weight, 0));   // the weightage is the score's own, untouched by what is listed
    }
  });

  it("shows a public check the scan could not verify as Unable to Verify, never as Passed or as a failure", () => {
    const s = buildSummaryReport(model());
    for (const f of s.findings) expect(["Requires Review", "Unable to Verify"]).toContain(f.status.label);
    for (const r of s.unassessedRows) expect(r.status.label).toBe("Unable to Verify");
  });

  it("shows the Grievance Officer and the Data Protection Officer only when the website's public text gives them, and nothing when it does not", () => {
    const none = render(<SummaryReport model={model()} />);
    expect(screen.queryByTestId("sm-contacts")).toBeNull();          // the fixture has no policy text: nothing is invented
    none.unmount();
    const grievance: InsightSection = { key: "grievance", title: "Grievance officer", status: "Grievance contact found", tone: "good", knowledge: "Confirmed", summary: "s",
      facts: [{ label: "Grievance contact", value: "g@x.test", tone: "good" }, { label: "Response period", value: "Within about 30 days", tone: "good" }, { label: "Complaint to the Data Protection Board", value: "Not found in the policy text", tone: "warn" }],
      quote: { text: "q", source: "https://x.test/privacy" }, legal: "", notes: [] };
    const dpo: InsightSection = { ...grievance, key: "dpo", title: "Data Protection Officer", status: "No Data Protection Officer or answering person found in the policy text", tone: "warn", knowledge: "Not verified",
      facts: [{ label: "Business contact", value: "Not found in the policy text", tone: "warn" }], quote: null };
    render(<SummaryReport model={{ ...model(), insights: [grievance, dpo] }} />);
    const c = screen.getByTestId("sm-contacts");
    expect(c).toHaveTextContent("Grievance Officer");
    expect(c).toHaveTextContent("Grievance contact: g@x.test");
    expect(c).toHaveTextContent("Response period: Within about 30 days");
    expect(c).not.toHaveTextContent("Not found in the policy text");     // an absence is not shown as a detail
    expect(c).not.toHaveTextContent("Data Protection Officer");           // none found: the section is left out
    expect(screen.getAllByTestId("sm-contact")).toHaveLength(1);
  });

  it("ends with a sign-up link to the exact address, as a real link", () => {
    render(<SummaryReport model={model()} />);
    const p2 = screen.getByTestId("sm-page-2");
    const link = within(p2).getByTestId("sm-signup") as HTMLAnchorElement;
    expect(link.tagName).toBe("A");
    expect(link.getAttribute("href")).toBe("https://app.consiva.ai/register");
    expect(SIGNUP_URL).toBe("https://app.consiva.ai/register");
    const cta = within(p2).getByTestId("sm-cta");
    expect(cta).toHaveTextContent("Want to run your own Privacy Readiness Scan?");
    expect(cta).toHaveTextContent(/DPDP Act in minutes/);
    expect(link).toHaveTextContent(/^Sign Up$/);
    expect(getComputedStyle(cta).backgroundImage).toMatch(/linear-gradient/);                // the banner is a coloured block, not a plain line
    // near the bottom of page 2: the block before the disclaimer
    expect(within(p2).getByTestId("sm-cta").nextElementSibling).toBe(within(p2).getByTestId("sm-disclaimer").closest(".sm-disc"));
    // no other link in the report points anywhere else
    expect([...screen.getByTestId("summary-report").querySelectorAll("a")].map((a) => a.getAttribute("href"))).toEqual(["https://app.consiva.ai/register"]);
  });
});

describe("fitting a page to its room", () => {
  it("keeps every observed cookie, up to the most a page lists, through all the layout steps, and cuts the list only after them, in steps, never below the minimum", () => {
    for (let step = 0; step <= MAX_LAYOUT_FIT; step++) expect(cookieRowsAt(step, 9)).toBe(9);
    expect(cookieRowsAt(0, 17)).toBe(COOKIE_ROWS);                       // the rest are counted and left to the long report
    expect(cookieRowsAt(MAX_LAYOUT_FIT + 1, 17)).toBe(COOKIE_ROWS - 3);
    expect(cookieRowsAt(MAX_LAYOUT_FIT + 2, 17)).toBe(COOKIE_ROWS - 6);
    expect(cookieRowsAt(MAX_FIT, 17)).toBe(COOKIE_MIN);
    expect(cookieRowsAt(0, 3)).toBe(3);
    expect(cookieRowsAt(MAX_FIT, 3)).toBe(3);
    expect(cookieRowsAt(0, 50)).toBe(COOKIE_ROWS);
  });
});

describe("the cookie evidence", () => {
  it("lists every observed cookie with its name, domain, classification and what it did in each consent state", () => {
    render(<SummaryReport model={model()} />);
    const t = within(screen.getByTestId("sm-cookies"));
    const rows = [...screen.getByTestId("sm-cookies").querySelectorAll("tbody tr")].map((tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent));
    const byName = Object.fromEntries(rows.map((r) => [r[0], r]));
    expect(t.getByText("_ga")).toBeTruthy();
    expect(byName._ga).toEqual(["_ga", "x.test", "Analytics · Google Analytics", "Not seen", "Set", "Not seen"]);
    expect(byName._fbp).toEqual(["_fbp", ".facebook.com", "Marketing · Meta", "Set", "Set", "Not seen"]);
    expect(byName.mystery).toEqual(["mystery", "x.test", "Not classified", "Set", "Not seen", "Not seen"]);
  });

  it("shows only as many cookies as the page has room for, and counts the rest", () => {
    render(<SummaryReport model={model()} />);
    expect(screen.queryByTestId("sm-cookies-more")).toBeNull();
    const s = buildSummaryReport(model());
    expect(s.cookieTotal).toBe(3);
    expect(s.cookieLines).toHaveLength(3);
  });

  it("counts the cookies from the same rows, and never prints a purpose", () => {
    const { container } = render(<SummaryReport model={model()} />);
    const s = buildSummaryReport(model());
    expect(s.cookieCounts).toMatchObject({ found: 3, beforeConsent: 2, afterAccept: 2, afterReject: 0, thirdParty: 3 });
    const strip = screen.getByTestId("sm-cookie-counts");
    expect(strip).toHaveTextContent("3 found");
    expect(strip).toHaveTextContent("2 before consent");
    expect(container.textContent).not.toContain("SHOULD NEVER BE PRINTED");
  });

  it("says Not Tested, not zero, for a consent state in which no choice was made", () => {
    render(<SummaryReport model={model({ meta: META_UNTESTED })} />);
    const strip = screen.getByTestId("sm-cookie-counts");
    expect(strip).toHaveTextContent("Not Tested after Accept");
    expect(strip).toHaveTextContent("Not Tested after Reject");
    const rows = [...screen.getByTestId("sm-cookies").querySelectorAll("tbody tr")].map((tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent));
    // _ga was held by the Accept pass, in which nothing was clicked: seen, but not as the result of a choice. Nothing was held by the Reject pass.
    for (const r of rows) {
      expect(r[4]).toBe(r[0] === "_ga" || r[0] === "_fbp" ? "Seen, no choice made" : "Not Tested");
      expect(r[5]).toBe("Not Tested");
    }
    const consent = screen.getByTestId("sm-consent");
    for (const tr of consent.querySelectorAll("tbody tr")) { const c = [...tr.querySelectorAll("td")].map((td) => td.textContent); expect(c[2]).toBe("Not Tested"); expect(c[3]).toBe("Not Tested"); }
    expect(screen.getByTestId("sm-untested")).toHaveTextContent("Accept and Reject were not tested");
    expect(screen.getByTestId("sm-untested")).toHaveTextContent("Seen, no choice made");
  });

  it("names the classifier's result and provider, and calls an unclassified cookie Not classified", () => {
    expect(classificationOf({ category: "analytics", vendor: "Google Analytics", provider: "Google Analytics" })).toBe("Analytics · Google Analytics");
    expect(classificationOf({ category: "Unclassified", vendor: "Not available", provider: "Not identified" })).toBe("Not classified");
    expect(classificationOf({ category: "functional", vendor: "Not available", provider: "Not identified" })).toBe("Functional");
    expect(stateWord("Not tested")).toBe("Not Tested");
    expect(stateWord("Set")).toBe("Set");
  });
});

describe("the pages the scan chose", () => {
  it("lists each page with the reason the scan recorded for it, and what happened to a page that did not load", () => {
    render(<SummaryReport model={model()} />);
    const t = screen.getByTestId("sm-pages");
    expect(t).toHaveTextContent("x.test/privacy");
    expect(t).toHaveTextContent("Privacy or cookie page · sitemap");
    expect(t).toHaveTextContent("Terms page · link");
    expect(t).toHaveTextContent("Entered address (always first)");
    expect(t).toHaveTextContent("x.test/old");
    expect(t).toHaveTextContent(/Not scanned: failed to load/i);
  });

  it("says how many pages were scanned of those found, against the limit, and states the selection rule without claiming the whole site was scanned", () => {
    render(<SummaryReport model={model()} />);
    const p2 = screen.getByTestId("sm-page-2");
    expect(p2).toHaveTextContent(/3 scanned of 40 found, limit 5/);
    expect(within(p2).getByTestId("sm-rule")).toHaveTextContent(/privacy and cookie pages, terms, contact and legal notices/);
    expect(within(p2).getByTestId("sm-rule")).toHaveTextContent(/no address is guessed/);
    expect(screen.getByTestId("pg-limits")).toHaveTextContent(/not an assessment of the whole site/);
  });
});

describe("what the report will not claim", () => {
  it("does not call a website compliant, certified or guaranteed, and the disclaimer says the results are not a legal opinion", () => {
    const { container } = render(<SummaryReport model={model()} />);
    expect(container.textContent).not.toMatch(/fully compliant|is compliant|certified compliant|guarantees? compliance/i);
    expect(screen.getByTestId("sm-disclaimer")).toHaveTextContent(/do not constitute a legal opinion, certification, or guarantee/);
    expect(screen.getByTestId("pg-limits")).toHaveTextContent(/not an assessment of the whole site/);
  });
});

describe("where the checkpoint table is cut", () => {
  const mk = (n: number, scored: boolean) => ({ n, id: `C${n}`, title: `T${n}`, category: "", provisions: "", status: { label: scored ? "Passed" : "Unable to Verify", tone: "good" }, scored, awarded: scored ? 1 : null, max: scored ? 1 : null, note: "" }) as never;
  const assessed = [1, 2, 3, 4, 5, 6].map((n) => mk(n, true));
  const unassessed = [7, 8, 9].map((n) => mk(n, false));
  it("keeps every checkpoint on page 1 until it is full, then moves them from the end, one page each, never fewer than the minimum on page 1", () => {
    for (let moved = 0; moved <= 12; moved++) {
      const { first, second } = splitCheckpoints(assessed, unassessed, moved);
      expect([...first, ...second].map((r) => r.n)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);     // each on exactly one page, in order
      expect(first.length).toBe(Math.max(MIN_ON_PAGE_1, 9 - moved));
      expect(second.length).toBe(9 - first.length);
    }
    expect(splitCheckpoints(assessed, unassessed, 0).second).toHaveLength(0);
    expect(splitCheckpoints(assessed, unassessed, 2).second.map((r) => r.id)).toEqual(["C8", "C9"]);   // the unassessed ones go first
  });
});

describe("the reason a page was chosen, as a short label", () => {
  it("keeps the kind of page and where it was found, and says the entered address is always first", () => {
    expect(shortReason("Privacy or cookie page: ranked first, because it settles the notice and cookie checks. Selected from pages found in the sitemap.")).toBe("Privacy or cookie page · sitemap");
    expect(shortReason("Terms page: ranked after the privacy and cookie pages. Selected from pages found through a link on a scanned page.")).toBe("Terms page · link");
    expect(shortReason("The address that was entered: always scanned first.")).toBe("Entered address (always first)");
    expect(shortReason("A page whose address mentions privacy, cookies, terms or contact but is not named like a policy page: ranked after the site's own policy pages. Selected from pages found in the sitemap.")).toContain("· sitemap");
  });
});
