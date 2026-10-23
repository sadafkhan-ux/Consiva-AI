import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DpdpScoreResponse, EvidenceCookieItem, PolicyAnalysis, PolicyRequirementResult, WebsiteScanMetadata } from "../api/types";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildInsightSections } from "../report/insights";
import { CookiesTable } from "./CookiesTable";
import { DpdpScoreSection } from "./DpdpScoreSection";
import { InsightSections } from "./InsightSections";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;

const row = (id: string, status: string, over: Partial<PolicyRequirementResult> = {}): PolicyRequirementResult => ({
  id, title: id, group: "Grievance", basis: "required", provisions: ["DPDP Act s.13(1)"], status, quote: status === "verified" ? `A passage for ${id} that is long enough to count.` : "",
  source_url: status === "verified" ? "https://x.test/privacy" : null, detail: "", fix: "", contacts: null, response_days: null, ...over,
});
const policy: PolicyAnalysis = {
  status: "analysed", reason: null, sector: null, documents: [], notes: [], limitations: [], model: null,
  summary: { applicable: 3, verified: 3, partial: 0, not_found: 0, not_applicable: 0, quotes_rejected: 0, completeness_percent: 100 },
  requirements: [row("grievance_contact", "verified", { contacts: { emails: ["g@x.test"], phones: [], urls: [] } }), row("grievance_timeline", "verified", { response_days: 30 }), row("dpo_contact", "verified")],
};
const meta: WebsiteScanMetadata = {
  language_signals: { pages_checked: 3, pages_with_indic_script: 1, indic_scripts: { Tamil: 10 }, switcher_scripts: [], hreflang: [], html_lang: "en" },
  security_signals: { pages_total: 3, pages_https: 3, homepage_https: true, homepage_headers: { hsts: "x", csp: null, x_frame_options: null, x_content_type_options: "nosniff", referrer_policy: null, permissions_policy: null } },
};

describe("the dedicated sections on the dashboard", () => {
  const sections = buildInsightSections({ policy, meta, evidence: null, sector: null });

  it("shows each of the five, in order, with its status and the kind of knowledge it rests on", () => {
    render(<InsightSections sections={sections} />);
    for (const key of ["grievance", "dpo", "language", "children", "security"]) {
      expect(screen.getByTestId(`insight-${key}`)).toBeInTheDocument();
      expect(screen.getByTestId(`insight-${key}-status`)).toBeInTheDocument();
      expect(["Confirmed", "Observed", "Not verified"]).toContain(screen.getByTestId(`insight-${key}-knowledge`).textContent);
    }
    const order = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(order).toEqual(["Grievance officer", "Data Protection Officer", "Indian-language support", "Children's data and age gate", "Observable security signals"]);
  });

  it("gives the grievance contact, the response period, the policy's own words with their page, and the provisions", () => {
    render(<InsightSections sections={sections} />);
    const g = screen.getByTestId("insight-grievance");
    expect(screen.getByTestId("insight-grievance-status")).toHaveTextContent("Grievance contact found");
    expect(screen.getByTestId("insight-grievance-knowledge")).toHaveTextContent("Confirmed");
    expect(within(screen.getByTestId("insight-grievance-facts")).getByText("g@x.test")).toBeInTheDocument();
    expect(within(g).getByText(/Within about 30 days/)).toBeInTheDocument();
    expect(screen.getByTestId("insight-grievance-quote")).toHaveTextContent("A passage for grievance_contact that is long enough to count.");
    expect(screen.getByTestId("insight-grievance-quote").querySelector("a")).toHaveAttribute("href", "https://x.test/privacy");
    expect(screen.getByTestId("insight-grievance-legal")).toHaveTextContent("DPDP Act, 2023, Section 13(1)");
  });

  it("separates what was observed from what was not verified: language is observed, children is not applicable and not verified", () => {
    render(<InsightSections sections={sections} />);
    expect(screen.getByTestId("insight-language-knowledge")).toHaveTextContent("Observed");
    expect(screen.getByTestId("insight-children-knowledge")).toHaveTextContent("Not verified");
    expect(screen.getByTestId("insight-children-status")).toHaveTextContent("Not applicable on the evidence");
    expect(screen.getByTestId("insight-security-status")).toHaveTextContent("4 of 8 signals observed");
    expect(screen.getByTestId("insight-security")).toHaveTextContent("cannot be seen from outside");
  });

  it("says plainly that nothing was recorded, rather than showing a clean result, for a scan made before the signals existed", () => {
    render(<InsightSections sections={buildInsightSections({ policy: null, meta: null, evidence: null, sector: null })} />);
    expect(screen.getByTestId("insight-language-status")).toHaveTextContent("Not recorded for this scan");
    expect(screen.getByTestId("insight-security-status")).toHaveTextContent("Not recorded for this scan");
    expect(screen.getByTestId("insight-grievance-status")).toHaveTextContent("Not assessed");
    expect(screen.getByTestId("insight-security").textContent).not.toMatch(/violat/i);
  });
});

const ck = (over: Partial<EvidenceCookieItem> = {}): EvidenceCookieItem => ({
  id: over.name ?? "_ga", name: "_ga", domain: "x.test", category: "analytics", vendor: "Google Analytics", is_first_party: true, source: "lookup", consent_states: ["pre_consent", "post_accept"],
  path: "/", expiry: "2027-11-26T15:18:31+00:00", secure: false, http_only: false, same_site: "Lax", observations: [], provider: "Google Analytics",
  purpose: "Gives each browser a random identifier so Google Analytics can count visitors and sessions.", purpose_source: "catalogue", is_session: false, lifetime_days: 400, ...over,
});

describe("the cookie table on the dashboard", () => {
  const cookies = [
    ck(),
    ck({ name: "_fbp", category: "marketing", provider: "Meta (Facebook)", is_first_party: false, consent_states: ["post_accept", "post_reject"], lifetime_days: 90, purpose: "Identifies the browser so the Meta Pixel can measure ads and deliver them." }),
    ck({ name: "zz_x", category: null, provider: null, vendor: null, purpose: "The purpose could not be identified from the cookie's name.", purpose_source: "unknown", consent_states: ["post_accept"], expiry: null, is_session: true, lifetime_days: null }),
  ];

  it("opens with the five counts", () => {
    render(<CookiesTable cookies={cookies} untestedStates={[]} />);
    const tiles = screen.getByTestId("cookie-summary");
    for (const [label, value] of [["Cookies found", "3"], ["Third-party", "1"], ["Set before consent", "1"], ["Long-lived (over a year)", "1"], ["Seen after Reject", "1"]]) {
      expect(within(tiles).getByText(label).previousElementSibling).toHaveTextContent(value);
    }
  });

  it("says the Reject count was not tested when no choice was made, instead of a zero", () => {
    render(<CookiesTable cookies={cookies} untestedStates={["post_reject"]} />);
    expect(within(screen.getByTestId("cookie-summary")).getByText("Seen after Reject").previousElementSibling).toHaveTextContent("Not tested");
  });

  it("gives each cookie its provider, purpose, lifetime and what it did in each consent state", () => {
    render(<CookiesTable cookies={cookies} untestedStates={[]} />);
    expect(screen.getByTestId("cookie-purpose-_ga")).toHaveTextContent("count visitors and sessions");
    expect(screen.getByTestId("cookie-purpose-_ga")).not.toHaveTextContent("not identified");
    expect(screen.getByTestId("cookie-purpose-zz_x")).toHaveTextContent("could not be identified from the cookie's name");
    expect(screen.getByTestId("cookie-before-_ga")).toHaveTextContent("Set");
    expect(screen.getByTestId("cookie-accept-_ga")).toHaveTextContent("Set");
    expect(screen.getByTestId("cookie-reject-_ga")).toHaveTextContent("Not seen");
    expect(screen.getByTestId("cookie-reject-_fbp")).toHaveTextContent("Set");
    const ga = screen.getByText("_ga").closest("tr")!;       // a cookie's row is a button that opens its evidence, so find it by its name
    expect(ga).toHaveTextContent("13 months");
    expect(ga).toHaveTextContent("long-lived");
    expect(screen.getByText("zz_x").closest("tr")).toHaveTextContent("Session");
    expect(screen.getByText("Meta (Facebook)")).toBeInTheDocument();
  });

  it("marks a state that was never tested as Not tested for every cookie, not Not seen", () => {
    render(<CookiesTable cookies={[ck({ consent_states: ["pre_consent"] })]} untestedStates={["post_accept", "post_reject"]} />);
    expect(screen.getByTestId("cookie-accept-_ga")).toHaveTextContent("Not tested");
    expect(screen.getByTestId("cookie-reject-_ga")).toHaveTextContent("Not tested");
  });

  it("says that the Act sets no cookie lifetime, and that a purpose is only given for a recognised cookie", () => {
    render(<CookiesTable cookies={cookies} />);
    const note = screen.getByTestId("cookie-lifetime-note");
    expect(note).toHaveTextContent("The Act and Rules set no limit on a cookie's lifetime");
    expect(note).toHaveTextContent("only for a recognised cookie");
  });

  it("still has the search, the by-state counts and the empty message it had", () => {
    const { unmount } = render(<CookiesTable cookies={cookies} />);
    expect(screen.getByPlaceholderText(/Search cookies/)).toBeInTheDocument();
    expect(screen.getByText("By consent state")).toBeInTheDocument();
    unmount();
    render(<CookiesTable cookies={[]} />);
    expect(screen.getByText("No cookies were detected on this scan.")).toBeInTheDocument();
  });
});

describe("the score card's remediation steps", () => {
  it("say what fixing each does to the score and why it matters, when the server gave the figures", () => {
    const s = JSON.parse(JSON.stringify(F.review)) as DpdpScoreResponse;
    s.remediation_plan = s.remediation_plan.map((p) => ({ ...p, current_percent: 45.5, percent_if_fixed: 90.9, gain_percent: 45.4, impact: "Visitors' browsing is shared before they have chosen." }));
    render(<DpdpScoreSection score={s} />);
    const first = screen.getByTestId("dpdp-step-1");
    expect(first).toHaveTextContent("Impact on the score: Fixing this alone would raise the readiness score from 45.5% to 90.9% (+45.4 percentage points).");
    expect(first).toHaveTextContent("Why it matters: Visitors' browsing is shared before they have chosen.");
    expect(screen.getByTestId("dpdp-step-1-impact")).toBeInTheDocument();
  });

  it("show no impact line when there are no figures", () => {
    render(<DpdpScoreSection score={F.review} />);
    expect(screen.queryByTestId("dpdp-step-1-impact")).not.toBeInTheDocument();
    expect(screen.getByTestId("dpdp-step-1")).not.toHaveTextContent("Why it matters");
  });
});
