import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DpdpScoreResponse, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import { DpdpScoreSection } from "../components/DpdpScoreSection";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildReportModel } from "./buildReport";
import { buildScoreView } from "./dpdpScore";
import { CheckCoverage } from "./print/CheckCoverage";
import { ReportDocument } from "./print/ReportDocument";
import { assessedCategories, isReportCategory, isReportMetric } from "./reportScope";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
afterEach(cleanup);

const META = { consent_mechanism: "cmp", pages_scanned: 3, pages_discovered: 10, page_limit: 5 } as unknown as WebsiteScanMetadata;
const EVIDENCE = { pages: [{ id: "p", url: "https://x.test/", title: "t", http_status: 200, discovered_via: "seed" }], forms: [], cookies: [], trackers: [], third_party_services: [], policies: [], consent_signals: [] } as unknown as ScanEvidenceResponse;
const SCAN = { id: "11111111-1111-1111-1111-111111111111", url: "https://x.test/", evidence_counts: { pages: 1, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 } } as never;
const model = (score: DpdpScoreResponse) => buildReportModel(SCAN, [], META, [], [], buildScoreView(score), EVIDENCE, null, null, null);

/** A score as the server sends it once the privacy policy has been read: DPDP-R9 is Met from a quotation, DPDP-S6 stays unverified with what is needed. */
function withPolicyChecks(base: DpdpScoreResponse): DpdpScoreResponse {
  const score = structuredClone(base);
  for (const q of score.requirements) {
    if (q.id === "DPDP-R9") {
      Object.assign(q, { status: "met", counts_in_score: true, points_earned: q.weight, method: "policy_text", check_type: "policy", needed_evidence: [],
        observations: ['The privacy policy says: "Contact our Data Protection Officer at dpo@x.test." (email dpo@x.test) [https://x.test/privacy]'], pages: ["https://x.test/privacy"] });
    }
    if (q.id === "DPDP-S6") {
      Object.assign(q, { status: "unable_to_verify", method: "organisation_records", needed_evidence: ["Encryption of personal data (Rules r.6(1)(a)).", "Access control (r.6(1)(b))."],
        observations: ["The privacy policy says: \"We use encryption.\"", "A policy's description of its safeguards is not evidence that they are in place."] });
    }
  }
  return score;
}

describe("What was tested and what was not", () => {
  it.each(Object.keys(F))("%s: every applicable check is named once, as tested or as not verified", (key) => {
    const v = buildScoreView(F[key]);
    render(<CheckCoverage checks={v.allChecks} />);
    const applicable = v.allChecks.filter((c) => c.statusKey !== "not_applicable");
    const text = screen.getByTestId("check-coverage").textContent ?? "";
    for (const c of applicable) expect(text, `${key}: ${c.id}`).toContain(c.title);
    const tested = applicable.filter((c) => ["met", "partially_met", "not_met"].includes(c.statusKey)).length;
    expect(screen.getByTestId("cc-summary").textContent).toContain(`${applicable.length} applicable checks: ${tested} tested`);
    expect(text).toContain("never counted as met or as failed");
  });

  it("an unverified check says why, and what would settle it", () => {
    const v = buildScoreView(withPolicyChecks(F.mixed));
    render(<CheckCoverage checks={v.allChecks} />);
    const row = within(screen.getByTestId("cc-unverified")).getByText(/Reasonable security safeguards/).closest("tr")!;
    expect(row.textContent).toContain("Needs the organisation's own records");
    expect(row.textContent).toContain("Encryption of personal data");
    expect(row.textContent).toContain("Access control");
  });

  it("an unverified check leads with the reason, not with the quotation the policy offered", () => {
    const v = buildScoreView(withPolicyChecks(F.mixed));
    render(<CheckCoverage checks={v.allChecks} />);
    const row = within(screen.getByTestId("cc-unverified")).getByText(/Reasonable security safeguards/).closest("tr")!;
    const why = row.querySelectorAll("td")[1].textContent ?? "";
    expect(why).toContain("not evidence that they are in place");
    expect(why).not.toContain("We use encryption");
  });

  it("a check tested from the privacy policy is listed as tested, by the policy text, with its quotation", () => {
    const v = buildScoreView(withPolicyChecks(F.mixed));
    render(<CheckCoverage checks={v.allChecks} />);
    const row = within(screen.getByTestId("cc-tested")).getByText(/Contact of the Data Protection Officer/).closest("tr")!;
    expect(row.textContent).toContain("Privacy policy text");
    expect(row.textContent).toContain("Pass");   // the score card's word for Met
    expect(row.textContent).toContain("dpo@x.test");
    expect(screen.getByTestId("cc-summary").textContent).toMatch(/1 from the privacy policy text/);
  });

  it("a check that was not tested is never shown as Met or as failed", () => {
    const v = buildScoreView(withPolicyChecks(F.mixed));
    for (const c of v.allChecks.filter((x) => x.statusKey === "unable_to_verify")) {
      expect(["Pass", "Fail", "Partial"]).not.toContain(c.verdict);
      expect(c.points).toBe("not scored");
    }
  });

  it("is part of the long report and the Full appendix, and of the dashboard", () => {
    const m = model(withPolicyChecks(F.mixed));
    render(<ReportDocument model={m} />);
    expect(screen.getByTestId("check-coverage")).toBeTruthy();
    expect(document.body.textContent).toContain("What was tested and what was not");
    cleanup();
    render(<ReportDocument model={m} appendixOnly />);
    expect(screen.getByTestId("check-coverage")).toBeTruthy();
    cleanup();
    render(<DpdpScoreSection score={withPolicyChecks(F.mixed)} />);
    expect(screen.getByTestId("dpdp-check-coverage")).toBeTruthy();
  });
});

describe("a category the report normally leaves out appears once one of its checks was assessed", () => {
  it("stays hidden while nothing in it was assessed (as before)", () => {
    const v = buildScoreView(F.mixed);
    expect(isReportCategory("Data principal rights", v.assessedCategories)).toBe(false);
    expect(isReportCategory("Security and breach", v.assessedCategories)).toBe(false);
    expect(isReportCategory("Consent", v.assessedCategories)).toBe(true);
  });

  it("appears when a policy-tested check in it was assessed, and the other hidden category stays hidden", () => {
    const v = buildScoreView(withPolicyChecks(F.mixed));
    expect(v.assessedCategories.has("Data principal rights")).toBe(true);
    expect(isReportCategory("Data principal rights", v.assessedCategories)).toBe(true);
    expect(isReportCategory("Security and breach", v.assessedCategories)).toBe(false);
    expect(isReportMetric("Data principal rights: score", v.assessedCategories)).toBe(true);
    expect(isReportMetric("Security and breach: score", v.assessedCategories)).toBe(false);
  });

  it("the table of every check then lists the assessed rights check, and the tables agree with the score", () => {
    const m = model(withPolicyChecks(F.mixed));
    const { container } = render(<ReportDocument model={m} />);
    const section = container.querySelector('[data-testid="rpt-sec-checks"]');
    expect(section?.textContent).toContain("Contact of the Data Protection Officer");
    expect(section?.textContent).not.toContain("Reasonable security safeguards");     // security stays out of the table: nothing in it was assessed
  });

  it("assessedCategories counts only checks that count in the score", () => {
    expect([...assessedCategories([{ category: "A", statusKey: "met" }, { category: "B", statusKey: "unable_to_verify" }, { category: "C", statusKey: "not_applicable" }, { category: "D", statusKey: "not_met" }])].sort()).toEqual(["A", "D"]);
  });
});

describe("the check-type line says how many checks each method covers", () => {
  it("is the old sentence for a score with no policy-tested check", () => {
    expect(buildScoreView(F.mixed).checkTypesLine).toMatch(/A scan can assess only the website checks/);
  });

  it("names the policy checks when there are some", () => {
    const score = withPolicyChecks(F.mixed);
    // a score from the new server names the method on every check
    for (const q of score.requirements) if (!q.method) q.method = q.check_type === "website" ? "website_scan" : "organisation_records";
    const line = buildScoreView(score).checkTypesLine;
    expect(line).toMatch(/1 policy check/);
    expect(line).toMatch(/never counted as met or as failed/);
  });
});
