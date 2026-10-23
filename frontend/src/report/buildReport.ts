import type {
  EvidenceCookieItem, EvidenceTrackerItem, FindingResponse, PolicyAnalysis, ScanEvidenceResponse, ScanScreenshot, ScanStatusResponse, WebsiteScanMetadata,
} from "../api/types";
import type { ScoreView } from "./dpdpScore";
import { buildInsightSections, type InsightSection } from "./insights";
import { buildReadinessReview, type ReadinessReview } from "./readiness";
import { summariseTrackers } from "./trackerSummary";
import { buildCookieInventory, type CookieInventory } from "./cookieInventory";
import { buildScanPages, type ScanPagesView } from "./scanPages";
import { buildReportVisual, type ReportVisual } from "./reportVisual";

/** Risk ordering, worst first -- used both to pick the overall band and to decide which
 *  findings earn a place on a deliberately one-page summary. */
const RISK_ORDER = ["critical", "high", "medium", "low", "info"] as const;

function riskRank(level: string): number {
  const i = RISK_ORDER.indexOf(level.toLowerCase() as (typeof RISK_ORDER)[number]);
  return i === -1 ? RISK_ORDER.length : i;
}

/** Said on the score card and in the PDF whenever the analysis step did not complete. */
export const ANALYSIS_INCOMPLETE_NOTE =
  "The analysis did not complete for this scan, so there are no explanations or citations. The score and the checks come from the scan evidence and do not depend on it.";

export interface ReportModel {
  siteUrl: string;
  generatedAt: string;
  /** True while findings still carry status "pending" -- the report is a draft until a
   *  human has actually signed off, and must say so rather than implying sign-off. */
  provisional: boolean;
  headline: string;
  /** The proposed DPDP Compliance Score, as the dashboard shows it (report/dpdpScore.ts). Null when it was not loaded. */
  dpdpScore: ScoreView | null;
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
  /** Counts per consent state, technologies and privacy elements for the visual report. Null when the evidence was not loaded. */
  visual: ReportVisual | null;
  /** The pages this scan visited and how each one went, for the "Website Pages Scanned" section. Null when neither the page records nor the scan summary were loaded. */
  scanPages: ScanPagesView | null;
  /** Set when the analysis step did not complete, so the report says so beside the score. */
  analysisNote: string | null;
  /** The scan run this report describes: the same one the score card and the page list come from. */
  scanId: string | null;
  /** The scan's complete evidence (forms with every field, banner details, cookies, requests, policies, services), for the appendix. Null when it was not loaded. */
  evidence: ScanEvidenceResponse | null;
  /** What the site's policies say, for the appendix (the same object the dashboard section shows). Null when it was not loaded or not run. */
  policyAnalysis: PolicyAnalysis | null;
  /** The scanned homepage's preview, the same object the dashboard shows. Null when it was not loaded. */
  screenshot: ScanScreenshot | null;
  /** The dedicated sections beside the score (grievance officer, DPO, Indian-language support, children's data, security signals): the same ones the dashboard shows. */
  insights: InsightSection[];
  /** The five-page DPDP readiness review: the same one the dashboard shows. */
  readiness: ReadinessReview;
}

/** Everything the one-page PDF needs, derived from data the app already has in state.
 *  Pure and side-effect free so it can be unit-tested and reasoned about separately
 *  from the rendering. */
/** The consent states in which nothing was clicked. What the scan recorded for them is the same page seen again, not what happens after a choice. */
export function untestedConsentStates(meta: WebsiteScanMetadata | null | undefined): string[] {
  const clicked = (o: string | null | undefined) => o === "clicked";
  return [...(meta && !clicked(meta.accept_interaction) ? ["post_accept"] : []), ...(meta && !clicked(meta.reject_interaction) ? ["post_reject"] : [])];
}

export function buildReportModel(
  scan: ScanStatusResponse | null,
  findings: FindingResponse[],
  meta: WebsiteScanMetadata | null,
  cookies: EvidenceCookieItem[] = [],
  trackers: EvidenceTrackerItem[] = [],
  dpdpScore: ScoreView | null = null,
  evidence: ScanEvidenceResponse | null = null,
  analysisNote: string | null = null,
  policyAnalysis: PolicyAnalysis | null = null,
  screenshot: ScanScreenshot | null = null,
): ReportModel {
  const sorted = [...findings].sort((a, b) => riskRank(a.risk_level) - riskRank(b.risk_level));
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
  const untestedStates = untestedConsentStates(meta);

  const headline = sorted.length
    ? `${sorted.length} ${sorted.length === 1 ? "finding" : "findings"} to review.`
    : allStatesTested
      ? "No issues were observed in the tested scope (all three consent states were tested)."
      : "No findings were produced, but not every consent state could be tested: this is not a clean result.";

  return {
    siteUrl: scan?.url ?? "(unknown site)",
    generatedAt: new Date().toLocaleString(),
    provisional,
    headline,
    scope,
    dpdpScore,
    counts,
    problems: sorted.slice(0, MAX_PROBLEMS).map((f) => ({ risk: f.risk_level, text: f.finding_text })),
    consentIssues: consentIssues.slice(0, 4),
    dpdpRefs: [...refSet].slice(0, 5),
    actions: actions.slice(0, MAX_ACTIONS),
    totalFindings: findings.length,
    shownFindings: Math.min(sorted.length, MAX_PROBLEMS),
    cookies: buildCookieInventory(cookies.length ? cookies : evidence?.cookies ?? [], untestedStates),
    visual: buildReportVisual(evidence, meta, dpdpScore, untestedStates),
    scanPages: buildScanPages(evidence?.pages, meta, scan?.url ?? "", dpdpScore?.pageLimit ?? null),
    scanId: scan?.id ?? null,
    analysisNote,
    evidence,
    policyAnalysis: policyAnalysis && policyAnalysis.status !== "not_run" ? policyAnalysis : null,
    screenshot,
    insights: buildInsightSections({ policy: policyAnalysis, meta, evidence, sector: dpdpScore?.sector ?? null }),
    readiness: buildReadinessReview({ siteUrl: scan?.url ?? "", evidence, meta, policy: policyAnalysis, sector: dpdpScore?.sector ?? null }),
  };
}
