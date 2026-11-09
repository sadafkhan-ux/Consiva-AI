import type { DpdpFindingEntry, DpdpGroupKey, DpdpNeed, DpdpRequirementsResponse, DpdpStatus, SectorAssessment } from "../api/types";
import { assessedCategories } from "./reportScope";

/**
 * The ONE place the DPDP requirements reading is turned into words. The dashboard section and the PDF both render this view. It holds statuses and evidence only: there is no
 * numeric rating of any kind in it. The only score in the application is the ten-check assessment (report/tenChecks.ts).
 */

export const STATUS_LABEL: Record<DpdpStatus, string> = {
  met: "Met",
  partially_met: "Partially Met",
  not_met: "Not Met",
  unable_to_verify: "Unable to Verify",
  not_applicable: "Not Applicable",
};

export interface RequirementView {
  id: string;
  category: string;
  title: string;
  provisions: string;
  statusKey: DpdpStatus;
  status: string;
  inForce: string;
  /** "Readiness gap: applies from 13 May 2027" for a gap in a duty that is not yet in force; null otherwise. */
  timingNote: string | null;
  /** "Website check" (a scan can observe it) or "Business check" (needs internal evidence, information or legal review). */
  checkType: string;
  applicability: string;
  /** "High", "Medium" or "None": how far to trust the result. */
  confidence: string;
  description: string;
  criteria: string;
  expectedEvidence: string;
  observations: string[];
  pages: string[];
  /** "Sufficient: ...", "Not sufficient: ...", or null when it does not apply. */
  sufficiency: string | null;
  limitation: string;
  evidenceIds: string[];
  rules: string[];
  note: string | null;
  /** The check's verdict in the words of the score card table: Pass, Partial, Fail, Unable to Verify or N/A. */
  verdict: string;
  tone: "pass" | "partial" | "fail" | "unknown" | "na";
  /** What kind of legal basis the check has and how far a scan can speak to it. */
  legalNote: string;
  /** Why a check is Unable to Verify or Not Applicable; empty for an assessed check. */
  reason: string;
  /** The first observation, or the exclusion reason: the one-line evidence for the table. */
  evidenceLine: string;
  /** How the check was (or could be) answered: the website scan, the privacy policy text, or only the organisation's own records. */
  methodKey: "website_scan" | "policy_text" | "organisation_records";
  methodLabel: string;
  /** The records that would settle a check left unverified; empty for a tested check. */
  neededEvidence: string[];
  effective: string;
}

export const NEED_LABEL: Record<DpdpNeed, string> = {
  business_context: "Business context",
  legal_interpretation: "Legal interpretation",
  more_evidence: "More evidence",
};

export interface ReviewItemView {
  id: string;
  kind: "uncertain" | "missing_evidence";
  topic: string;
  needs: string[];
  known: string[];
  missing: string[];
  decision: string;
  provisions: string;
  evidenceIds: string[];
}

export interface GapView {
  id: string;
  title: string;
  provisions: string;
  /** What the scan observed. */
  detail: string[];
  pages: string[];
  evidenceIds: string[];
  rules: string[];
  status: string;
  /** "Readiness gap: applies from 13 May 2027" or "Current gap: in force". */
  timingNote: string;
  whyItMatters: string;
  action: string;
  verify: string;
}

export const GROUP_TITLE: Record<DpdpGroupKey, string> = {
  confirmed: "Confirmed Findings",
  needs_review: "Needs Human Review",
  unable_to_verify: "Unable to Verify",
  future_readiness: "Future Readiness",
};
export const GROUP_ORDER: DpdpGroupKey[] = ["confirmed", "needs_review", "unable_to_verify", "future_readiness"];
export const GROUP_INTRO: Record<DpdpGroupKey, string> = {
  confirmed: "A check in a duty that is in force failed, with sufficient evidence.",
  needs_review: "The evidence is ambiguous, or business context or legal interpretation is needed.",
  unable_to_verify: "The scan lacks the evidence. Neither a pass nor a failure.",
  future_readiness: "Duties that start later. Not current violations.",
};

export interface FindingView {
  id: string;
  title: string;
  /** "Not Met", "Partially Met", "Needs review" or "Unable to Verify". */
  outcome: string;
  /** "Severity: High", or "Severity: not rated". */
  severity: string;
  severityNote: string;
  legal: string;
  pages: string[];
  consentState: string;
  observed: string[];
  evidenceIds: string[];
  rules: string[];
  timingNote: string;
  businessImpact: string;
  explanation: string;
  whatToReview: string[];
  missing: string[];
  howToVerify: string;
  remediation: string;
  caveat: string;
  related: string;
}

export interface FindingGroupView {
  key: DpdpGroupKey;
  title: string;
  intro: string;
  count: number;
  note: string;
  entries: FindingView[];
}

export interface MethodologyView {
  title: string;
  /** The rules in words: duplicates, timing, scope. */
  rules: string[];
  dedup: string[];
}

export interface RemediationView {
  rank: number;
  title: string;
  outcome: string;
  timing: string;
  priority: string;
  priorityReason: string;
  action: string;
  howToVerify: string;
  legal: string;
  /** What the gap means for the organisation, in plain words ("" when none is stated for this check). */
  impact: string;
}

export interface RequirementsView {
  /** The sector reading, passed through from the server (absent on an older one). */
  sector: SectorAssessment | null;
  title: string;
  basisLine: string;
  unableLine: string | null;
  disclaimer: string;
  outOfScope: string[];
  /** What was assessed and when: the main domain, the pages, the scan time and the consent states tested. */
  scopeLines: string[];
  scanTimeLine: string;
  /** The most pages one scan covers, as the server reported it (never a number typed into the report). */
  pageLimit: number;
  /** "21 Oct 2026, 06:41 UTC", or "not recorded". */
  scanTime: string;
  /** "Legal mapping v2026.10.1". */
  versionsLine: string;
  legalSources: string[];
  /** This is website readiness, not full legal compliance. */
  natureNote: string;
  assessed: RequirementView[];
  unableToVerify: RequirementView[];
  excluded: RequirementView[];
  /** Prioritised remediation, in the order to do it. */
  remediationPlan: RemediationView[];
  remediationNote: string;
  methodology: MethodologyView;
  /** Verified gaps, partially satisfied, missing evidence and uncertain, as one line. */
  breakdownLine: string;
  /** Met 3 · Partially Met 1 · Not Met 2 · Unable to Verify 5 · Not Applicable 5. */
  statusCountsLine: string;
  /** "7 website checks and 5 business checks apply; a scan can assess only the website checks." */
  checkTypesLine: string;
  verifiedGaps: GapView[];
  readinessGaps: GapView[];
  partiallySatisfied: GapView[];
  missingEvidence: { id: string; title: string; provisions: string; reason: string; kind: string }[];
  /** The three technical signals, counted apart, and why none of them is a finding on its own. */
  evidenceLines: string[];
  evidenceNote: string;
  /** The four finding groups, in order: Confirmed Findings, Needs Human Review, Unable to Verify, Future Readiness. */
  groups: FindingGroupView[];
  groupCountsLine: string;
  /** "13 May 2027: 10 duties start (DPDP-C1, ...)". */
  upcomingLines: string[];
  /** The "Logic Required / Human Review" box. */
  reviewTitle: string;
  reviewStatement: string;
  reviewUncertain: ReviewItemView[];
  reviewMissing: ReviewItemView[];
  /** Plain counts of checks by status. */
  ledger: LedgerView;
  /** All checks in registry order: the table on the dashboard and the PDF appendix. */
  allChecks: RequirementView[];
  /** The categories with at least one assessed check: a category the report normally leaves out is shown once it has one. */
  assessedCategories: ReadonlySet<string>;
}

export interface LedgerView {
  counts: { label: string; value: string; note: string }[];
  notApplicable: { id: string; reason: string }[];
}

function formatDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
}

function formatDateTimeUtc(iso: string | null): string {
  if (!iso) return "not recorded";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" })}, ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" })} IST`;
}

/** A response that is not a whole requirements reading (an older server, a failed or malformed reply) is treated as none at all. */
export function isRequirements(x: unknown): x is DpdpRequirementsResponse {
  const s = x as Partial<DpdpRequirementsResponse> | null | undefined;
  return !!s && Array.isArray(s.requirements) && !!s.ledger && Array.isArray(s.ledger.status_rules)
    && !!s.evidence_summary && !!s.breakdown && !!s.status_counts && Array.isArray(s.breakdown.readiness_gaps)
    && !!s.human_review && Array.isArray(s.human_review.items) && !!s.finding_groups && Array.isArray(s.finding_groups.confirmed)
    && !!s.methodology && !!s.assessment && Array.isArray(s.remediation_plan);
}

const entryView = (e: DpdpFindingEntry): FindingView => ({
  id: e.id,
  title: e.title,
  outcome: e.outcome,
  severity: e.severity ? `Severity: ${e.severity}` : "Severity: not rated",
  severityNote: e.severity_note,
  legal: e.legal_references.join("; "),
  pages: e.pages,
  consentState: e.consent_states.length ? e.consent_states.join(", ") : "Not specific to a consent state",
  observed: e.observed,
  evidenceIds: e.evidence_ids,
  rules: e.rule_ids,
  timingNote: e.timing_note,
  businessImpact: e.business_impact,
  explanation: e.explanation,
  whatToReview: e.what_to_review,
  missing: e.missing,
  howToVerify: e.how_to_verify,
  remediation: e.remediation,
  caveat: e.signal_caveat,
  related: e.related_ids.join(", "),
});

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
const inState = (c: { pre_consent: number; post_accept: number; post_reject: number }) =>
  `before consent ${c.pre_consent} · after Accept ${c.post_accept} · after Reject ${c.post_reject}`;

const VERDICT: Record<DpdpStatus, { verdict: string; tone: RequirementView["tone"] }> = {
  met: { verdict: "Pass", tone: "pass" },
  partially_met: { verdict: "Partial", tone: "partial" },
  not_met: { verdict: "Fail", tone: "fail" },
  unable_to_verify: { verdict: "Unable to Verify", tone: "unknown" },
  not_applicable: { verdict: "N/A", tone: "na" },
};

const METHOD_LABEL = { website_scan: "Website scan", policy_text: "Privacy policy text", organisation_records: "Organisation's records" } as const;
type MethodKey = keyof typeof METHOD_LABEL;

/** How a check is answered. A score from an older server names none, so the check type decides (a website check, or one that needs the organisation's records). */
function methodOf(q: { method?: string; check_type: string }): MethodKey {
  if (q.method === "website_scan" || q.method === "policy_text" || q.method === "organisation_records") return q.method;
  return q.check_type === "website" ? "website_scan" : "organisation_records";
}

export function buildRequirementsView(reading: DpdpRequirementsResponse): RequirementsView {
  const pending = reading.requirements.filter((q) => q.status !== "not_applicable" && !q.in_force).map((q) => q.effective).sort();
  const notYetInForce = pending.length > 0;
  const view = (q: DpdpRequirementsResponse["requirements"][number]): RequirementView => ({
    id: q.id,
    category: q.category,
    title: q.title,
    provisions: q.provisions.join("; "),
    statusKey: q.status,
    status: STATUS_LABEL[q.status],
    inForce: q.in_force ? "in force" : `in force from ${formatDate(q.effective)}`,
    timingNote: !q.in_force && (q.status === "not_met" || q.status === "partially_met")
      ? `Readiness gap: applies from ${formatDate(q.effective)}` : null,
    checkType: methodOf(q) === "website_scan" ? "Website check" : methodOf(q) === "policy_text" ? "Policy check" : "Business check",
    applicability: q.applicability,
    confidence: q.confidence === "none" ? "None" : q.confidence === "high" ? "High" : "Medium",
    description: q.description,
    criteria: q.criteria,
    expectedEvidence: q.expected_evidence,
    pages: q.pages,
    sufficiency: q.sufficient === null ? null : `${q.sufficient ? "Sufficient" : "Not sufficient"}: ${q.sufficiency_reason}`,
    limitation: q.limitation,
    observations: q.observations,
    evidenceIds: q.evidence_ids,
    rules: q.rule_ids,
    note: q.exclusion_reason,
    verdict: VERDICT[q.status].verdict,
    tone: VERDICT[q.status].tone,
    legalNote: q.legal_note,
    reason: q.status === "unable_to_verify" ? (q.observations[0] ?? q.sufficiency_reason) : q.status === "not_applicable" ? (q.exclusion_reason ?? "") : "",
    evidenceLine: q.observations[0] ?? q.exclusion_reason ?? "No observation recorded.",
    methodKey: methodOf(q),
    methodLabel: METHOD_LABEL[methodOf(q)],
    neededEvidence: q.needed_evidence ?? [],
    effective: formatDate(q.effective),
  });

  const gap = (g: DpdpRequirementsResponse["breakdown"]["verified_gaps"][number]): GapView => ({
    id: g.requirement_id, title: g.title, provisions: g.provisions.join("; "), detail: g.what_the_scan_observed,
    pages: g.pages, evidenceIds: g.evidence_ids, rules: g.rule_ids,
    status: STATUS_LABEL[g.status],
    timingNote: g.timing === "future" ? `Readiness gap: applies from ${formatDate(g.applies_from)}` : "Current gap: in force",
    whyItMatters: g.why_it_matters, action: g.recommended_action, verify: g.how_to_verify,
  });
  const item = (i: DpdpRequirementsResponse["human_review"]["items"][number]): ReviewItemView => ({
    id: i.id, kind: i.category, topic: i.topic, needs: i.needs.map((n) => NEED_LABEL[n] ?? n), known: i.known,
    missing: i.missing, decision: i.decision, provisions: i.provisions.join("; "), evidenceIds: i.evidence_ids,
  });
  const ev = reading.evidence_summary;
  const bd = reading.breakdown;
  const rows = reading.requirements.map(view);
  const L = reading.ledger;
  const unable = rows.filter((x) => x.statusKey === "unable_to_verify");
  const m = reading.methodology;
  return {
    sector: reading.sector ?? null,
    title: "DPDP requirements reading",
    basisLine: notYetInForce
      ? `Readiness against DPDP obligations that are not yet in force (they start on ${formatDate(pending[pending.length - 1])}, 18 months after the Rules were published).`
      : "Against obligations that are in force on the assessment date.",
    unableLine: unable.length
      ? `${unable.length} applicable requirement${unable.length === 1 ? "" : "s"} could not be verified from a website scan. They are never counted as met.`
      : null,
    assessed: rows.filter((x) => x.statusKey === "met" || x.statusKey === "partially_met" || x.statusKey === "not_met"),
    unableToVerify: unable,
    excluded: rows.filter((x) => x.statusKey === "not_applicable"),
    disclaimer: reading.disclaimer,
    outOfScope: reading.out_of_scope.map((o) => `${o.provision}: ${o.reason}`),
    breakdownLine: `${plural(bd.verified_gaps.length, "current gap", "current gaps")} · ${plural(bd.readiness_gaps.length, "readiness gap", "readiness gaps")} (duty not yet in force) · ${bd.partially_satisfied.length} partially satisfied · ${plural(bd.missing_evidence.length, "requirement", "requirements")} with missing evidence · ${bd.uncertain_count} uncertain, for review`,
    checkTypesLine: (() => {
      const live = reading.requirements.filter((q) => q.status !== "not_applicable");
      const web = live.filter((q) => methodOf(q) === "website_scan").length;
      const policy = live.filter((q) => methodOf(q) === "policy_text").length;
      const biz = live.filter((q) => methodOf(q) === "organisation_records").length;
      if (policy > 0) {
        return `${plural(web, "website check", "website checks")}, ${plural(policy, "policy check", "policy checks")} and ${plural(biz, "business check", "business checks")} apply. A scan assesses website checks by observing the site and policy checks from passages quoted word for word from the privacy policy; business checks need internal documents, business information, integrations or legal review. A check that could not be tested is never counted as met or as failed.`;
      }
      return `${plural(web, "website check", "website checks")} and ${plural(biz, "business check", "business checks")} apply. A scan can assess only the website checks; business checks need internal documents, business information, integrations or legal review, and are never counted as met.`;
    })(),
    statusCountsLine: (["met", "partially_met", "not_met", "unable_to_verify", "not_applicable"] as DpdpStatus[])
      .map((k) => `${STATUS_LABEL[k]} ${reading.status_counts[k] ?? 0}`).join(" · "),
    verifiedGaps: bd.verified_gaps.map(gap),
    readinessGaps: bd.readiness_gaps.map(gap),
    partiallySatisfied: bd.partially_satisfied.map(gap),
    missingEvidence: bd.missing_evidence.map((x) => ({
      id: x.requirement_id, title: x.title, provisions: x.provisions.join("; "), reason: x.reason,
      kind: x.kind === "organisational" ? "needs organisational evidence" : "the scan could not observe it",
    })),
    evidenceLines: [
      `Third-party network requests: ${ev.network_requests} (${inState(ev.network_requests_by_state)})`,
      `Requests matching known analytics or advertising services: ${ev.known_tracker_requests} (${inState(ev.known_tracker_requests_by_state)}); execution not confirmed`,
      `Requests that matched no known service: ${ev.unclassified_requests}`,
      `Cookies: ${ev.cookies} (${inState(ev.cookies_by_state)}); ${ev.known_tracker_cookies} matched a known analytics or advertising service`,
      `Pages of the main domain scanned: ${ev.pages_scanned}`,
    ],
    evidenceNote: ev.note,
    scopeLines: [
      `Scope: the entered main domain${reading.assessment.main_domain ? ` (${reading.assessment.main_domain})` : ""} only. ${plural(reading.assessment.pages_scanned.length, "page", "pages")} scanned, of a limit of ${reading.assessment.page_limit}.`,
      ...(reading.assessment.pages_scanned.length ? [`Pages scanned: ${reading.assessment.pages_scanned.join(", ")}`] : []),
      `Consent states tested: ${reading.assessment.states_tested.length ? reading.assessment.states_tested.join(", ") : "none"}${reading.assessment.states_not_tested.length ? `. Not tested: ${reading.assessment.states_not_tested.join(", ")}` : ""}.`,
    ],
    pageLimit: reading.assessment.page_limit,
    scanTime: formatDateTimeUtc(reading.assessment.scanned_at),
    scanTimeLine: `Scan time: ${formatDateTimeUtc(reading.assessment.scanned_at)} · assessed against effective dates on ${formatDate(reading.assessment.assessed_on)}`,
    versionsLine: `Legal mapping v${m.legal_mapping_version}`,
    legalSources: m.legal_sources.map((l) => `${l.name}: ${l.reference}`),
    natureNote: reading.assessment.nature_note,
    remediationPlan: reading.remediation_plan.map((p) => ({
      rank: p.rank, title: `${p.requirement_id}  ${p.title}`, outcome: p.outcome, timing: p.timing, priority: p.priority,
      priorityReason: p.priority_reason, action: p.action, howToVerify: p.how_to_verify, legal: p.legal_references.join("; "),
      impact: p.impact ?? "",
    })),
    remediationNote: "In the order to do them: duties already in force first, then Not Met before Partially Met, then by the severity each finding is rated.",
    methodology: {
      title: "Rules and legal sources",
      rules: [m.duplicate_rule, m.timing_rule, m.scope_rule].filter((x): x is string => !!x),
      dedup: m.dedup.map((d) => `${d.requirement_ids.join(" and ")}: ${d.explanation}`),
    },
    groups: GROUP_ORDER.map((key) => ({
      key,
      title: GROUP_TITLE[key],
      intro: GROUP_INTRO[key],
      count: reading.finding_groups[key].length,
      note: reading.finding_groups.notes[key],
      entries: reading.finding_groups[key].map(entryView),
    })),
    groupCountsLine: GROUP_ORDER.map((k) => `${GROUP_TITLE[k]} ${reading.finding_groups[k].length}`).join(" · "),
    upcomingLines: reading.finding_groups.upcoming.map(
      (u) => `${formatDate(u.date)}: ${plural(u.requirement_ids.length, "duty starts", "duties start")} (${u.requirement_ids.join(", ")})`),
    reviewTitle: "Logic Required / Human Review",
    reviewStatement: reading.human_review.statement,
    reviewUncertain: reading.human_review.items.filter((i) => i.category === "uncertain").map(item),
    reviewMissing: reading.human_review.items.filter((i) => i.category === "missing_evidence").map(item),
    allChecks: rows,
    assessedCategories: assessedCategories(rows),
    ledger: {
      counts: [
        { label: "Checks defined", value: String(L.total_checks), note: "Every check the system has, applicable or not." },
        { label: "Applicable checks", value: String(L.applicable_checks), note: "Checks that apply to this site." },
        { label: "Assessed checks", value: String(L.assessed_checks), note: `${L.passed_checks} met, ${L.partial_checks} partly met, ${L.failed_checks} not met.` },
        { label: "Unable to verify", value: String(L.unable_checks), note: "Applicable, but a scan could not obtain the evidence. Never counted as met." },
        { label: "Not applicable", value: String(L.not_applicable_checks), note: "Excluded under the applicability rule, with a reason on each check." },
      ],
      notApplicable: L.not_applicable.map((n) => ({ id: n.id, reason: n.reason })),
    },
  };
}
