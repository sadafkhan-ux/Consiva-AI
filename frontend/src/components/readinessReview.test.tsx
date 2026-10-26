import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { PolicyAnalysis, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import { buildReadinessReview, READINESS_CLOSING } from "../report/readiness";
import { ReadinessReview } from "./ReadinessReview";

const page = (url: string, via = "link") => ({ id: url, url, title: null, http_status: 200, discovered_via: via });
const ev = {
  pages: [page("https://x.test/", "seed"), page("https://x.test/privacy"), page("https://x.test/contact")], forms: [], cookies: [],
  trackers: [{ id: "1", script_src: "https://t.example/a.js", vendor: "Tag Co", category: "analytics", source: "rule", consent_states: ["pre_consent"] }],
  third_party_services: [], policies: [{ id: "p", url: "https://x.test/privacy", policy_type: "privacy_policy" }],
  consent_signals: [{ mechanism_type: "cmp", cmp_vendor: "CMP", has_reject_all: true, has_granular_choices: null, evidence: {} }],
} as unknown as ScanEvidenceResponse;
const meta = { consent_mechanism: "cmp", reject_interaction: "clicked", accept_interaction: "clicked" } as unknown as WebsiteScanMetadata;
const policy = { status: "analysed", reason: null, sector: null, documents: [], notes: [], limitations: [], model: null, summary: null, requirements: [
  { id: "notice_data_itemised", title: "t", group: "g", basis: "required", provisions: [], status: "verified", quote: "We collect your name.", source_url: "https://x.test/privacy", detail: "", fix: "" },
] } as unknown as PolicyAnalysis;
const review = buildReadinessReview({ siteUrl: "https://x.test/", evidence: ev, meta, policy, sector: null });

describe("the five-page readiness review on the dashboard", () => {
  it("shows the page table with a URL or NOT FOUND for each of the five", () => {
    const bare = buildReadinessReview({ siteUrl: "https://x.test/", evidence: { ...ev, pages: ev.pages.filter((p) => !p.url.includes("contact")) }, meta, policy, sector: null });
    render(<ReadinessReview review={bare} />);
    const t = screen.getByTestId("readiness-pages");
    expect(within(t).getAllByRole("row")).toHaveLength(6);
    expect(within(screen.getByTestId("readiness-page-1")).getByText("NOT FOUND")).toBeInTheDocument();
    expect(within(screen.getByTestId("readiness-page-3")).getByRole("link")).toHaveAttribute("href", "https://x.test/privacy");
  });

  it("has one row per check with page, section, check, status, evidence and fix, and the five counts", () => {
    render(<ReadinessReview review={review} />);
    const rows = within(screen.getByTestId("readiness-findings")).getAllByRole("row");
    expect(rows).toHaveLength(review.rows.length + 1);
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual(expect.arrayContaining(["Page", "DPDP section", "Check", "Status", "Evidence", "Fix"]));
    const counts = screen.getByTestId("readiness-counts").textContent ?? "";
    for (const s of ["Passes this technical check", "Gap", "Not Found", "Unable to Verify", "Requires document review"]) expect(counts).toContain(s);
    expect(screen.getByText(/Tag Co \(analytics\)/)).toBeInTheDocument();
  });

  it("gives the score, the ranked gaps, the riskiest page and the closing sentence", () => {
    render(<ReadinessReview review={review} />);
    expect(screen.getByTestId("readiness-score")).toHaveTextContent(`${review.scorePercent} out of 100`);
    expect(within(screen.getByTestId("readiness-top-gaps")).getAllByRole("listitem").length).toBe(review.topGaps.length);
    expect(screen.getByTestId("readiness-riskiest")).toHaveTextContent("Highest-risk page");
    expect(screen.getByTestId("readiness-closing")).toHaveTextContent(READINESS_CLOSING);
  });

  it("says why when the review is not available", () => {
    render(<ReadinessReview review={buildReadinessReview({ siteUrl: "x", evidence: null, meta: null, policy: null, sector: null })} />);
    expect(screen.getByTestId("readiness-unavailable")).toHaveTextContent("no loaded pages");
  });
});
