import type { PolicyAnalysis, PolicyRequirementResult } from "../api/types";
import type { ReportModel } from "./buildReport";

/**
 * The ten predefined checks every scanned website is assessed on, and the one 10-mark score they give. Every check is assessed on its own evidence: the missing of a privacy policy page
 * fails check 1 and nothing else stops. There is no "Unable to Verify" result: a check whose evidence could not be established is Failed, with the reason and the limit stated, and the wording
 * never says the law was broken (a missing disclosure is "not evidenced", not a violation). Pure: it reads the report model and invents nothing.
 */
export type TenResult = "Passed" | "Partially Passed" | "Failed" | "Not Applicable";

export interface TenCheck {
  id: number;
  name: string;
  result: TenResult;
  /** 1, 0.5 or 0; null when the check does not apply. */
  awarded: number | null;
  /** 1, or null when the check does not apply (it is then left out of the applicable maximum). */
  max: number | null;
  /** The text or technical observation the result rests on ("" when there is none). */
  evidence: string;
  /** The page or request the evidence came from. */
  sourceUrl: string | null;
  /** Why the result was given. */
  explanation: string;
  /** The Act and Rules provisions behind the check, as text. */
  legal: string | null;
  /** True when the check ran to the end on the evidence available; false when it could not be completed. */
  completed: boolean;
  /** A limit that lowers confidence in the result, or null. */
  limitation: string | null;
  /** The marks as printed: "1 / 1", "0.5 / 1", "0 / 1", or "N/A" when the check does not apply. */
  marksText: string;
}

export interface TenAssessment {
  checks: TenCheck[];
  awarded: number;
  applicableMax: number;
  /** 0 to 100, one decimal, half up; null when no check applies (no meaningful score). */
  score: number | null;
  formula: string;
  /** The total as printed: "7.5 / 10" (marks awarded over the applicable maximum), or "No applicable check" when none applies. */
  totalText: string;
  /** The score as printed: "75%", or "No meaningful score" when no check applies. */
  scoreText: string;
  counts: { passed: number; partial: number; failed: number; notApplicable: number };
  pages: { discovered: number | null; scanned: number; failed: number; skipped: number };
  limitations: string[];
}

const MARKS: Record<TenResult, number | null> = { Passed: 1, "Partially Passed": 0.5, Failed: 0, "Not Applicable": null };
const fmt = (n: number): string => String(Math.round(n * 100) / 100);
const clip = (t: string, n = 150): string => (t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t);

/** Marks as printed: awarded over maximum, or N/A for a check that does not apply. */
export const marksLabel = (awarded: number | null, max: number | null): string => (max === null ? "N/A" : `${fmt(awarded ?? 0)} / ${fmt(max)}`);

/** 0 to 100 to one decimal, half up (not banker's rounding), so 31.25 prints as 31.3 and never as 31.2. */
export const roundScore = (v: number): number => Math.floor(v * 10 + 0.5 + 1e-9) / 10;

/**
 * The scoring, in one place: marks awarded and the applicable maximum (a check that does not apply is left out of the maximum), the score, the counts, and every figure as it is printed.
 * Everything the report shows about marks comes from here, so the table, the total and the score cannot disagree.
 */
export function scoreChecks(checks: readonly Pick<TenCheck, "result" | "awarded" | "max">[]): {
  awarded: number; applicableMax: number; score: number | null; counts: TenAssessment["counts"]; formula: string; totalText: string; scoreText: string;
} {
  const applicable = checks.filter((c) => c.max !== null);
  const awarded = applicable.reduce((n, c) => n + (c.awarded ?? 0), 0);
  const applicableMax = applicable.reduce((n, c) => n + (c.max ?? 0), 0);
  const score = applicableMax === 0 ? null : roundScore((awarded / applicableMax) * 100);
  const count = (r: TenResult) => checks.filter((c) => c.result === r).length;
  const counts = { passed: count("Passed"), partial: count("Partially Passed"), failed: count("Failed"), notApplicable: count("Not Applicable") };
  const formula = score === null
    ? "No check applies to this website, so no meaningful score can be calculated."
    : `Score = marks awarded ÷ applicable marks × 100 = ${fmt(awarded)} ÷ ${fmt(applicableMax)} × 100 = ${fmt(score)}${counts.notApplicable ? ` (${counts.notApplicable} check${counts.notApplicable === 1 ? "" : "s"} not applicable, left out of the maximum)` : ""}`;
  return {
    awarded, applicableMax, score, counts, formula,
    totalText: score === null ? "No applicable check" : `${fmt(awarded)} / ${fmt(applicableMax)}`,
    scoreText: score === null ? "No meaningful score" : `${fmt(score)}%`,
  };
}

const NO_POLICY_WORDS = "No privacy policy text was available to read, so this could not be evidenced (not a finding that the law was broken).";

interface Draft { result: TenResult; evidence?: string; sourceUrl?: string | null; explanation: string; completed?: boolean; limitation?: string | null }

function make(id: number, name: string, legal: string | null, d: Draft): TenCheck {
  const m = MARKS[d.result];
  return {
    id, name, result: d.result, awarded: m, max: m === null ? null : 1,
    evidence: d.evidence ?? "", sourceUrl: d.sourceUrl ?? null, explanation: d.explanation, legal,
    completed: d.completed ?? true, limitation: d.limitation ?? null, marksText: marksLabel(m, m === null ? null : 1),
  };
}

/** What the policy analysis says about one requirement: its status, the verified quote and its page. */
const find = (a: PolicyAnalysis | null, id: string): PolicyRequirementResult | undefined => a?.requirements.find((r) => r.id === id);
const read = (a: PolicyAnalysis | null): boolean => !!a && a.status === "analysed" && a.requirements.length > 0;

/** A check that rests on one policy requirement. */
function policyCheck(a: PolicyAnalysis | null, reqId: string, what: string, sourceLimit: string | null, foundUrl: string | null = null): Draft {
  if (!read(a)) {
    // A policy page that exists but could not be read is a limit of the scan, not an absence on the site: it is said so and the page is named.
    if (foundUrl) {
      return {
        result: "Failed", completed: false, sourceUrl: foundUrl,
        explanation: "Policy page found, but its text could not be read: not evidenced (a limit of the scan, not a legal finding).",
        limitation: "A privacy policy page was found but its text could not be read.",
      };
    }
    return { result: "Failed", completed: false, explanation: NO_POLICY_WORDS, limitation: sourceLimit };
  }
  const r = find(a, reqId);
  if (!r) return { result: "Failed", completed: false, explanation: `The policy analysis did not assess ${what}.`, limitation: sourceLimit };
  if (r.status === "verified") return { result: "Passed", evidence: clip(r.quote), sourceUrl: r.source_url, explanation: `The policy states ${what}; the quoted passage was verified word for word against the policy text.` };
  if (r.status === "partial") return { result: "Partially Passed", evidence: clip(r.quote), sourceUrl: r.source_url, explanation: `The policy says part of ${what}: ${r.detail || "not every element was found"}.` };
  return {
    result: "Failed", explanation: `${what[0].toUpperCase()}${what.slice(1)} was not found in the policy text that was read (not a finding that the law was broken; it may be given elsewhere).`,
    sourceUrl: a!.documents[0]?.url ?? null, limitation: sourceLimit,
  };
}

const pickBest = (rs: (PolicyRequirementResult | undefined)[]) => rs.find((r) => r?.status === "verified") ?? rs.find((r) => r?.status === "partial");

export function buildTenChecks(model: ReportModel): TenAssessment {
  const ev = model.evidence;
  const a = model.policyAnalysis;
  const sp = model.scanPages;
  const scanned = sp?.scannedCount ?? model.visual?.pagesScanned ?? 0;
  const failedPages = sp?.failed.length ?? 0;
  const skippedPages = (sp?.skipped ?? []).reduce((n, x) => n + x.count, 0);
  const pageNote = scanned > 0 ? `${scanned} page${scanned === 1 ? "" : "s"} of the entered domain ${scanned === 1 ? "was" : "were"} scanned${sp?.discovered != null ? ` of ${sp.discovered} found` : ""}; the text of pages other than the privacy policy is not kept.` : null;

  // 1. Privacy policy availability
  const policies = (ev?.policies ?? []).filter((p) => /privacy/i.test(p.policy_type));
  const doc = a?.documents.find((d) => /privacy/i.test(d.policy_type)) ?? a?.documents[0];
  const c1: Draft = policies.length > 0 || doc
    ? (read(a) || (doc && doc.characters > 0)
      ? { result: "Passed", sourceUrl: policies[0]?.url ?? doc?.url ?? null, evidence: `Privacy policy page found${doc ? ` (${doc.characters.toLocaleString("en-IN")} characters of text read)` : ""}.`, explanation: "A privacy policy page was found on the scanned pages and its text was read." }
      : { result: "Partially Passed", sourceUrl: policies[0]?.url ?? null, evidence: "Privacy policy page found.", explanation: "A privacy policy page was found, but its text could not be read, so what it says could not be assessed.", limitation: "The policy text was not available." })
    : scanned === 0
      ? { result: "Failed", completed: false, explanation: "The website could not be accessed, so no page could be scanned and the search for a privacy policy was not made.", limitation: "No page loaded." }
      : { result: "Failed", explanation: `No privacy policy page was found after scanning ${scanned} page${scanned === 1 ? "" : "s"}${sp?.discovered != null ? ` of the ${sp.discovered} found` : ""} on the entered domain (privacy and cookie pages are tried first). It may exist on another domain or on a page the scan did not reach. This is not a finding that the law was broken.`,
        limitation: sp?.stopReason ?? "Only the entered domain was searched." };

  // 2-5 policy content
  const noPol = "No privacy policy text was available to read; the text of other pages is not kept.";
  const foundUrl = policies[0]?.url ?? null;
  const c2 = policyCheck(a, "notice_data_itemised", "the personal data it collects", noPol, foundUrl);
  const c3 = policyCheck(a, "notice_purposes", "why personal data is collected and used", noPol, foundUrl);
  const c4 = policyCheck(a, "data_sharing", "sharing of personal data with processors and other third parties", noPol, foundUrl);
  const c5 = policyCheck(a, "retention", "how long personal data is kept", noPol, foundUrl);

  // 6. user rights
  let c6: Draft;
  if (!read(a)) c6 = policyCheck(a, "rights_means", "the user rights and how to exercise them", noPol, foundUrl);
  else {
    const means = find(a, "rights_means");
    const rights = ["rights_access", "rights_correction_erasure", "rights_nominate"].map((id) => find(a, id));
    const rightsFound = rights.filter((r) => r?.status === "verified").length;
    const best = pickBest([means, ...rights]);
    if (means?.status === "verified" && rightsFound > 0) c6 = { result: "Passed", evidence: clip(best?.quote ?? ""), sourceUrl: best?.source_url ?? null, explanation: `The policy names ${rightsFound} user right${rightsFound === 1 ? "" : "s"} and the means of making a request.` };
    else if (best) c6 = { result: "Partially Passed", evidence: clip(best.quote), sourceUrl: best.source_url, explanation: `Only part is evidenced: ${means?.status === "verified" ? "the means of making a request, but no right is named" : rightsFound > 0 ? "rights are named, but no means of making a request" : "some wording, not a complete statement of rights and the means"}.` };
    else c6 = { result: "Failed", explanation: "No statement of user rights or of how to exercise them was found in the policy text that was read. This is not a finding that the law was broken.", sourceUrl: a!.documents[0]?.url ?? null, limitation: noPol };
  }

  // 7. contact and grievance
  let c7: Draft;
  if (!read(a)) c7 = policyCheck(a, "grievance_contact", "a privacy contact or grievance mechanism", noPol, foundUrl);
  else {
    const g = find(a, "grievance_contact"), d = find(a, "dpo_contact");
    const t = find(a, "grievance_timeline");
    const gOk = g?.status === "verified", dOk = d?.status === "verified", tOk = t?.status === "verified";
    const best = pickBest([g, d, t]);
    if (gOk && (dOk || tOk)) c7 = { result: "Passed", evidence: clip(g!.quote), sourceUrl: g!.source_url, explanation: `A grievance contact was found together with ${dOk && tOk ? "a business contact and a response period" : dOk ? "a business contact of a Data Protection Officer or answering person" : "a response period"}.` };
    else if (best) c7 = { result: "Partially Passed", evidence: clip(best.quote), sourceUrl: best.source_url, explanation: `Only part is evidenced: ${gOk ? "a grievance contact, but neither a response period nor a business contact" : dOk ? "a business contact, but no grievance contact" : "some wording, not a complete contact"}.` };
    else c7 = { result: "Failed", explanation: "No privacy contact, Data Protection Officer or grievance mechanism was found in the policy text that was read. This is not a finding that the law was broken: it may be published elsewhere.", sourceUrl: a!.documents[0]?.url ?? null, limitation: noPol };
  }

  // 8. cookie and tracker detection
  const detectionRan = scanned > 0 && !!ev;
  const cookies = ev?.cookies ?? [], trackers = ev?.trackers ?? [];
  const unclassified = cookies.filter((c) => !c.category && !c.vendor).length + trackers.filter((t) => !t.category && !t.vendor).length;
  const names = [...new Set([...trackers.map((t) => t.vendor).filter((v): v is string => !!v), ...cookies.map((c) => c.name)])].slice(0, 4).join(", ");
  const classNote = unclassified > 0 ? `${unclassified} observed item${unclassified === 1 ? "" : "s"} could not be classified as necessary, analytics or advertising.` : null;
  const lim8 = ["Absence of a tracker is not proof that none exists under all conditions.", classNote].filter(Boolean).join(" ");
  const seen8 = cookies.length + trackers.length === 0 ? "No cookie or tracking request was observed on the scanned pages." : `${cookies.length} cookie${cookies.length === 1 ? "" : "s"} and ${trackers.length} tracking request${trackers.length === 1 ? "" : "s"} observed${names ? ` (${names})` : ""}.`;
  const c8: Draft = !detectionRan
    ? { result: "Failed", completed: false, explanation: scanned === 0 ? "No page loaded, so cookies and trackers could not be detected." : "The scan's cookie and tracker evidence was not loaded, so the detection could not be assessed.", limitation: "Detection could not be completed." }
    : failedPages > 0
      ? { result: "Partially Passed", evidence: seen8, sourceUrl: trackers[0]?.page_url ?? null, explanation: `Detection ran on the pages that loaded, but ${failedPages} page${failedPages === 1 ? "" : "s"} did not load, so tracking there could not be observed.`, limitation: lim8 }
      : { result: "Passed", evidence: seen8, sourceUrl: trackers[0]?.page_url ?? null, explanation: "Cookies and tracking requests were detected on every scanned page.", limitation: lim8 };

  // 9. consent behaviour
  const cons = model.visual?.consent ?? null;
  const sig = ev?.consent_signals?.[0] ?? null;
  const hasBanner = !!sig && sig.mechanism_type !== "none" && sig.banner_displayed !== false;
  // Analytics, marketing and advertising cookies and requests, from the scan's own classification, in each consent state.
  const TRACKING = /analytic|market|advert|track/i;
  const trackCookie = (state: string) => cookies.filter((c) => TRACKING.test(c.category ?? "") && c.consent_states.includes(state)).length;
  const trackReq = (state: string) => trackers.filter((t) => TRACKING.test(t.category ?? "") && t.consent_states.includes(state)).length;
  const preTracking = trackCookie("pre_consent") + trackReq("pre_consent");
  const rejectTracking = trackCookie("post_reject") + trackReq("post_reject");
  const anyTracking = cookies.some((c) => TRACKING.test(c.category ?? "")) || trackers.some((t) => TRACKING.test(t.category ?? ""));
  let c9: Draft;
  if (!cons) c9 = { result: "Failed", completed: false, explanation: "The consent states were not loaded, so Accept and Reject could not be compared.", limitation: "Consent behaviour could not be assessed." };
  else {
    const preClean = cons.known[0] === 0 && preTracking === 0, rejectClean = cons.known[2] === 0 && (!cons.rejectTested || rejectTracking === 0);
    const line = `Analytics/marketing requests: before consent ${cons.known[0]}, after Accept ${cons.acceptTested ? cons.known[1] : "not tested"}, after Reject ${cons.rejectTested ? cons.known[2] : "not tested"}. Analytics/marketing cookies and requests seen before consent: ${preTracking}.`;
    const home = model.screenshot?.page_url ?? null;
    if (!hasBanner && !anyTracking && unclassified === 0 && cons.known[0] === 0 && cons.known[1] === 0 && cons.known[2] === 0) {
      c9 = { result: "Not Applicable", evidence: line, sourceUrl: home, explanation: "No consent banner was found and every cookie and request observed was classified as necessary or functional (none as analytics, marketing or advertising), so there was no consent interface to test." };
    } else if (!hasBanner) {
      c9 = { result: "Failed", evidence: line, sourceUrl: home, explanation: "Analytics, marketing or advertising cookies or requests were observed, but no consent banner was displayed to the scanner, so Accept and Reject could not be tested.", limitation: "A banner that was not shown to the scanner may still exist." };
    } else if (!cons.acceptTested && !cons.rejectTested) {
      c9 = { result: "Failed", completed: false, evidence: line, sourceUrl: home, explanation: "A banner was found, but neither Accept nor Reject could be clicked and confirmed, so only the state before consent was observed.", limitation: "Accept and Reject were not tested." };
    } else if (cons.acceptTested && cons.rejectTested && preClean && rejectClean) {
      c9 = { result: "Passed", evidence: line, sourceUrl: home, explanation: "All three states were tested: no analytics or marketing before consent, and none after Reject." };
    } else if (!preClean && !rejectClean) {
      c9 = { result: "Failed", evidence: line, sourceUrl: home, explanation: "Analytics or marketing requests were observed before consent and again after Reject." };
    } else {
      c9 = { result: "Partially Passed", evidence: line, sourceUrl: home, explanation: `${!preClean ? "Analytics or marketing requests were observed before any consent action" : !rejectClean ? "Analytics or marketing requests were still observed after Reject" : "Only one of Accept and Reject could be tested"}.`, limitation: !cons.acceptTested || !cons.rejectTested ? `${!cons.acceptTested ? "Accept" : "Reject"} was not tested.` : null };
    }
  }

  const first = [
    make(1, "Privacy Policy Availability", "DPDP Act s.5(1); Rules r.3(a), 3(b)", c1),
    make(2, "Personal Data Collection Disclosure", "DPDP Act s.5(1)(i); Rules r.3(b)(i)", c2),
    make(3, "Purpose of Data Processing", "DPDP Act s.5(1)(i); Rules r.3(b)(ii)", c3),
    make(4, "Data Sharing and Third Parties", "DPDP Act s.8(2)", c4),
    make(5, "Data Retention Disclosure", "DPDP Act s.8(7)", c5),
    make(6, "User Rights Disclosure", "DPDP Act s.11, 12, 13, 14; Rules r.14(1)", c6),
    make(7, "Contact and Grievance Mechanism", "DPDP Act s.8(9), 8(10), 13; Rules r.9, 14(3)", c7),
    make(8, "Cookie and Tracker Detection", "DPDP Act s.6(1); Rules r.3", c8),
    make(9, "Consent Behaviour", "DPDP Act s.6(1), 6(4); Rules r.3(c)(i)", c9),
  ];

  // 10. evidence and transparency: can the results that could be assessed be traced to a page or an observation? A check that could not be completed has no result to trace, and its gap is
  // named here instead of being counted against this check a second time.
  const gaps = first.filter((c) => c.result === "Failed" && !c.completed).map((c) => c.id);
  const rest = first.filter((c) => c.result !== "Not Applicable" && !(c.result === "Failed" && !c.completed));
  const witnessed = rest.filter((c) => c.evidence !== "" || c.sourceUrl !== null).length;
  const frac = rest.length === 0 ? 0 : witnessed / rest.length;
  const gapNote = gaps.length ? ` Check${gaps.length === 1 ? "" : "s"} ${gaps.join(", ")} could not be completed and ${gaps.length === 1 ? "is" : "are"} listed under the limitations.` : "";
  const c10: Draft = !ev
    ? { result: "Failed", completed: false, explanation: "The scan's evidence was not loaded, so the other results could not be traced to their source.", limitation: "Evidence unavailable." }
    : rest.length === 0
      ? { result: "Failed", completed: false, explanation: `None of the other checks could be completed, so there is no result to trace to its source.${gapNote}`, limitation: "No check could be completed." }
      : frac >= 0.8
        ? { result: "Passed", evidence: `${witnessed} of ${rest.length} completed results carry a source page or an observation.`, sourceUrl: null, explanation: `The completed results can be traced to a source page and its text, or to a technical observation.${gapNote}` }
        : frac >= 0.5
          ? { result: "Partially Passed", evidence: `${witnessed} of ${rest.length} completed results carry a source page or an observation.`, explanation: `Some completed results rest on evidence that could be traced; the others found nothing to cite.${gapNote}`, limitation: failedPages > 0 ? `${failedPages} page${failedPages === 1 ? "" : "s"} did not load.` : null }
          : { result: "Failed", evidence: `${witnessed} of ${rest.length} completed results carry a source page or an observation.`, explanation: `Most completed results have no source page or observation behind them, so they cannot be independently reviewed. Missing evidence is not proof of a violation.${gapNote}`, limitation: failedPages > 0 ? `${failedPages} page${failedPages === 1 ? "" : "s"} did not load.` : null };
  const checks = [...first, make(10, "Privacy Evidence and Transparency", null, c10)];

  const sc = scoreChecks(checks);
  const limitations: string[] = [];
  if (pageNote) limitations.push(pageNote);
  if (failedPages > 0) limitations.push(`${failedPages} page${failedPages === 1 ? "" : "s"} did not load (see the page list).`);
  if (skippedPages > 0) limitations.push(`${skippedPages} page${skippedPages === 1 ? " was" : "s were"} found but not scanned.`);
  // Checks that could not be completed, grouped by the limit they share, so one limit is said once.
  const byLimit = new Map<string, number[]>();
  for (const c of checks) if (c.result === "Failed" && !c.completed) byLimit.set(c.limitation ?? c.explanation, [...(byLimit.get(c.limitation ?? c.explanation) ?? []), c.id]);
  for (const [text, ids] of byLimit) limitations.push(`Check${ids.length === 1 ? "" : "s"} ${ids.join(", ")} could not be completed: ${text}`);

  return {
    checks, awarded: sc.awarded, applicableMax: sc.applicableMax, score: sc.score, counts: sc.counts, formula: sc.formula, totalText: sc.totalText, scoreText: sc.scoreText,
    pages: { discovered: sp?.discovered ?? model.visual?.pagesFound ?? null, scanned, failed: failedPages, skipped: skippedPages },
    limitations,
  };
}
