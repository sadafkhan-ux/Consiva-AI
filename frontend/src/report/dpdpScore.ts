import type { DpdpFindingEntry, DpdpGroupKey, DpdpLedgerMetric, DpdpNeed, DpdpScoreBlock, DpdpScoreResponse, DpdpStatus, SectorAssessment } from "../api/types";

/**
 * The ONE place the DPDP Compliance Score is turned into words. The dashboard section and the PDF both render this view, so
 * they cannot show different numbers or wording for the same scan. The numbers themselves are computed once, on the server
 * (backend app/scoring/dpdp_score.py); nothing here recalculates a score.
 */

export const STATUS_LABEL: Record<DpdpStatus, string> = {
  met: "Met",
  partially_met: "Partially Met",
  not_met: "Not Met",
  unable_to_verify: "Unable to Verify",
  not_applicable: "Not Applicable",
};

export interface ScoreRequirementView {
  id: string;
  category: string;
  title: string;
  provisions: string;
  weight: number;
  statusKey: DpdpStatus;
  status: string;
  /** "4 of 4 points", or "not scored". */
  points: string;
  inForce: string;
  /** "Readiness gap: applies from 13 May 2027" for a gap in a duty that is not yet in force; null otherwise. */
  timingNote: string | null;
  /** "Website check" (a scan can observe it) or "Business check" (needs internal evidence, information or legal review). */
  checkType: string;
  applicability: string;
  /** "High", "Medium" or "None": how far to trust the result. */
  confidence: string;
  scoringRationale: string;
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
  /** The check's verdict in the words of the score card table: Pass, Partial, Fail, Cannot Verify or N/A. */
  verdict: string;
  tone: "pass" | "partial" | "fail" | "unknown" | "na";
  /** What kind of legal basis the check has and how far a scan can speak to it. */
  legalNote: string;
  /** "none (not scored)" or "1 of 2 points lost": the weight the check did not earn. */
  penalty: string;
  /** What the check adds to the score, or why it adds nothing and what it does to coverage. */
  contribution: string;
  /** Why a check is Unable to Verify or Not Applicable; empty for an assessed check. */
  reason: string;
  /** The first observation, or the exclusion reason: the one-line evidence for the table. */
  evidenceLine: string;
  effective: string;
}

export interface ScoreCategoryView {
  name: string;
  score: string;
  coverage: string;
  weights: string;
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
  points: string;
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
  /** "Methodology v1.4.0 · documented default weights", or "· CUSTOM weights". */
  versionLine: string;
  custom: boolean;
  weightsNote: string;
  /** The formulas and rules, in words. */
  rules: string[];
  categories: { name: string; weight: number; share: string; rationale: string; requirements: string }[];
  weights: { id: string; title: string; category: string; type: string; weight: string; rationale: string; applicability: string; applies: string }[];
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
  /** What fixing this one gap would do to the readiness score, in words ("" when the score is withheld), from the score's own weights. */
  scoreLine: string;
  /** What the gap means for the organisation, in plain words ("" when none is stated for this check). */
  impact: string;
}

export interface ScoreView {
  /** The sector reading, passed through from the server (absent on an older one). */
  sector: SectorAssessment | null;
  title: string;
  /** "56.5%" or "Insufficient Evidence". Never a number when the score is withheld. */
  headline: string;
  /** The score as a number, straight from the server (null when withheld). The headline text, the progress ring and the PDF all use this one value. */
  scoreValue: number | null;
  /** Assessment coverage as a number, straight from the server (null when there is none). */
  coverageValue: number | null;
  scored: boolean;
  basisLine: string;
  coverageLine: string;
  unableLine: string | null;
  withheldReason: string | null;
  inForceLine: string;
  formulaLine: string;
  categories: ScoreCategoryView[];
  assessed: ScoreRequirementView[];
  unableToVerify: ScoreRequirementView[];
  excluded: ScoreRequirementView[];
  methodologyLine: string;
  disclaimer: string;
  outOfScope: string[];
  /** What was assessed and when: the main domain, the pages, the scan time and the consent states tested. */
  scopeLines: string[];
  scanTimeLine: string;
  /** The most pages one scan covers, as the server reported it (never a number typed into the report). */
  pageLimit: number;
  /** "21 Oct 2026, 06:41 UTC", or "not recorded". */
  scanTime: string;
  /** The score and coverage worked out with this scan's own numbers: "13 ÷ 23 × 100 = 56.5%". Null when there is nothing to work out. */
  scoreWorking: string | null;
  coverageWorking: string | null;
  /** "Scoring methodology v1.5.0 · Legal mapping v2026.10.1". */
  versionsLine: string;
  legalSources: string[];
  /** This is website readiness, not full legal compliance. */
  natureNote: string;
  /** Set when a shown score rests on little (few assessed requirements or low coverage). */
  caution: string | null;
  /** Prioritised remediation, in the order to do it. */
  remediationPlan: RemediationView[];
  remediationNote: string;
  /** Assessment coverage as its own figure: "62.2%" or "n/a". Never folded into the score. */
  coverageHeadline: string;
  /** Why the result is what it is, in numbers: the points, the coverage and what was left out. */
  explanationLines: string[];
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
  /** Every number on the score card with its numerator, denominator, formula and the checks behind it. */
  ledger: LedgerView;
  /** All checks in registry order: the table below the score card and the PDF appendix. */
  allChecks: ScoreRequirementView[];
}

export interface MetricView {
  name: string;
  numerator: string;
  denominator: string;
  result: string;
  meaning: string;
}

export interface LedgerView {
  counts: { label: string; value: string; note: string }[];
  weights: { label: string; value: string; note: string }[];
  metrics: MetricView[];
  categoryMetrics: MetricView[];
  statusRules: { label: string; rule: string; numerator: string; denominator: string; coverage: string; count: number; weight: number; checks: string }[];
  penaltyNote: string;
  securityNote: string;
  notApplicable: { id: string; reason: string }[];
  checkNote: string;
}

const pts = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1));
const pct = (n: number | null) => (n === null ? "n/a" : `${n}%`);

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
  return `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })}, ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" })} UTC`;
}

function blockLine(b: DpdpScoreBlock, asOf: string): string {
  if (b.status === "scored") return `In force on ${formatDate(asOf)}: ${pct(b.score_percent)} (coverage ${pct(b.coverage_percent)}).`;
  return `In force on ${formatDate(asOf)}: no score. ${b.insufficient_reason ?? ""}`.trim();
}

/** A response that is not a whole score (an older server, a failed or malformed reply) is treated as no score at all. */
export function isDpdpScore(x: unknown): x is DpdpScoreResponse {
  const s = x as Partial<DpdpScoreResponse> | null | undefined;
  return !!s && Array.isArray(s.requirements) && !!s.ledger && Array.isArray(s.ledger.metrics) && !!s.readiness && !!s.in_force && Array.isArray(s.readiness.categories)
    && !!s.evidence_summary && !!s.breakdown && !!s.status_counts && Array.isArray(s.breakdown.readiness_gaps)
    && !!s.human_review && Array.isArray(s.human_review.items) && !!s.finding_groups && Array.isArray(s.finding_groups.confirmed)
    && !!s.methodology && Array.isArray(s.methodology.weights) && !!s.assessment && Array.isArray(s.remediation_plan);
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

const VERDICT: Record<DpdpStatus, { verdict: string; tone: ScoreRequirementView["tone"] }> = {
  met: { verdict: "Pass", tone: "pass" },
  partially_met: { verdict: "Partial", tone: "partial" },
  not_met: { verdict: "Fail", tone: "fail" },
  unable_to_verify: { verdict: "Cannot Verify", tone: "unknown" },
  not_applicable: { verdict: "N/A", tone: "na" },
};

const metricView = (m: DpdpLedgerMetric): MetricView => ({
  name: m.name,
  numerator: pts(m.numerator),
  denominator: pts(m.denominator),
  result: m.percent === null ? "not shown" : `${m.percent}%`,
  meaning: m.meaning,
});

export function buildScoreView(score: DpdpScoreResponse): ScoreView {
  const r = score.readiness;
  const pending = score.requirements.filter((q) => q.counts_in_score && !q.in_force).map((q) => q.effective).sort();
  const notYetInForce = pending.length > 0;
  const view = (q: DpdpScoreResponse["requirements"][number]): ScoreRequirementView => ({
    id: q.id,
    category: q.category,
    title: q.title,
    provisions: q.provisions.join("; "),
    weight: q.weight,
    statusKey: q.status,
    status: STATUS_LABEL[q.status],
    points: q.counts_in_score ? `${pts(q.points_earned)} of ${q.weight} points` : "not scored",
    inForce: q.in_force ? "in force" : `in force from ${formatDate(q.effective)}`,
    timingNote: !q.in_force && (q.status === "not_met" || q.status === "partially_met")
      ? `Readiness gap: applies from ${formatDate(q.effective)}` : null,
    checkType: q.check_type === "website" ? "Website check" : "Business check",
    applicability: q.applicability,
    confidence: q.confidence === "none" ? "None" : q.confidence === "high" ? "High" : "Medium",
    scoringRationale: q.scoring_rationale,
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
    penalty: q.penalty_points === null || q.penalty_points === undefined
      ? "none: not scored" : `${pts(q.penalty_points)} of ${q.weight} points lost`,
    contribution: q.counts_in_score
      ? `${pts(q.points_earned)} of ${q.weight} points${q.score_contribution_percent === null ? "" : `, +${q.score_contribution_percent} score points`}`
      : q.status === "unable_to_verify"
        ? `0: not scored. Its ${q.weight} points stay in the coverage denominator${q.coverage_share_percent === null ? "" : ` (${q.coverage_share_percent}% of it)`}`
        : "0: left out of the score and of coverage",
    reason: q.counts_in_score ? "" : (q.status === "unable_to_verify" ? (q.observations[0] ?? q.sufficiency_reason) : (q.exclusion_reason ?? "")),
    evidenceLine: q.observations[0] ?? q.exclusion_reason ?? "No observation recorded.",
    effective: formatDate(q.effective),
  });

  const gap = (g: DpdpScoreResponse["breakdown"]["verified_gaps"][number]): GapView => ({
    id: g.requirement_id, title: g.title, provisions: g.provisions.join("; "), detail: g.what_the_scan_observed,
    pages: g.pages, evidenceIds: g.evidence_ids, rules: g.rule_ids, points: `${pts(g.points)} of ${g.weight} points`,
    status: STATUS_LABEL[g.status],
    timingNote: g.timing === "future" ? `Readiness gap: applies from ${formatDate(g.applies_from)}` : "Current gap: in force",
    whyItMatters: g.why_it_matters, action: g.recommended_action, verify: g.how_to_verify,
  });
  const item = (i: DpdpScoreResponse["human_review"]["items"][number]): ReviewItemView => ({
    id: i.id, kind: i.category, topic: i.topic, needs: i.needs.map((n) => NEED_LABEL[n] ?? n), known: i.known,
    missing: i.missing, decision: i.decision, provisions: i.provisions.join("; "), evidenceIds: i.evidence_ids,
  });
  const ev = score.evidence_summary;
  const bd = score.breakdown;
  const rows = score.requirements.map(view);
  const L = score.ledger;
  const unable = rows.filter((x) => x.statusKey === "unable_to_verify");
  return {
    sector: score.sector ?? null,
    title: "DPDP Compliance Score (proposed methodology)",
    headline: r.status === "scored" && r.score_percent !== null ? `${r.score_percent}%` : "Insufficient Evidence",
    scoreValue: r.status === "scored" ? r.score_percent : null,
    coverageValue: r.coverage_percent,
    scored: r.status === "scored",
    basisLine: notYetInForce
      ? `Readiness against DPDP obligations that are not yet in force (they start on ${formatDate(pending[pending.length - 1])}, 18 months after the Rules were published).`
      : "Against obligations that are in force on the assessment date.",
    coverageLine: `Assessment coverage ${pct(r.coverage_percent)}: ${r.assessed_weight} of ${r.applicable_weight} weighted points of applicable requirements were assessed.`,
    unableLine: unable.length
      ? `${unable.length} applicable requirement${unable.length === 1 ? "" : "s"} (${r.unable_to_verify_weight} points) could not be verified from a website scan. They are left out of the score and lower the coverage; they are never counted as met.`
      : null,
    withheldReason: r.status === "scored" ? null : r.insufficient_reason,
    inForceLine: blockLine(score.in_force, score.as_of),
    formulaLine: `Score = weighted points earned (${pts(r.points_earned)}) divided by weighted points assessed (${r.points_assessed}), times 100. Met = 100% of a requirement's weight, Partially Met = 50%, Not Met = 0%.`,
    categories: r.categories.map((c) => ({
      name: c.category,
      score: c.status === "scored" ? pct(c.score_percent) : "Not assessed",
      coverage: `coverage ${pct(c.coverage_percent)}`,
      weights: `${c.assessed_weight} of ${c.applicable_weight} points assessed`,
    })),
    assessed: rows.filter((x) => x.statusKey === "met" || x.statusKey === "partially_met" || x.statusKey === "not_met"),
    unableToVerify: unable,
    excluded: rows.filter((x) => x.statusKey === "not_applicable"),
    methodologyLine: `Methodology v${score.methodology_version} · assessed as of ${formatDate(score.as_of)}`,
    disclaimer: score.disclaimer,
    outOfScope: score.out_of_scope.map((o) => `${o.provision}: ${o.reason}`),
    breakdownLine: `${plural(bd.verified_gaps.length, "current gap", "current gaps")} · ${plural(bd.readiness_gaps.length, "readiness gap", "readiness gaps")} (duty not yet in force) · ${bd.partially_satisfied.length} partially satisfied · ${plural(bd.missing_evidence.length, "requirement", "requirements")} with missing evidence · ${bd.uncertain_count} uncertain, for review`,
    checkTypesLine: (() => {
      const live = score.requirements.filter((q) => q.status !== "not_applicable");
      const web = live.filter((q) => q.check_type === "website").length;
      const biz = live.filter((q) => q.check_type === "business").length;
      return `${plural(web, "website check", "website checks")} and ${plural(biz, "business check", "business checks")} apply. A scan can assess only the website checks; business checks need internal documents, business information, integrations or legal review, and are never counted as met.`;
    })(),
    statusCountsLine: (["met", "partially_met", "not_met", "unable_to_verify", "not_applicable"] as DpdpStatus[])
      .map((k) => `${STATUS_LABEL[k]} ${score.status_counts[k] ?? 0}`).join(" · "),
    verifiedGaps: bd.verified_gaps.map(gap),
    readinessGaps: bd.readiness_gaps.map(gap),
    partiallySatisfied: bd.partially_satisfied.map(gap),
    missingEvidence: bd.missing_evidence.map((m) => ({
      id: m.requirement_id, title: m.title, provisions: m.provisions.join("; "), reason: m.reason,
      kind: m.kind === "organisational" ? "needs organisational evidence" : "the scan could not observe it",
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
      `Scope: the entered main domain${score.assessment.main_domain ? ` (${score.assessment.main_domain})` : ""} only. ${plural(score.assessment.pages_scanned.length, "page", "pages")} scanned, of a limit of ${score.assessment.page_limit}.`,
      ...(score.assessment.pages_scanned.length ? [`Pages scanned: ${score.assessment.pages_scanned.join(", ")}`] : []),
      `Consent states tested: ${score.assessment.states_tested.length ? score.assessment.states_tested.join(", ") : "none"}${score.assessment.states_not_tested.length ? `. Not tested: ${score.assessment.states_not_tested.join(", ")}` : ""}.`,
    ],
    pageLimit: score.assessment.page_limit,
    scanTime: formatDateTimeUtc(score.assessment.scanned_at),
    scoreWorking: r.status === "scored" && r.score_percent !== null
      ? `${pts(r.points_earned)} ÷ ${r.points_assessed} × 100 = ${r.score_percent}%` : null,
    coverageWorking: r.coverage_percent === null ? null : `${r.assessed_weight} ÷ ${r.applicable_weight} × 100 = ${r.coverage_percent}%`,
    scanTimeLine: `Scan time: ${formatDateTimeUtc(score.assessment.scanned_at)} · assessed against effective dates on ${formatDate(score.assessment.assessed_on)}`,
    versionsLine: `Scoring methodology v${score.methodology.version} · Legal mapping v${score.methodology.legal_mapping_version}`,
    legalSources: score.methodology.legal_sources.map((l) => `${l.name}: ${l.reference}`),
    natureNote: score.assessment.nature_note,
    caution: r.caution,
    remediationPlan: score.remediation_plan.map((p) => ({
      rank: p.rank, title: `${p.requirement_id}  ${p.title}`, outcome: p.outcome, timing: p.timing, priority: p.priority,
      priorityReason: p.priority_reason, action: p.action, howToVerify: p.how_to_verify, legal: p.legal_references.join("; "),
      scoreLine: p.current_percent != null && p.percent_if_fixed != null && p.gain_percent != null
        ? `Fixing this alone would raise the readiness score from ${p.current_percent}% to ${p.percent_if_fixed}% (+${p.gain_percent} percentage points).` : "",
      impact: p.impact ?? "",
    })),
    remediationNote: "In the order to do them: duties already in force first, then Not Met before Partially Met, then the heavier documented weight.",
    coverageHeadline: r.coverage_percent === null ? "n/a" : `${r.coverage_percent}%`,
    explanationLines: [
      r.status === "scored"
        ? `The score is ${r.score_percent}% because ${pts(r.points_earned)} of ${r.points_assessed} weighted points were earned across ${plural(rows.filter((x) => x.statusKey === "met" || x.statusKey === "partially_met" || x.statusKey === "not_met").length, "assessed requirement", "assessed requirements")}.`
        : `No score is shown: ${r.insufficient_reason ?? "the evidence is insufficient."}`,
      ...(rows.filter((x) => x.statusKey === "met" || x.statusKey === "partially_met" || x.statusKey === "not_met").length
        ? [`Assessed: ${rows.filter((x) => x.statusKey === "met" || x.statusKey === "partially_met" || x.statusKey === "not_met")
          .map((x) => `${x.id} ${x.status} ${x.points.replace(" points", "")}`).join(" · ")}.`] : []),
      `Coverage is ${r.coverage_percent === null ? "n/a" : `${r.coverage_percent}%`} because ${r.assessed_weight} of ${r.applicable_weight} applicable weighted points could be assessed; ${r.unable_to_verify_weight} points were unverifiable and are not counted as met or as not met.`,
    ],
    methodology: {
      title: "Methodology, weights and calculation",
      versionLine: `Methodology v${score.methodology.version} · ${score.methodology.weights_source === "custom" ? "CUSTOM weights" : "documented default weights"}`,
      custom: score.methodology.weights_source === "custom",
      weightsNote: score.methodology.weights_note,
      rules: [...score.methodology.points_rules,
        score.methodology.reliability_rule, score.methodology.duplicate_rule, score.methodology.timing_rule, score.methodology.scope_rule],
      categories: score.methodology.categories.map((c) => ({
        name: c.name, weight: c.weight, share: c.share_percent === null ? "n/a" : `${c.share_percent}%`,
        rationale: c.rationale, requirements: c.requirement_ids.join(", "),
      })),
      weights: score.methodology.weights.map((w) => ({
        id: w.id, title: w.title, category: w.category, type: w.check_type === "website" ? "Website check" : "Business check",
        weight: w.weight === w.default_weight ? String(w.weight) : `${w.weight} (default ${w.default_weight})`,
        rationale: w.rationale, applicability: w.applicability, applies: w.applies_now ? "applies" : "not applicable here",
      })),
      dedup: score.methodology.dedup.map((d) => `${d.requirement_ids.join(" and ")}: ${d.explanation}`),
    },
    groups: GROUP_ORDER.map((key) => ({
      key,
      title: GROUP_TITLE[key],
      intro: GROUP_INTRO[key],
      count: score.finding_groups[key].length,
      note: score.finding_groups.notes[key],
      entries: score.finding_groups[key].map(entryView),
    })),
    groupCountsLine: GROUP_ORDER.map((k) => `${GROUP_TITLE[k]} ${score.finding_groups[k].length}`).join(" · "),
    upcomingLines: score.finding_groups.upcoming.map(
      (u) => `${formatDate(u.date)}: ${plural(u.requirement_ids.length, "duty starts", "duties start")} (${u.requirement_ids.join(", ")})`),
    reviewTitle: "Logic Required / Human Review",
    reviewStatement: score.human_review.statement,
    reviewUncertain: score.human_review.items.filter((i) => i.category === "uncertain").map(item),
    reviewMissing: score.human_review.items.filter((i) => i.category === "missing_evidence").map(item),
    allChecks: rows,
    ledger: {
      counts: [
        { label: "Checks defined", value: String(L.total_checks), note: "Every check the system has, applicable or not." },
        { label: "Applicable checks", value: String(L.applicable_checks), note: "Checks that apply to this site." },
        { label: "Assessed checks", value: String(L.assessed_checks), note: `${L.passed_checks} pass, ${L.partial_checks} partial, ${L.failed_checks} fail. These are the checks in the score.` },
        { label: "Cannot verify", value: String(L.unable_checks), note: "Applicable, but a scan could not obtain the evidence. Not in the score." },
        { label: "Not applicable", value: String(L.not_applicable_checks), note: "Excluded under the applicability rule, with a reason on each check." },
      ],
      weights: [
        { label: "Total weight, all checks", value: String(L.total_weight), note: "The combined weight of every check." },
        { label: "Applicable weight", value: String(L.applicable_weight), note: "The combined weight of the checks that apply. Coverage is measured against it." },
        { label: "Assessed weight", value: String(L.assessed_weight), note: "The combined weight of the checks that were scored." },
        { label: "Earned weight", value: pts(L.earned_weight), note: "The weight earned under the rules: Met 100%, Partially Met 50%, Not Met 0%." },
        { label: "Penalty weight", value: pts(L.penalty_weight), note: "The weight the assessed checks did not earn." },
        { label: "Weight not verified", value: String(L.unable_weight), note: "Weight of the applicable checks a scan could not test. It lowers coverage and never raises or lowers the score." },
      ],
      metrics: L.metrics.map(metricView),
      categoryMetrics: L.category_metrics.map(metricView),
      statusRules: L.status_rules.map((r) => ({
        label: r.label, rule: r.rule, numerator: r.in_numerator, denominator: r.in_denominator, coverage: r.on_coverage,
        count: r.count, weight: r.weight, checks: r.check_ids.join(", ") || "none",
      })),
      penaltyNote: L.penalty_note,
      securityNote: L.security_score_note,
      notApplicable: L.not_applicable.map((n) => ({ id: n.id, reason: n.reason })),
      checkNote: "Each check's contribution is rounded to one decimal, so the contributions can differ from the score by a few tenths.",
    },
  };
}
