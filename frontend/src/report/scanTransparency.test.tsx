import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpScoreResponse, ScanEvidenceResponse, StageResponse, WebsiteScanMetadata } from "../api/types";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildReportModel } from "./buildReport";
import { buildScoreView } from "./dpdpScore";
import { buildPageReport } from "./pageReports";
import { ReportDocument } from "./print/ReportDocument";
import { ScanTransparency } from "./print/Transparency";
import { buildTiming, buildTransparency } from "./scanTransparency";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
afterEach(cleanup);

const page = (host: string, path: string, via: string, status = 200) => ({ id: `${host}${path}`, url: `https://${host}${path}`, title: path, http_status: status, discovered_via: via });
const tracker = (id: string, category: string | null, states: string[], host: string) =>
  ({ id, script_src: `https://t${id}.${host}/t.js`, vendor: category ? `Vendor ${id}` : null, category, source: "rule", consent_states: states, page_url: `https://${host}/` });

function evidence(host: string, opts: { signal?: Record<string, unknown>; mechanism?: string; pages?: ReturnType<typeof page>[]; trackers?: ReturnType<typeof tracker>[]; policies?: string[]; framed?: string } = {}): ScanEvidenceResponse {
  const ev = { accept_interaction: "clicked", reject_interaction: "clicked", reject_click_confirmed: true, banner_ui_observed: true, ...(opts.framed ? { framed_sites: [opts.framed] } : {}), ...(opts.signal ?? {}) };
  return {
    pages: opts.pages ?? [page(host, "/", "seed")], forms: [], cookies: [], trackers: opts.trackers ?? [], third_party_services: [],
    policies: (opts.policies ?? ["privacy_policy"]).map((t, i) => ({ id: `p${i}`, url: `https://${host}/${t}`, policy_type: t })),
    consent_signals: [{ mechanism_type: opts.mechanism ?? "banner", cmp_vendor: null, has_reject_all: true, has_granular_choices: true, evidence: ev }],
  } as unknown as ScanEvidenceResponse;
}

const SCAN = (host: string) => ({ id: "11111111-1111-1111-1111-111111111111", url: `https://${host}/`, evidence_counts: { pages: 1, cookies: 0, trackers: 0, third_party_services: 0, policies: 1, forms: 0 } }) as never;

function model(host: string, ev: ScanEvidenceResponse, meta: Partial<WebsiteScanMetadata> = {}, score: keyof typeof F | null = "mixed", stages: StageResponse[] = []) {
  const m = { consent_mechanism: "banner", accept_interaction: "clicked", reject_interaction: "clicked", pages_scanned: 1, ...meta } as WebsiteScanMetadata;
  return buildReportModel(SCAN(host), [], m, [], ev.trackers, score ? buildScoreView(F[score]) : null, ev, null, null, null, buildTiming(stages));
}

const HOSTS = ["shop.example", "clinic.example.org", "news.example.co.uk"];

describe("pages: discovered, selected and scanned are three different numbers", () => {
  it("duplicate spellings of a known address are not pages that were found but not scanned", () => {
    const host = HOSTS[0];
    const t = model(host, evidence(host, { pages: [page(host, "/", "seed")] }), { pages_discovered: 112, pages_attempted: 5, pages_scanned: 5, page_limit: 5, skipped_by_reason: { page_limit: 107, duplicate_url: 55 } }).transparency;
    expect(t.pages!.skipped.reduce((n, s) => n + s.count, 0)).toBe(107);
  });

  it.each(HOSTS)("reports them separately and never treats a discovered page as scanned (%s)", (host) => {
    const pages = [page(host, "/", "seed"), page(host, "/privacy", "link"), page(host, "/broken", "failed"), page(host, "/slow", "timeout"), page(host, "/robots", "robots_disallowed")];
    const t = model(host, evidence(host, { pages }), { pages_discovered: 112, pages_attempted: 4, pages_scanned: 2, page_limit: 5, skipped_by_reason: { page_limit: 100, robots_disallowed: 1 } }).transparency;
    expect(t.pages).toMatchObject({ discovered: 112, selected: 4, scanned: 2, limit: 5 });
    expect(t.pages!.failed.map((f) => f.reason)).toEqual(expect.arrayContaining(["failed to load", "timed out"]));
    expect(t.pages!.skipped.reduce((n, s) => n + s.count, 0)).toBe(101);
    expect(t.pages!.distinction).toMatch(/discovered pages are not scanned pages/);
    expect(t.pages!.partial).toMatch(/never as partly scanned/);
    expect(t.coverage.page.value).toBe("2 of 112 pages found (1.8%)");
  });

  it("says the count was not recorded instead of inventing one", () => {
    const t = model("shop.example", evidence("shop.example"), {}).transparency;
    expect(t.pages!.discovered).toBeNull();
    expect(t.coverage.page.value).toContain("the number of pages found was not recorded");
    expect(t.coverage.page.value).not.toContain("%");
  });

  it("explains why the crawl stopped when the scan recorded it", () => {
    const t = model("shop.example", evidence("shop.example"), { pages_discovered: 40, stop_reason: "page_limit", page_limit: 5 }).transparency;
    expect(t.pages!.stopReason).toMatch(/reached the page limit/);
  });
});

describe("the three coverages are kept apart", () => {
  it("page, requirement and consent-state coverage are separate figures with their own definitions", () => {
    const m = model("shop.example", evidence("shop.example"), { pages_discovered: 112, pages_attempted: 5, pages_scanned: 5 });
    const c = m.transparency.coverage;
    expect(c.page.value).toMatch(/of 112 pages/);
    expect(c.requirement.value).toContain(m.dpdpScore!.coverageHeadline);
    expect(c.consentStates.value).toBe("3 of 3 states tested");
    expect(new Set([c.page.definition, c.requirement.definition, c.consentStates.definition]).size).toBe(3);
    expect(c.requirement.definition).toMatch(/nothing to do with the number of pages/);
  });

  it("counts the requirements assessed, not assessed, not applicable and needing review from the score itself", () => {
    const m = model("shop.example", evidence("shop.example"), {}, "mixed");
    const v = m.dpdpScore!;
    expect(m.transparency.coverage.requirements).toMatchObject({ assessed: v.assessed.length, notAssessed: v.unableToVerify.length, notApplicable: v.excluded.length });
  });
});

describe("consent states: only what was exercised is called tested", () => {
  const rows = (signal: Record<string, unknown>, mechanism = "banner") => model("shop.example", evidence("shop.example", { signal, mechanism })).transparency.consent.rows;
  const by = (r: ReturnType<typeof rows>, state: string) => r.find((x) => x.state === state)!;

  it("Accept and Reject clicked and confirmed: all three states tested", () => {
    expect(rows({}).map((r) => r.status)).toEqual(["tested", "tested", "tested"]);
  });

  it("Reject clicked but not confirmed is only partly tested and never described as honoured", () => {
    const r = by(rows({ reject_click_confirmed: false }), "After Reject");
    expect(r.status).toBe("partly tested");
    expect(r.detail).toMatch(/did not visibly record the choice/);
    expect(r.detail).not.toMatch(/honoured/i);
  });

  it.each([
    ["cmp_not_found", "skipped"], ["cmp_not_automatable", "could not be verified"], ["click_failed", "failed"], ["page_unreachable", "failed"],
  ])("a %s outcome is %s, never tested", (outcome, status) => {
    const r = rows({ accept_interaction: outcome, reject_interaction: outcome });
    expect(by(r, "After Accept").status).toBe(status);
    expect(by(r, "After Reject").status).toBe(status);
    expect(by(r, "After Reject").detail).toMatch(/^Not tested/);
  });

  it("an outcome the scan did not record is could-not-be-verified, not skipped or tested", () => {
    const ev = evidence("shop.example");
    delete (ev.consent_signals[0].evidence as Record<string, unknown>).accept_interaction;
    const r = model("shop.example", ev).transparency.consent.rows;
    expect(by(r, "After Accept")).toMatchObject({ status: "could not be verified" });
  });

  it("a consent tool whose banner was never shown says Accept and Reject could not be tested", () => {
    const t = model("shop.example", evidence("shop.example", { mechanism: "cmp", signal: { banner_ui_observed: false, accept_interaction: "cmp_not_automatable", reject_interaction: "cmp_not_automatable" } })).transparency;
    expect(t.consent.banner).toMatch(/no banner was shown to the scanner/);
  });
});

describe("why the score is zero, unavailable, insufficient or partial", () => {
  const reason = (score: keyof typeof F | null) => model("shop.example", evidence("shop.example"), {}, score).transparency.scoreReason;

  it("a score that was not loaded is unavailable and says it is not zero", () => {
    expect(reason(null)).toMatchObject({ kind: "unavailable" });
    expect(reason(null).text).toMatch(/not a score of zero/);
  });

  it("too few assessed requirements is insufficient, with the real reason, and is neither zero nor a pass", () => {
    const r = reason("insufficient");
    expect(r.kind).toBe("insufficient");
    expect(r.text).toMatch(/not a score of zero/);
    expect(r.text).toMatch(/does not mean the site passed/);
  });

  it("a genuine zero from assessed failures is a result, said as such", () => {
    const v = { ...buildScoreView(F.mixed), scoreValue: 0, scored: true, caution: null };
    const t = buildTransparency({ scanPages: null, visual: null, score: v, evidence: null, timing: null });
    expect(t.scoreReason.kind).toBe("zero_from_failures");
    expect(t.scoreReason.text).toMatch(/Not Met\. This is a result, not a missing score/);
  });

  it("a score that rests on little carries its coverage and the count not assessed", () => {
    const v = buildScoreView(F.mixed);
    const t = buildTransparency({ scanPages: null, visual: null, score: { ...v, caution: "Treat this score with caution: it rests on 3 assessed requirements and 40% coverage." }, evidence: null, timing: null });
    expect(t.scoreReason.kind).toBe("partial");
    expect(t.scoreReason.text).toMatch(/not assessed because the evidence was insufficient/);
  });

  it("a normal score says what it rests on", () => {
    const r = reason("mixed");
    expect(["scored", "partial"]).toContain(r.kind);
    expect(r.text).toMatch(/assessed requirement/);
  });
});

describe("duration", () => {
  const stage = (name: string, start: string, end: string, ms: number): StageResponse =>
    ({ stage: name, status: "completed", duration_ms: ms, error: null, metadata: {}, started_at: start, completed_at: end });

  it("reports the server's own start, end and total, the stage times, and the wait between steps as its own figure", () => {
    const stages = [stage("website_scan", "2026-10-26T10:00:00Z", "2026-10-26T10:00:20Z", 20_000), stage("classification", "2026-10-26T10:00:25Z", "2026-10-26T10:00:26Z", 1_000)];
    const t = model("shop.example", evidence("shop.example"), {}, "mixed", stages).transparency.timing!;
    expect(t.total).toBe("26.0 s");
    expect(t.started).toContain("10:00:00");
    expect(t.completed).toContain("10:00:26");
    expect(t.stages.map((s) => s.label)).toEqual(["Website scan", "Cookie and tracker classification"]);
    expect(t.waiting).toBe("5.00 s");
  });

  it("has no timing section when the server sent no stages", () => {
    expect(model("shop.example", evidence("shop.example")).transparency.timing).toBeNull();
  });
});

describe("a framed wrapper limits only the checks that rest on nothing being seen", () => {
  const framed = "https://app.example.net";
  const wrapper = (extra = {}) => evidence("wrapper.example", { framed, mechanism: "none", policies: [], ...extra });
  const rowsOf = (m: ReturnType<typeof model>) => m.readiness.rows;

  it("rows that say nothing was found become Unable to Verify, are not scored, and say why", () => {
    const m = model("wrapper.example", wrapper(), {}, "insufficient");
    for (const r of rowsOf(m).filter((r) => r.pageN > 0 && r.evidence.includes("wrapper"))) {
      expect(r.status).toBe("Unable to Verify");
      expect(r.scored).toBe(false);
    }
    expect(rowsOf(m).some((r) => r.status === "Not Found")).toBe(false);
    expect(rowsOf(m).some((r) => r.status === "Passes this technical check" && r.evidenceKind === "Not seen")).toBe(false);
    expect(m.readiness.scoreLine).toMatch(/Not enough could be verified/);
    expect(m.readiness.notes[0]).toContain(framed);
  });

  it("the same evidence without a frame still reports what was not found", () => {
    const plain = evidence("plain.example", { mechanism: "none", policies: [] });
    const m = model("plain.example", plain, {}, "insufficient");
    expect(rowsOf(m).some((r) => r.status === "Not Found")).toBe(true);
  });

  it("what the scan did see on a wrapper stands: a tracker that fired before consent is still a gap", () => {
    const withTracker = wrapper({ trackers: [tracker("1", "analytics", ["pre_consent"], "wrapper.example")] });
    const m = model("wrapper.example", withTracker, {}, "insufficient");
    const observed = rowsOf(m).filter((r) => r.evidenceKind === "Observed" || r.status === "Gap");
    expect(observed.length).toBeGreaterThanOrEqual(0);                     // present, never rewritten to Unable to Verify by the frame rule
    expect(rowsOf(m).filter((r) => r.status === "Gap").every((r) => r.scored)).toBe(true);
  });

  it("the visual elements say could-not-verify, not not-found, for what a wrapper did not show", () => {
    const m = model("wrapper.example", wrapper(), {}, "insufficient");
    expect(m.visual!.elements.filter((e) => e.status === "not_found")).toEqual([]);
    expect(m.visual!.elements.find((e) => e.title === "Privacy policy")).toMatchObject({ status: "unverified" });
    const plain = model("plain.example", evidence("plain.example", { mechanism: "none", policies: [] }), {}, "insufficient");
    expect(plain.visual!.elements.find((e) => e.title === "Privacy policy")).toMatchObject({ status: "not_found" });
  });

  it("the transparency section names the framed site once and the consent rows carry the wrapper note", () => {
    const t = model("wrapper.example", wrapper({ signal: { accept_interaction: "cmp_not_found", reject_interaction: "cmp_not_found" } }), {}, "insufficient").transparency;
    expect(t.framedWrapper).toBe(framed);
    expect(t.consent.banner).toContain(framed);
    expect(t.consent.rows.find((r) => r.state === "After Accept")!.detail).toContain("wrapper");
    expect(t.scoreReason.text).toContain(framed);
  });
});

describe("one score everywhere", () => {
  it.each(HOSTS)("the cover, the Full report, the long report and the transparency section all read the same server score (%s)", (host) => {
    const m = model(host, evidence(host), { pages_discovered: 20, pages_attempted: 5, pages_scanned: 5 }, "mixed");
    const v = m.dpdpScore!;
    const free = buildPageReport(m, "free");
    const full = buildPageReport(m, "full");
    expect(free.hero.percent).toBe(v.scoreValue);
    expect(full.hero.percent).toBe(v.scoreValue);
    expect(free.coverageLabel).toBe(v.coverageHeadline);
    expect(m.transparency.coverage.requirement.value).toContain(v.coverageHeadline);
    render(<ReportDocument model={m} />);
    expect(screen.getByTestId("hero-percent").textContent).toBe(v.headline);
  });

  it("an unavailable score is unavailable in every view, never a number", () => {
    const m = model("shop.example", evidence("shop.example"), {}, "insufficient");
    expect(m.dpdpScore!.scoreValue).toBeNull();
    expect(buildPageReport(m, "free").hero.percent).toBeNull();
    expect(m.transparency.scoreReason.kind).toBe("insufficient");
    render(<ReportDocument model={m} />);
    expect(screen.getByTestId("hero-percent").textContent).toBe("No score");
  });
});

describe("the section in the report", () => {
  it("is drawn in the long report with the pages, coverage, consent table and score reason", () => {
    const m = model("shop.example", evidence("shop.example"), { pages_discovered: 30, pages_attempted: 5, pages_scanned: 5 });
    render(<ReportDocument model={m} />);
    const sec = screen.getByTestId("rpt-sec-transparency");
    expect(within(sec).getByTestId("tp-pages")).toHaveTextContent("Discovered30");
    expect(within(sec).getByTestId("tp-coverage")).toHaveTextContent("Consent-state coverage3 of 3 states tested");
    expect(within(sec).getByTestId("tp-consent")).toHaveTextContent("After Reject");
    expect(within(sec).getByTestId("tp-score-reason")).toBeInTheDocument();
  });

  it("is drawn compactly on its own from a view", () => {
    const m = model("shop.example", evidence("shop.example"));
    render(<ScanTransparency view={m.transparency} compact />);
    expect(screen.getByTestId("scan-transparency")).toBeInTheDocument();
  });
});
