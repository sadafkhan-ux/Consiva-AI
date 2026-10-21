import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { buildReportModel } from "../report/buildReport";
import { DomainsReviewSection } from "./DomainsReviewSection";
import { FindingsSection } from "./FindingsSection";
import { ScanStatusSection } from "./ScanStatusSection";
import { ThreeStateSection } from "./ThreeStateSection";
import {
  cookieScope, countByState, deriveDomainReport, evidenceDomains, findingDomainCounts, findingScope, hostKey, mainHostOf,
  splitEvidence, trackerScope,
} from "./subdomains";

const MAIN = "https://smartians.ai/";
const MAIN_HOST = "smartians.ai";

const tracker = (id: string, src: string, category: string | null, vendor: string | null, states = ["pre_consent"]) =>
  ({ id, script_src: src, vendor, category, source: "rule", consent_states: states, page_url: MAIN }) as never;
const cookie = (id: string, name: string, domain: string, category: string | null, states = ["pre_consent"]) =>
  ({ id, name, domain, category, vendor: null, is_first_party: null, source: "rule", consent_states: states }) as never;
const finding = (id: string, evidence: string[], risk = "high") =>
  ({ id, finding_text: `finding ${id}`, risk_level: risk, evidence, status: "pending", priority: "high", category: "other",
     dpdp_reference: [], recommendations: [], requires_human_review: true }) as never;

// A site in the shape of the smartians.ai report: its own pages, a named tracker it loads, a subdomain, a booking widget
// and a payment/Cloudflare service from a separate website, and a font host.
const trackers = [
  tracker("t1", "https://smartians.ai/app.js", "functional", null),
  tracker("t2", "https://www.smartians.ai/b.js", "functional", null),
  tracker("t3", "https://corp.smartians.ai/x.js", null, null),
  tracker("t4", "https://assets.corp.smartians.ai/y.js", null, null),
  tracker("t5", "https://www.googletagmanager.com/gtm.js", "analytics", "Google Tag Manager"),
  tracker("t6", "https://www.googletagmanager.com/td", "analytics", "Google Tag Manager", ["post_accept"]),
  tracker("t7", "https://calendly.com/a", null, null),
  tracker("t8", "https://assets.calendly.com/b", null, null),
  tracker("t9", "https://fonts.gstatic.com/f.woff", "functional", "Google Fonts"),
];
const cookies = [
  cookie("c1", "_ga", ".smartians.ai", "analytics"),
  cookie("c2", "sid", "corp.smartians.ai", "functional"),
  cookie("c3", "__cf_bm", "calendly.com", "functional", ["pre_consent", "post_accept", "post_reject"]),
  cookie("c4", "test_cookie", ".doubleclick.net", "marketing"),
  cookie("c5", "pref", "www.smartians.ai", null),
];
const meta = { discovered_domains: [
  { host: "corp.smartians.ai", kind: "subdomain", links: 3, sample_url: "https://corp.smartians.ai/", framed: true },
  { host: "blog.smartians.ai", kind: "subdomain", links: 1, sample_url: "https://blog.smartians.ai/", framed: false },
  { host: "twitter.com", kind: "external", links: 2, sample_url: "https://twitter.com/x", framed: false },
] } as never;

describe("hostKey / mainHostOf", () => {
  it("treats www and the bare domain as one host", () => {
    expect(hostKey("https://WWW.Smartians.ai/a")).toBe("smartians.ai");
    expect(mainHostOf("https://www.smartians.ai/")).toBe("smartians.ai");
    expect(mainHostOf(null)).toBe("");
  });
});

describe("whose evidence is it: the scanned site's own, or a discovered subdomain / separate website", () => {
  it.each([
    ["the site itself", "https://smartians.ai/a.js", "functional", "main"],
    ["www of the site", "https://www.smartians.ai/a.js", null, "main"],
    ["a subdomain", "https://corp.smartians.ai/a.js", null, "discovered"],
    ["a named analytics service the site loads", "https://www.googletagmanager.com/gtm.js", "analytics", "main"],
    ["a named advertising service", "https://connect.facebook.net/fbevents.js", "marketing", "main"],
    ["a booking widget (unclassified)", "https://calendly.com/a", null, "discovered"],
    ["a font host (functional, not a tracker)", "https://fonts.gstatic.com/f.woff", "functional", "discovered"],
    ["a named tracker on a SUBDOMAIN is still the subdomain's", "https://stats.smartians.ai/a.js", "analytics", "discovered"],
  ])("request to %s", (_name, src, category, expected) => {
    expect(trackerScope(MAIN_HOST, tracker("x", src, category, null)).scope).toBe(expected);
  });

  it.each([
    ["set for the site's own host", "smartians.ai", null, "main"],
    ["set for a parent domain, with the leading dot", ".smartians.ai", "analytics", "main"],
    ["set for www", "www.smartians.ai", null, "main"],
    ["set by a subdomain", "corp.smartians.ai", "functional", "discovered"],
    ["set by a separate website's service", "calendly.com", "functional", "discovered"],
    ["set by a named third-party tracker", ".doubleclick.net", "marketing", "main"],
    ["with no domain recorded", null as unknown as string, null, "main"],
  ])("cookie %s", (_name, domain, category, expected) => {
    expect(cookieScope(MAIN_HOST, cookie("x", "n", domain, category)).scope).toBe(expected);
  });

  it("a parent-domain cookie is the site's own even when the entered host is itself a subdomain", () => {
    expect(cookieScope("app.example.com", cookie("x", "n", ".example.com", null)).scope).toBe("main");
  });

  it("splits the evidence without dropping any of it", () => {
    const s = splitEvidence(MAIN_HOST, trackers, cookies);

    expect(s.mainTrackers.map((t) => t.id)).toEqual(["t1", "t2", "t5", "t6"]);
    expect(s.discoveredTrackers.map((t) => t.id)).toEqual(["t3", "t4", "t7", "t8", "t9"]);
    expect(s.mainCookies.map((c) => c.id)).toEqual(["c1", "c4", "c5"]);
    expect(s.discoveredCookies.map((c) => c.id)).toEqual(["c2", "c3"]);
    expect(s.mainTrackers.length + s.discoveredTrackers.length).toBe(trackers.length);
    expect(s.mainCookies.length + s.discoveredCookies.length).toBe(cookies.length);
  });

  it("counts per consent state", () => {
    expect(countByState(splitEvidence(MAIN_HOST, trackers, cookies).discoveredCookies)).toEqual({ pre_consent: 2, post_accept: 1, post_reject: 1 });
  });
});

describe("deriveDomainReport lists only what is NOT the site's own", () => {
  const report = deriveDomainReport({ scanUrl: MAIN, trackers, cookies, findings: [], meta });

  it("lists subdomains by full host and separate websites by registered domain, without the site or its named trackers", () => {
    expect(report.mainHost).toBe("smartians.ai");
    expect(report.subdomains.map((r) => r.key).sort()).toEqual(["assets.corp.smartians.ai", "blog.smartians.ai", "corp.smartians.ai"]);
    expect(report.external.map((r) => r.key).sort()).toEqual(["calendly.com", "gstatic.com", "twitter.com"]);
    const all = [...report.subdomains, ...report.external].map((r) => r.key);
    expect(all).not.toContain("smartians.ai");
    expect(all).not.toContain("googletagmanager.com");   // a named tracker the site loaded: the site's own result
    expect(all).not.toContain("doubleclick.net");
  });

  it("counts a domain's requests and cookies per consent state, apart from the site's", () => {
    const calendly = report.external.find((r) => r.key === "calendly.com")!;
    expect(calendly.requests).toEqual({ pre_consent: 2, post_accept: 0, post_reject: 0 });
    expect(calendly.cookies).toMatchObject({ pre_consent: 1, post_accept: 1, post_reject: 1, names: ["__cf_bm"] });
    expect(calendly.unclassifiedRequests).toBe(2);
    expect(report.subdomains.find((r) => r.key === "corp.smartians.ai")!.cookies.names).toEqual(["sid"]);
  });

  it("merges the links and frames the crawl recorded, which are not requests", () => {
    const corp = report.subdomains.find((r) => r.key === "corp.smartians.ai")!;
    expect(corp).toMatchObject({ linksFound: 3, framed: true, sampleUrl: "https://corp.smartians.ai/" });
    expect(report.subdomains.find((r) => r.key === "blog.smartians.ai")).toMatchObject({ linksFound: 1, requests: { pre_consent: 0 } });
  });

  it("marks what needs review and puts it first", () => {
    const review = (key: string) => [...report.subdomains, ...report.external].find((r) => r.key === key)!.needsReview;
    expect(review("blog.smartians.ai")).toBe(true);
    expect(review("calendly.com")).toBe(true);        // unknown requests and a pre-consent cookie
    expect(review("twitter.com")).toBe(false);        // only linked
    expect(report.subdomains[0].key).toBe("corp.smartians.ai");
    expect(report.external[0].key).toBe("calendly.com");
  });

  it("is empty when the scan met nothing but the site itself and its named trackers", () => {
    const r = deriveDomainReport({
      scanUrl: MAIN, trackers: [trackers[0], trackers[4]], cookies: [cookies[0]], findings: [], meta: null,
    });
    expect(r.subdomains).toEqual([]);
    expect(r.external).toEqual([]);
  });
});

describe("findings: whom are they about", () => {
  const index = evidenceDomains(MAIN_HOST, trackers, cookies);

  it("attributes evidence to the site or to a discovered domain", () => {
    expect(findingDomainCounts(finding("f", ["t1", "t5", "t3", "t7", "c3", "nope"]), index)).toEqual({
      "smartians.ai": 2, "corp.smartians.ai": 1, "calendly.com": 2,
    });
  });

  it.each([
    ["only the site's own evidence", { "smartians.ai": 2 }, "main"],
    ["only discovered domains", { "calendly.com": 2, "corp.smartians.ai": 1 }, "discovered"],
    ["both", { "smartians.ai": 1, "calendly.com": 1 }, "mixed"],
    ["no evidence this page can place: stays the site's until shown otherwise", {}, "main"],
  ])("%s -> %s", (_n, counts, expected) => {
    expect(findingScope(counts as Record<string, number>, MAIN_HOST)).toBe(expected);
  });

  it("lists a finding under each discovered domain it involves, with how much of it", () => {
    const withFinding = deriveDomainReport({ scanUrl: MAIN, trackers, cookies, findings: [finding("f", ["t1", "t3", "t7"])], meta: null });

    expect(withFinding.subdomains.find((r) => r.key === "corp.smartians.ai")!.findings).toEqual(
      [{ id: "f", text: "finding f", risk: "high", fromDomain: 1, total: 3 }]);
    expect(withFinding.external.find((r) => r.key === "calendly.com")!.findings[0]).toMatchObject({ fromDomain: 1, total: 3 });
  });
});

describe("DomainsReviewSection", () => {
  const row = (key: string, kind: "subdomain" | "external", over: Record<string, unknown> = {}) => ({
    key, kind, requests: { pre_consent: 0, post_accept: 0, post_reject: 0 }, cookies: { pre_consent: 0, post_accept: 0, post_reject: 0, names: [] },
    namedVendors: [], unclassifiedRequests: 0, linksFound: 0, sampleUrl: null, framed: false, findings: [], needsReview: false, ...over,
  }) as never;

  it("renders nothing when there is nothing to review", () => {
    const { container } = render(<DomainsReviewSection report={{ mainHost: "smartians.ai", subdomains: [], external: [] }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("is headed 'Discovered Subdomains & Websites' and says these are not the site's pages, trackers or cookies", () => {
    render(<DomainsReviewSection report={{
      mainHost: "smartians.ai",
      subdomains: [row("corp.smartians.ai", "subdomain", { linksFound: 3, sampleUrl: "https://corp.smartians.ai/", framed: true, needsReview: true })],
      external: [row("calendly.com", "external", {
        requests: { pre_consent: 2, post_accept: 1, post_reject: 1 }, cookies: { pre_consent: 1, post_accept: 1, post_reject: 1, names: ["__cf_bm"] },
        unclassifiedRequests: 2, needsReview: true })],
    }} />);

    expect(screen.getByRole("heading", { name: "Discovered Subdomains & Websites" })).toBeInTheDocument();
    const section = screen.getByTestId("domains-review");
    expect(section).toHaveTextContent("covers smartians.ai only");
    expect(section).toHaveTextContent("not pages of smartians.ai");
    expect(section).toHaveTextContent("counted here, not in the site's own trackers and cookies");
    expect(section).toHaveClass("domain-review");

    const corp = screen.getByTestId("domain-corp.smartians.ai");
    expect(corp).toHaveTextContent("subdomain");
    expect(corp).toHaveTextContent("shown in a frame");
    expect(corp).toHaveTextContent("Linked 3 times, not followed");
    expect(corp).toHaveTextContent("Enter it as the address to scan it");

    const calendly = screen.getByTestId("domain-calendly.com");
    expect(calendly).toHaveTextContent("separate website");
    expect(calendly).toHaveTextContent("Requests — before consent 2 · after Accept 1 · after Reject 1");
    expect(calendly).toHaveTextContent("Cookies set — before consent 1 · after Accept 1 · after Reject 1 (__cf_bm)");
    expect(calendly).toHaveTextContent("2 requests before consent could not be classified: purpose unknown");
    expect(calendly).not.toHaveTextContent("Enter it as the address to scan it");
  });

  it("lists findings under the domain they involve", () => {
    render(<DomainsReviewSection report={{
      mainHost: "smartians.ai", subdomains: [],
      external: [row("calendly.com", "external", { findings: [{ id: "f1", text: "Trackers fired before consent", risk: "high", fromDomain: 2, total: 5 }] })],
    }} />);

    expect(screen.getByTestId("domain-calendly.com")).toHaveTextContent("2 of 5 evidence items for this finding involve calendly.com, requested by smartians.ai");
  });

  it("shows the first 15 separate websites and expands on request", () => {
    const external = Array.from({ length: 20 }, (_, i) => row(`site${String(i).padStart(2, "0")}.test`, "external"));
    render(<DomainsReviewSection report={{ mainHost: "smartians.ai", subdomains: [], external }} />);

    expect(screen.queryByTestId("domain-site15.test")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("domains-toggle"));
    expect(screen.getByTestId("domain-site19.test")).toBeInTheDocument();
  });
});

describe("the main findings list keeps the site's results apart from the discovered domains'", () => {
  const props = { onDecision: async () => {}, mainHost: MAIN_HOST };

  it("lists a finding about discovered domains ONLY in its own highlighted group, with review actions intact", () => {
    render(<FindingsSection
      {...props}
      findings={[finding("own", ["t1"]), finding("other", ["t7"]), finding("both", ["t1", "t7"])]}
      scopes={{ own: "main", other: "discovered", both: "mixed" }}
      hostCounts={{ own: { "smartians.ai": 1 }, other: { "calendly.com": 1 }, both: { "smartians.ai": 1, "calendly.com": 1 } }}
    />);

    const group = screen.getByTestId("discovered-findings");
    expect(group).toHaveClass("domain-review");
    expect(group).toHaveTextContent("Findings about discovered subdomains & websites (1)");
    expect(group).toHaveTextContent("finding other");
    expect(group).not.toHaveTextContent("finding own");
    expect(group).not.toHaveTextContent("finding both");
    expect(group).toHaveTextContent("not about smartians.ai itself");
    expect(group.querySelectorAll("button.approve").length).toBe(1);          // still needs a review decision

    expect(screen.getByTestId("finding-scope-both")).toHaveTextContent("also involves discovered subdomains / websites");
    expect(screen.queryByTestId("finding-scope-own")).not.toBeInTheDocument();
  });

  it("names the domains a finding involves and points to the section", () => {
    render(<FindingsSection {...props} findings={[finding("f1", ["t1"])]} scopes={{ f1: "mixed" }}
      hostCounts={{ f1: { "smartians.ai": 2, "calendly.com": 3 } }} />);

    const line = screen.getByTestId("finding-hosts-f1");
    expect(line).toHaveTextContent("smartians.ai (2)");
    expect(line).toHaveTextContent("calendly.com (3) — see Discovered Subdomains & Websites");
    expect(line.textContent).not.toMatch(/smartians\.ai \(2\) — see/);
  });

  it("shows no separate group when every finding is the site's own", () => {
    render(<FindingsSection {...props} findings={[finding("own", ["t1"])]} scopes={{ own: "main" }} />);
    expect(screen.queryByTestId("discovered-findings")).not.toBeInTheDocument();
  });
});

describe("the three consent-state cards count the site's own evidence and say what was left out", () => {
  const scoped = {
    cookies: { pre_consent: 3, post_accept: 3, post_reject: 3 }, trackers: { pre_consent: 1, post_accept: 1, post_reject: 0 },
    discoveredCookies: { pre_consent: 2, post_accept: 1, post_reject: 0 }, discoveredTrackers: { pre_consent: 5, post_accept: 0, post_reject: 0 },
  };
  const m = { accept_interaction: "clicked", reject_interaction: "clicked", cookies_by_consent_state: { pre_consent: 99 }, trackers_by_consent_state: { pre_consent: 99 } } as never;

  it("uses the scoped figures, not the scan's totals", () => {
    render(<ThreeStateSection meta={m} trackers={[]} scoped={scoped} />);

    expect(screen.queryByText("99")).not.toBeInTheDocument();
    expect(screen.getByTestId("state-discovered-pre_consent")).toHaveTextContent("Not counted above: 5 requests and 2 cookies from discovered subdomains & websites");
    expect(screen.queryByTestId("state-discovered-post_reject")).not.toBeInTheDocument();
  });

  it("without scoped figures it shows the scan's totals as before", () => {
    render(<ThreeStateSection meta={m} trackers={[]} />);
    expect(screen.getAllByText("99", { selector: "b" })).toHaveLength(2);   // cookies and trackers, before consent
  });
});

describe("the PDF summary", () => {
  const meta2 = { consent_mechanism: "cmp", cmp_vendor: null, accept_interaction: "clicked", reject_interaction: "clicked", pages_scanned: 3, trackers_by_consent_state: { pre_consent: 0 } } as never;

  it("says what was found apart from the site's own results", () => {
    const model = buildReportModel(null, [], meta2, [], [], { requests: 9, cookies: 2, domains: 4 });

    expect(model.discoveredLine).toContain("4 subdomains or separate websites (9 requests, 2 cookies)");
    expect(model.discoveredLine).toContain("counted apart from the site's own results");
    expect(model.discoveredLine).toContain("not scanned as pages");
  });

  it("says nothing when none was found", () => {
    expect(buildReportModel(null, [], meta2, [], [], { requests: 0, cookies: 0, domains: 0 }).discoveredLine).toBeNull();
    expect(buildReportModel(null, [], meta2).discoveredLine).toBeNull();
  });
});

describe("pages that were not scanned", () => {
  const state = (m: unknown) => ({
    phase: "completed", scanId: "abc12345", scan: null, stages: [], startedAt: null, completedAt: null, durationMs: null,
    websiteScanMeta: m, error: null, failedStage: null,
  }) as never;

  it("lists every exclusion with its reason, including subdomains left for review", () => {
    render(<ScanStatusSection state={state({
      page_limit: 3, stop_reason: "page_limit", pages_scanned: 3,
      skipped_by_reason: { page_limit: 6, robots_disallowed: 2, subdomain_not_scanned: 1, redirected_to_subdomain: 1 },
      skipped_pages: [{ url: "https://smartians.ai/blog", reason: "page_limit" }, { url: "https://corp.smartians.ai/", reason: "subdomain_not_scanned" }],
    })} />);

    const block = screen.getByTestId("skipped-pages");
    expect(block).toHaveTextContent("Pages not scanned (10)");
    expect(block).toHaveTextContent("1 subdomain: not scanned, listed for review");
    expect(block).toHaveTextContent("https://corp.smartians.ai/ — subdomain: not scanned, listed for review");
  });

  it("shows nothing when nothing was skipped", () => {
    render(<ScanStatusSection state={state({ page_limit: 3, skipped_by_reason: {} })} />);
    expect(screen.queryByTestId("skipped-pages")).not.toBeInTheDocument();
  });
});
