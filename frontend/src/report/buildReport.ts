import type {
  EvidenceCookieItem, EvidenceTrackerItem, FindingResponse, ScanStatusResponse, WebsiteScanMetadata,
} from "../api/types";
import type { DomainReport, DomainRow } from "../components/subdomains";
import { summariseTrackers } from "./trackerSummary";
import { buildCookieInventory, type CookieInventory } from "./cookieInventory";

/** Risk ordering, worst first -- used both to pick the overall band and to decide which
 *  findings earn a place on a deliberately one-page summary. */
const RISK_ORDER = ["critical", "high", "medium", "low", "info"] as const;

function riskRank(level: string): number {
  const i = RISK_ORDER.indexOf(level.toLowerCase() as (typeof RISK_ORDER)[number]);
  return i === -1 ? RISK_ORDER.length : i;
}

/** One discovered subdomain or separate website, as the PDF prints it. */
export interface DiscoveredPdfRow {
  name: string;
  kind: "subdomain" | "separate website";
  review: boolean;
  framed: boolean;
  /** Where it was found: the link or frame it was found through, and example requests the scanned pages made to it. */
  urls: { label: string; url: string }[];
  requests: string;
  cookies: string | null;
  vendors: string | null;
  unclassified: string | null;
  findings: { risk: string; text: string; share: string }[];
}

export interface DiscoveredPdfSection {
  mainHost: string;
  subdomains: DiscoveredPdfRow[];
  websites: DiscoveredPdfRow[];
}

function pdfRow(r: DomainRow): DiscoveredPdfRow {
  const q = r.requests;
  const c = r.cookies;
  const cookieTotal = c.pre_consent + c.post_accept + c.post_reject;
  const urls: DiscoveredPdfRow["urls"] = [];
  for (const u of r.foundUrls) urls.push({ label: r.framed ? "Shown in a frame" : "Linked from the scanned site", url: u });
  if (r.moreHosts > 0) urls.push({ label: "More", url: `and ${r.moreHosts} more host${r.moreHosts === 1 ? "" : "s"} of this website` });
  for (const u of r.requestUrls) urls.push({ label: "Requested by a scanned page", url: u });
  return {
    name: r.key,
    kind: r.kind === "subdomain" ? "subdomain" : "separate website",
    review: r.needsReview,
    framed: r.framed,
    urls,
    requests: q.pre_consent + q.post_accept + q.post_reject === 0
      ? "No requests to it were seen."
      : `Requests: before consent ${q.pre_consent} · after Accept ${q.post_accept} · after Reject ${q.post_reject}`,
    cookies: cookieTotal === 0
      ? null
      : `Cookies set: before consent ${c.pre_consent} · after Accept ${c.post_accept} · after Reject ${c.post_reject} (${c.names.join(", ")})`,
    vendors: r.namedVendors.length ? `Named analytics or advertising: ${r.namedVendors.join(", ")}` : null,
    unclassified: r.unclassifiedRequests
      ? `${r.unclassifiedRequests} request${r.unclassifiedRequests === 1 ? "" : "s"} before consent could not be classified: purpose unknown.`
      : null,
    findings: r.findings.map((f) => ({ risk: f.risk, text: f.text, share: `${f.fromDomain} of ${f.total} evidence items for this finding involve it.` })),
  };
}

export function discoveredPdfSection(report: DomainReport | null): DiscoveredPdfSection | null {
  if (!report || (report.subdomains.length === 0 && report.external.length === 0)) return null;
  return { mainHost: report.mainHost, subdomains: report.subdomains.map(pdfRow), websites: report.external.map(pdfRow) };
}

export interface ReportModel {
  siteUrl: string;
  generatedAt: string;
  overallRisk: string;
  /** True while findings still carry status "pending" -- the report is a draft until a
   *  human has actually signed off, and must say so rather than implying sign-off. */
  provisional: boolean;
  headline: string;
  /** Subdomains and separate websites the scan came across: counted apart, not in the figures above. Null when none. */
  discoveredLine: string | null;
  /** Every discovered subdomain and separate website, with its URLs: printed on its own highlighted pages. Null when none. */
  discoveredSection: DiscoveredPdfSection | null;
  /** What was actually tested: pages, and which consent states could be exercised. Printed under the headline. */
  scope: string;
  counts: { label: string; value: string }[];
  problems: { risk: string; text: string }[];
  consentIssues: string[];
  dpdpRefs: string[];
  actions: string[];
  totalFindings: number;
  shownFindings: number;
  /** The complete cookie inventory, appended after the one-page summary. */
  cookies: CookieInventory;
}

/** Everything the one-page PDF needs, derived from data the app already has in state.
 *  Pure and side-effect free so it can be unit-tested and reasoned about separately
 *  from the rendering. */
export function buildReportModel(
  scan: ScanStatusResponse | null,
  findings: FindingResponse[],
  meta: WebsiteScanMetadata | null,
  cookies: EvidenceCookieItem[] = [],
  trackers: EvidenceTrackerItem[] = [],
  /** What the scan came across on subdomains and separate websites. `cookies` and `trackers` above are the site's own. */
  discovered: { requests: number; cookies: number; domains: number } | null = null,
  domainReport: DomainReport | null = null,
): ReportModel {
  const sorted = [...findings].sort((a, b) => riskRank(a.risk_level) - riskRank(b.risk_level));
  const overallRisk = sorted.length ? sorted[0].risk_level : "none";
  const provisional = findings.some((f) => f.status === "pending");

  const counts: { label: string; value: string }[] = [];
  if (scan?.evidence_counts) {
    const c = scan.evidence_counts;
    counts.push(
      { label: "Pages scanned", value: String(c.pages) },
      { label: "Cookies", value: String(c.cookies) },
      // Every third-party request the scan saw, not "trackers": it includes the consent tool's own script and fonts.
      { label: "Third-party requests", value: String(c.trackers) },
      { label: "Third parties", value: String(c.third_party_services) },
      { label: "Policies found", value: String(c.policies) },
    );
  }

  // Consent-mechanism headlines, read from the scanner's real metadata fields. Only
  // statements the scan actually evidences -- an absent/unknown field is omitted
  // rather than reported as a negative finding.
  const consentIssues: string[] = [];
  if (meta) {
    if (meta.consent_mechanism === "none") {
      consentIssues.push("No consent banner or CMP was detected on the pages scanned.");
    } else if (meta.consent_mechanism === "banner" || meta.consent_mechanism === "cmp") {
      consentIssues.push(
        meta.cmp_vendor
          ? `Consent mechanism detected (${meta.cmp_vendor}).`
          : "Consent mechanism detected; the vendor could not be identified.",
      );
    }
    // "cmp_not_found" / "cmp_not_automatable" mean the scanner could not exercise the
    // control -- a scan limitation, distinct from the control being genuinely absent,
    // so the wording deliberately does not assert non-compliance.
    // "cmp_not_found" means no banner was found at all, which the line above already says; it is not a statement that a
    // banner lacks a Reject control. Only a control that was found and could not be used is reported here.
    if (meta.reject_interaction === "click_failed") {
      consentIssues.push("A 'Reject' control was found but could not be exercised during the scan.");
    }
    if (meta.accept_interaction === "click_failed") {
      consentIssues.push("An 'Accept' control was found but could not be exercised during the scan.");
    }

    // The strongest single signal in this report: trackers already firing before any
    // consent decision was made. Taken from the scanner's own per-state counts.
    const preConsent = meta.trackers_by_consent_state?.pre_consent ?? 0;
    const pre = trackers.length ? summariseTrackers(trackers).pre_consent : null;
    if (pre && (pre.analyticsAdRequests > 0 || pre.unclassifiedRequests > 0)) {
      // What the 'N trackers' figure is made of. Named analytics and advertising is the finding; the unknown part is
      // not dismissed and not called a violation -- it is for a person to look at.
      if (pre.analyticsAdRequests > 0) {
        consentIssues.push(
          `${pre.analyticsAdRequests} request${pre.analyticsAdRequests === 1 ? "" : "s"} to ${pre.analyticsAdVendors} named analytics or advertising vendor${pre.analyticsAdVendors === 1 ? "" : "s"} were made before any consent was given.`,
        );
      }
      if (pre.unclassifiedRequests > 0) {
        consentIssues.push(
          `${pre.unclassifiedRequests} further third-party request${pre.unclassifiedRequests === 1 ? "" : "s"} before consent (${pre.unclassifiedDomains} domain${pre.unclassifiedDomains === 1 ? "" : "s"}) could not be classified and need review.`,
        );
      }
    } else if (preConsent > 0) {
      consentIssues.push(
        `${preConsent} third-party request${preConsent === 1 ? "" : "s"} were made before any consent was given (not classified in this report).`,
      );
    }
  }

  // De-duplicated legal references, preferring "Doc — Section" where a section was
  // actually detected (never fabricated; section is null when the chunker could not
  // identify one, and is then simply left off).
  const refSet = new Set<string>();
  for (const f of findings) {
    for (const r of f.dpdp_reference ?? []) {
      refSet.add(r.section ? `${r.source_doc} — ${r.section}` : r.source_doc);
    }
  }

  const actions: string[] = [];
  for (const f of sorted) {
    for (const rec of f.recommendations ?? []) {
      if (rec.recommendation_text && !actions.includes(rec.recommendation_text)) {
        actions.push(rec.recommendation_text);
      }
    }
  }

  const MAX_PROBLEMS = 4;
  const MAX_ACTIONS = 5;

  // What could actually be tested. A scan that never exercised Accept or Reject has not shown that those states are
  // fine, so "nothing found" must not read as "clean".
  const tested = (outcome: string | null | undefined) => outcome === "clicked";
  const allStatesTested = !!meta && tested(meta.accept_interaction) && tested(meta.reject_interaction);
  const pages = meta?.pages_scanned ?? scan?.evidence_counts?.pages;
  const stateWord = (outcome: string | null | undefined) => (tested(outcome) ? "tested" : "NOT tested");
  const scope = meta
    ? `Scope: ${pages ?? "?"} page${pages === 1 ? "" : "s"} scanned. Consent states: before consent tested; Accept ${stateWord(meta.accept_interaction)}; Reject ${stateWord(meta.reject_interaction)}.`
    : "Scope: not recorded for this scan.";

  // The consent states in which nothing was clicked. Their counts are the same page seen again.
  const untestedStates = [
    ...(meta && !tested(meta.accept_interaction) ? ["post_accept"] : []),
    ...(meta && !tested(meta.reject_interaction) ? ["post_reject"] : []),
  ];

  const headline = sorted.length
    ? `${sorted.length} ${sorted.length === 1 ? "finding" : "findings"} to review; highest severity is ${overallRisk.toUpperCase()}.`
    : allStatesTested
      ? "No issues were observed in the tested scope (all three consent states were tested)."
      : "No findings were produced, but not every consent state could be tested: this is not a clean result.";

  return {
    siteUrl: scan?.url ?? "(unknown site)",
    generatedAt: new Date().toLocaleString(),
    overallRisk,
    provisional,
    headline,
    scope,
    discoveredSection: discoveredPdfSection(domainReport),
    discoveredLine: discovered && discovered.domains > 0
      ? `Also found, and counted apart from the site's own results: ${discovered.domains} subdomain${discovered.domains === 1 ? "" : "s"} or separate website${discovered.domains === 1 ? "" : "s"} (${discovered.requests} request${discovered.requests === 1 ? "" : "s"}, ${discovered.cookies} cookie${discovered.cookies === 1 ? "" : "s"}). They were not scanned as pages; they are listed, with their URLs, in the Discovered Subdomains & Websites section that follows this page.`
      : null,
    counts,
    problems: sorted.slice(0, MAX_PROBLEMS).map((f) => ({ risk: f.risk_level, text: f.finding_text })),
    consentIssues: consentIssues.slice(0, 4),
    dpdpRefs: [...refSet].slice(0, 5),
    actions: actions.slice(0, MAX_ACTIONS),
    totalFindings: findings.length,
    shownFindings: Math.min(sorted.length, MAX_PROBLEMS),
    cookies: buildCookieInventory(cookies, untestedStates),
  };
}
