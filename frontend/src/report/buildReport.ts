import type { FindingResponse, ScanStatusResponse, WebsiteScanMetadata } from "../api/types";

/** Risk ordering, worst first -- used both to pick the overall band and to decide which
 *  findings earn a place on a deliberately one-page summary. */
const RISK_ORDER = ["critical", "high", "medium", "low", "info"] as const;

function riskRank(level: string): number {
  const i = RISK_ORDER.indexOf(level.toLowerCase() as (typeof RISK_ORDER)[number]);
  return i === -1 ? RISK_ORDER.length : i;
}

export interface ReportModel {
  siteUrl: string;
  generatedAt: string;
  overallRisk: string;
  /** True while findings still carry status "pending" -- the report is a draft until a
   *  human has actually signed off, and must say so rather than implying sign-off. */
  provisional: boolean;
  headline: string;
  counts: { label: string; value: string }[];
  problems: { risk: string; text: string }[];
  consentIssues: string[];
  dpdpRefs: string[];
  actions: string[];
  totalFindings: number;
  shownFindings: number;
}

/** Everything the one-page PDF needs, derived from data the app already has in state.
 *  Pure and side-effect free so it can be unit-tested and reasoned about separately
 *  from the rendering. */
export function buildReportModel(
  scan: ScanStatusResponse | null,
  findings: FindingResponse[],
  meta: WebsiteScanMetadata | null,
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
      { label: "Trackers", value: String(c.trackers) },
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
      consentIssues.push("No consent banner or CMP was detected on the site.");
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
    if (meta.reject_interaction === "cmp_not_found") {
      consentIssues.push("No 'Reject' control was found — consent may not be as easy to refuse as to give.");
    } else if (meta.reject_interaction === "click_failed") {
      consentIssues.push("A 'Reject' control was found but could not be exercised during the scan.");
    }
    if (meta.accept_interaction === "click_failed") {
      consentIssues.push("An 'Accept' control was found but could not be exercised during the scan.");
    }

    // The strongest single signal in this report: trackers already firing before any
    // consent decision was made. Taken from the scanner's own per-state counts.
    const preConsent = meta.trackers_by_consent_state?.pre_consent ?? 0;
    if (preConsent > 0) {
      consentIssues.push(
        `${preConsent} tracker${preConsent === 1 ? "" : "s"} fired before any consent was given.`,
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

  const headline = sorted.length
    ? `${sorted.length} compliance ${sorted.length === 1 ? "issue" : "issues"} identified; highest severity is ${overallRisk.toUpperCase()}.`
    : "No compliance issues were identified from the collected evidence.";

  return {
    siteUrl: scan?.url ?? "(unknown site)",
    generatedAt: new Date().toLocaleString(),
    overallRisk,
    provisional,
    headline,
    counts,
    problems: sorted.slice(0, MAX_PROBLEMS).map((f) => ({ risk: f.risk_level, text: f.finding_text })),
    consentIssues: consentIssues.slice(0, 4),
    dpdpRefs: [...refSet].slice(0, 5),
    actions: actions.slice(0, MAX_ACTIONS),
    totalFindings: findings.length,
    shownFindings: Math.min(sorted.length, MAX_PROBLEMS),
  };
}
