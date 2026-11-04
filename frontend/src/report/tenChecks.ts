import type { PolicyAnalysis, PolicyRequirementResult } from "../api/types";
import type { ReportModel } from "./buildReport";
import { formatScore } from "./scoreFormat";

/**
 * The ten predefined checks every scanned website is assessed on, and the one 10-mark score they give. Every check is assessed on its own evidence: the missing of a privacy policy page
 * fails check 1 and nothing else stops. There is no "Unable to Verify" result: a check whose evidence could not be established is Failed, with the reason and the limit stated, and the wording
 * never says the law was broken (a missing disclosure is "not evidenced", not a violation). Pure: it reads the report model and invents nothing.
 */
export type TenResult = "Passed" | "Partially Passed" | "Failed" | "Not Assessed" | "Not Applicable";

/** How good the evidence behind a result is, apart from whether there is any: Specific (direct and itemised), General (broad wording that does not name what it covers), Limited (narrow in scope or in the pages it covers). */
export type TenQuality = "Specific" | "General" | "Limited" | "None" | "Not assessed" | "n/a";

/** What a check is, so a technical observation is never read as a legal requirement and a best practice is never read as a required item. */
export type TenKind = "dpdp" | "practice" | "technical" | "transparency";
/** Why the result is what it is. Not found, not tested, evidence unavailable and scan failed are different things and are never merged. */
export type TenBasis =
  | "Verified" | "Partially verified" | "Not evidenced" | "Not found in scanned pages" | "Content extraction failed" | "Partially extracted" | "Access failed" | "Crawling restricted"
  | "Not tested" | "Assessment incomplete" | "Evidence unavailable" | "Scan failed" | "Not applicable";

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
  kind: TenKind;
  basis: TenBasis;
  /** The quality of the evidence: what it states, and how specifically. */
  quality: TenQuality;
  /** How much of the requirement the evidence covers, in words ("One data category only", "Homepage only"), or null when it covers it as far as the scan can tell. */
  coverage: string | null;
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
  /** The score as printed everywhere: "75/100" (report/scoreFormat.ts), or "No meaningful score" when no check applies. */
  scoreText: string;
  counts: { passed: number; partial: number; failed: number; notAssessed: number; notApplicable: number };
  /** Checks that could not be assessed (a scan failure, an unreadable page, an analysis that did not run). They are left out of the score, never scored 0: a missing page is not a failed check. A disclosure that was looked for and not found is Failed. */
  incomplete: number;
  /** True when fewer than half the checks could be assessed: the score then rests on very little and is only provisional. */
  limitedBasis: boolean;
  /** What the score rests on and what it does not show, in two or three sentences. One text, used by the dashboard and the PDF. */
  summary: string;
  /** One recommendation for each check that did not pass, each tied to that check's own finding; none for a check that passed. */
  recommendations: { id: number; text: string }[];
  pages: { discovered: number | null; scanned: number; failed: number; skipped: number };
  limitations: string[];
}

const MARKS: Record<TenResult, number | null> = { Passed: 1, "Partially Passed": 0.5, Failed: 0, "Not Assessed": null, "Not Applicable": null };
const fmt = (n: number): string => String(Math.round(n * 100) / 100);
/** A passage cut to `n` characters, at a word boundary when one is near, so a quotation never ends mid-word. */
const clip = (t: string, n = 150): string => {
  if (t.length <= n) return t;
  const cut = t.slice(0, n - 1), sp = cut.lastIndexOf(" ");
  return `${(sp > n * 0.6 ? cut.slice(0, sp) : cut).trimEnd()}…`;
};

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
  const counts = { passed: count("Passed"), partial: count("Partially Passed"), failed: count("Failed"), notAssessed: count("Not Assessed"), notApplicable: count("Not Applicable") };
  const formula = score === null
    ? "No check applies to this website, so no meaningful score can be calculated."
    : `Score = marks awarded ÷ applicable marks × 100 = ${fmt(awarded)} ÷ ${fmt(applicableMax)} × 100 = ${fmt(score)}${counts.notApplicable ? ` (${counts.notApplicable} check${counts.notApplicable === 1 ? "" : "s"} not applicable, left out of the maximum)` : ""}${counts.notAssessed ? ` (${counts.notAssessed} check${counts.notAssessed === 1 ? "" : "s"} could not be assessed, left out of the maximum)` : ""}`;
  return {
    awarded, applicableMax, score, counts, formula,
    totalText: score === null ? "No applicable check" : `${fmt(awarded)} / ${fmt(applicableMax)}`,
    scoreText: formatScore(score),
  };
}

const NO_POLICY_WORDS = "No privacy policy text was available to read, so this could not be evidenced (not a finding that the law was broken).";

interface Draft { basis?: TenBasis; result: TenResult; evidence?: string; sourceUrl?: string | null; explanation: string; completed?: boolean; limitation?: string | null; quality?: TenQuality; coverage?: string | null }

function make(id: number, name: string, legal: string | null, kind: TenKind, d0: Draft): TenCheck {
  // A check that could not be run to the end is Not Assessed: it is left out of the score, not scored 0 (a page that could not be reached is not a failed check).
  const d: Draft = d0.result === "Failed" && d0.completed === false ? { ...d0, result: "Not Assessed" } : d0;
  const m = MARKS[d.result];
  const basis: TenBasis = d.basis ?? (d.result === "Passed" ? "Verified" : d.result === "Partially Passed" ? "Partially verified" : d.result === "Not Applicable" ? "Not applicable" : d.completed === false ? "Evidence unavailable" : "Not evidenced");
  const quality: TenQuality = d.quality ?? (d.result === "Passed" ? "Specific" : d.result === "Partially Passed" ? "Limited" : d.result === "Not Assessed" ? "Not assessed" : d.result === "Not Applicable" ? "n/a" : "None");
  return {
    kind, basis, quality, coverage: d.coverage ?? null,
    id, name, result: d.result, awarded: m, max: m === null ? null : 1,
    evidence: d.evidence ?? "", sourceUrl: d.sourceUrl ?? null, explanation: d.explanation, legal,
    completed: d.result === "Not Assessed" ? false : d.completed ?? true, limitation: d.limitation ?? null,
    marksText: d.result === "Not Assessed" ? "Not assessed" : marksLabel(m, m === null ? null : 1),
  };
}

/** What the policy analysis says about one requirement: its status, the verified quote and its page. */
const find = (a: PolicyAnalysis | null, id: string): PolicyRequirementResult | undefined => a?.requirements.find((r) => r.id === id);
/** A requirement counts as verified only when it carries the quoted passage it was verified by: a "verified" status with no quote is not evidence. */
const isVerified = (r: PolicyRequirementResult | undefined): boolean => r?.status === "verified" && r.quote.trim().length > 0;
const read = (a: PolicyAnalysis | null): boolean => !!a && a.status === "analysed" && a.requirements.length > 0;

const LEGAL_DOC = /\/(privacy|cookie)[-_]?(policy|policies|notice|statement)?\/?$/i;
const docPath = (u: string): string => { try { return new URL(u, "https://x.invalid").pathname; } catch { return u; } };
/**
 * The privacy and cookie policy pages whose text was cut. Only these matter: a long page that merely mentions a policy (a course or product page the scan filed as one) being
 * cut says nothing about what the policy says, so it is never the reason a requirement was not found. (A report once blamed "the storage limit" for a 7,808-character policy
 * because an unrelated long page in the same scan had been cut.)
 */
const cutPolicyDocs = (a: PolicyAnalysis) => a.documents.filter((d) => d.truncated && LEGAL_DOC.test(docPath(d.url)));
/** Why a requirement not found cannot be called Failed when a policy page was cut: what came after the cut was not read. */
function cutPolicyDraft(a: PolicyAnalysis, what: string, sourceLimit: string | null): Draft {
  const cut = cutPolicyDocs(a)[0];
  return {
    result: "Failed", completed: false, basis: "Partially extracted", sourceUrl: cut.url,
    explanation: `${what[0].toUpperCase()}${what.slice(1)} was not found in the first ${cut.characters.toLocaleString("en-IN")} characters of ${cut.url.replace(/^https?:\/\//, "")}, which is longer than the scan keeps; the rest was not reviewed, so this is not assessed.`,
    limitation: sourceLimit ?? "Only the first part of a very long policy page was read.",
  };
}

/** A check that rests on one policy requirement. */
function policyCheck(a: PolicyAnalysis | null, reqId: string, what: string, sourceLimit: string | null, foundUrl: string | null = null, unread: TenBasis = "Content extraction failed"): Draft {
  // An analysis that never ran is not a policy that says nothing: nothing was assessed.
  if (a?.status === "not_run") return { result: "Failed", completed: false, basis: "Assessment incomplete", sourceUrl: foundUrl, explanation: "The policy analysis has not run for this scan, so what the policy says was not assessed.", limitation: "The policy analysis has not run." };
  if (!read(a)) {
    // A policy page that exists but could not be read is a limit of the scan, not an absence on the site: it is said so and the page is named.
    if (foundUrl) {
      return {
        result: "Failed", completed: false, sourceUrl: foundUrl, basis: unread,
        explanation: unread === "Crawling restricted" ? "Policy page found, but the site does not allow it to be scanned: not evidenced (a limit of the scan, not a legal finding)."
          : unread === "Access failed" ? "Policy page found, but it did not load: not evidenced (a limit of the scan, not a legal finding)."
          : "Policy page found, but its text could not be read: not evidenced (a limit of the scan, not a legal finding).",
        limitation: unread === "Crawling restricted" ? "The site restricts access to the privacy policy page." : unread === "Access failed" ? "The privacy policy page did not load." : "A privacy policy page was found but its text could not be read.",
      };
    }
    return { result: "Failed", completed: false, basis: "Not evidenced", explanation: NO_POLICY_WORDS, limitation: sourceLimit };
  }
  const r = find(a, reqId);
  if (!r) return { result: "Failed", completed: false, explanation: `The policy analysis did not assess ${what}.`, limitation: sourceLimit };
  if (r.status === "verified" && !isVerified(r)) return { result: "Failed", completed: false, basis: "Evidence unavailable", sourceUrl: r.source_url, explanation: "The analysis marked this as verified but gave no quoted passage, so it is not counted.", limitation: sourceLimit };
  if (r.status === "verified") return { result: "Passed", evidence: clip(r.quote), sourceUrl: r.source_url, explanation: `The policy states ${what}; the quoted passage was verified word for word against the policy text.` };
  if (r.status === "partial") return { result: "Partially Passed", evidence: clip(r.quote), sourceUrl: r.source_url, explanation: `The policy says part of ${what}: ${r.detail || "not every element was found"}.` };
  // A privacy or cookie policy page that was cut: what came after the cut was not read, so "not found" cannot be called Failed. It is not assessed.
  if (cutPolicyDocs(a!).length > 0) return cutPolicyDraft(a!, what, sourceLimit);
  return {
    result: "Failed", explanation: `${what[0].toUpperCase()}${what.slice(1)} was not found in the policy text that was read (not a finding that the law was broken; it may be given elsewhere).`,
    sourceUrl: a!.documents[0]?.url ?? null, limitation: sourceLimit,
  };
}

/**
 * Evidence QUALITY, as against evidence that merely exists. A requirement the analysis verified word for word is still only as good as what the passage says: a retention period for one kind of data
 * is not a retention disclosure, and "service providers" with no kind of provider named is not a statement of who data is shared with. These read the verified passage itself (never the page), and
 * lower a Passed to Partially Passed with the gap stated. They are the documented rules of the 10-check methodology; none of them is specific to one website.
 */
const GENERIC_SCOPE = /\b(personal (data|information|details)|your (data|information|details)|(data|information) (that )?we (collect|hold|process|receive)|all (of )?(your |the )?(data|information)|(we|company) (retain|keep|store|hold)s? (your|personal|the))\b/i;
const CATEGORY_ONLY = /\b(contact (form|us)|enquir(y|ies)|inquir(y|ies)|form submissions?|submissions?|newsletters?|subscribers?|job applications?|applicants?|resumes?|surveys?|support (tickets?|requests?)|server logs?|log files?|backups?|cookies?)\b/i;
/** True when a retention passage names a period or criterion for one kind of data only. */
export const retentionIsCategoryOnly = (quote: string): boolean => CATEGORY_ONLY.test(quote) && !GENERIC_SCOPE.test(quote);
const RECIPIENT_NAMED = /\b(analytics|advertis\w*|marketing|payment (processors?|providers?|gateways?)|cloud|hosting|(sub-?)?processors?|affiliates?|subsidiar\w+|group compan\w+|government|law enforcement|regulator\w*|authorit\w+|courts?|legal (process|obligations?|requirements?)|crm|email (service|delivery) providers?|shipping|logistics|auditors?|professional advis[eo]rs?)\b/i;
/** True when a sharing passage names at least one kind of recipient (not only "service providers" or "third parties"). */
export const sharingNamesRecipients = (quote: string): boolean => RECIPIENT_NAMED.test(quote);

const pickBest = (rs: (PolicyRequirementResult | undefined)[]) => rs.find((r) => isVerified(r)) ?? rs.find((r) => r?.status === "partial");

export function buildTenChecks(model: ReportModel): TenAssessment {
  const ev = model.evidence;
  const a = model.policyAnalysis;
  const sp = model.scanPages;
  const scanned = sp?.scannedCount ?? model.visual?.pagesScanned ?? 0;
  const failedPages = sp?.failed.length ?? 0;
  const skippedPages = (sp?.skipped ?? []).reduce((n, x) => n + x.count, 0);
  const pageNote = scanned > 0 ? `${scanned} page${scanned === 1 ? "" : "s"} of the entered domain ${scanned === 1 ? "was" : "were"} scanned${sp?.discovered != null ? ` of ${sp.discovered} found` : ""}; the text of pages other than the privacy policy is not kept.` : null;

  // 1. Privacy policy availability
  // The page at the policy's own address (/privacy-policy) comes before a page that only mentions one (/privacy-policy-generator, a product page), so that is the page named as the evidence.
  const LEGAL_PATH = /\/(privacy|cookie)[-_]?(policy|policies|notice|statement)?\/?$/i;
  const pathOf = (u: string) => { try { return new URL(u, "https://x.invalid").pathname; } catch { return u; } };
  const legalFirst = <T extends { url: string }>(list: T[]): T[] => [...list].sort((x, y) => Number(LEGAL_PATH.test(pathOf(y.url))) - Number(LEGAL_PATH.test(pathOf(x.url))));
  const policies = legalFirst((ev?.policies ?? []).filter((p) => /privacy/i.test(p.policy_type)));
  const privacyDocs = legalFirst((a?.documents ?? []).filter((d) => /privacy/i.test(d.policy_type)));
  const doc = privacyDocs[0] ?? a?.documents[0];
  // A privacy page the scan found a link to but could not load is not a missing policy: it is a page that could not be read.
  const failedPolicy = (sp?.failed ?? []).find((f) => /privacy|policy/i.test(f.url)) ?? (ev?.pages ?? []).filter((p) => /privacy|policy/i.test(p.url) && p.discovered_via === "robots_disallowed").map((p) => ({ url: p.url, reason: "Blocked by robots.txt" }))[0];
  const policyBlocked = !!failedPolicy && /robots|restrict|denied|forbidden|401|403/i.test(failedPolicy.reason);
  const c1: Draft = policies.length > 0 || doc
    ? (read(a) || (doc && doc.characters > 0)
      ? (doc?.truncated || policies[0]?.text_state === "partial"
        ? { result: "Passed", basis: "Partially extracted", sourceUrl: policies[0]?.url ?? doc?.url ?? null, evidence: `Privacy policy page found (${(doc?.characters ?? 0).toLocaleString("en-IN")} characters of text read).`, explanation: "A privacy policy page was found and readable text extracted, but the text was cut at the storage limit, so the rest was not reviewed.", limitation: "Only the first part of a very long policy was read." }
        : { result: "Passed", sourceUrl: policies[0]?.url ?? doc?.url ?? null, evidence: `Privacy policy page found${doc ? ` (${doc.characters.toLocaleString("en-IN")} characters of text read)` : ""}.`, explanation: "A privacy policy page was found on the scanned pages and its text was read." })
      : { result: "Partially Passed", basis: a?.status === "not_run" ? "Assessment incomplete" : "Content extraction failed", sourceUrl: policies[0]?.url ?? null, evidence: "Privacy policy page found.", explanation: a?.status === "not_run" ? "A privacy policy page was found, but the policy analysis has not run, so what it says was not assessed." : "A privacy policy page was found, but its text could not be read, so what it says could not be assessed.", limitation: "The policy text was not available." })
    : failedPolicy
      ? { result: "Failed", completed: false, basis: policyBlocked ? "Crawling restricted" : "Access failed", sourceUrl: failedPolicy.url,
          explanation: policyBlocked ? `A privacy policy page was found at this address, but the site does not allow it to be scanned (${failedPolicy.reason}), so it was not read. This is not a finding that the policy is missing.` : `A privacy policy page was found at this address but did not load (${failedPolicy.reason}), so it could not be read.`,
          limitation: policyBlocked ? "The site restricts access to this page." : "The privacy policy page did not load." }
    : scanned === 0
      ? { result: "Failed", completed: false, basis: "Scan failed", explanation: "The website could not be accessed, so no page could be scanned and the search for a privacy policy was not made.", limitation: "No page loaded." }
      : { result: "Failed", basis: "Not found in scanned pages", explanation: `No privacy policy page was found after scanning ${scanned} page${scanned === 1 ? "" : "s"}${sp?.discovered != null ? ` of the ${sp.discovered} found` : ""} on the entered domain (privacy and cookie pages are tried first). It may exist on another domain or on a page the scan did not reach, so this is limited to the pages actually searched, and is not a finding that the law was broken.`,
        limitation: sp?.stopReason ?? "Only the entered domain was searched." };

  // 2-5 policy content
  const noPol = "No privacy policy text was available to read; the text of other pages is not kept.";
  const foundUrl = policies[0]?.url ?? failedPolicy?.url ?? null;
  const unread: TenBasis = policies.length > 0 ? "Content extraction failed" : failedPolicy ? (policyBlocked ? "Crawling restricted" : "Access failed") : "Content extraction failed";
  const c2 = policyCheck(a, "notice_data_itemised", "the personal data it collects", noPol, foundUrl, unread);
  const c3 = policyCheck(a, "notice_purposes", "why personal data is collected and used", noPol, foundUrl, unread);
  const c4 = policyCheck(a, "data_sharing", "sharing of personal data with processors and other third parties", noPol, foundUrl, unread);
  const c5 = policyCheck(a, "retention", "how long personal data is kept", noPol, foundUrl, unread);
  {
    const sq = find(a, "data_sharing")?.quote ?? "";
    if (c4.result === "Passed" && !sharingNamesRecipients(sq)) {
      const seen = [...new Set((ev?.trackers ?? []).filter((t) => /analytic|market|advert/i.test(t.category ?? "") && t.vendor).map((t) => t.vendor as string))].slice(0, 3);
      c4.result = "Partially Passed"; c4.quality = "General";
      c4.coverage = "Recipients not named";
      c4.explanation = `The policy says data is shared with service providers, but the passage names no kind of recipient (analytics, advertising, payment, hosting, authorities and so on), so who receives it is not established.${seen.length ? ` The scan observed ${seen.join(", ")}, which the passage does not name.` : ""}`;
      c4.limitation = "Only the sharing passage the analysis verified was assessed; the policy may name recipients elsewhere.";
    }
    const rq = find(a, "retention")?.quote ?? "";
    if (c5.result === "Passed" && retentionIsCategoryOnly(rq)) {
      c5.result = "Partially Passed"; c5.quality = "Limited";
      c5.coverage = "One kind of data only";
      c5.explanation = "The policy gives a retention period for one kind of data only (for example form submissions). Periods or criteria for the other personal data it collects are not evidenced.";
      c5.limitation = "Retention for the policy's other data categories is not established by the verified passage.";
    }
  }

  // 6. user rights
  let c6: Draft;
  if (!read(a)) c6 = policyCheck(a, "rights_means", "the user rights and how to exercise them", noPol, foundUrl, unread);
  else {
    const means = find(a, "rights_means");
    const rights = ["rights_access", "rights_correction_erasure", "rights_nominate"].map((id) => find(a, id));
    const rightsFound = rights.filter((r) => isVerified(r)).length;
    const best = pickBest([means, ...rights]);
    const namedRight = rights.find((r) => isVerified(r));
    const shown = namedRight ?? best;
    // A right is explained when it is stated, not when the policy only says "contact us": two of the three rights the analysis looks for (access, correction and erasure, nomination) and the means of making a request.
    if (isVerified(means) && rightsFound >= 2) c6 = { result: "Passed", evidence: clip(shown?.quote ?? ""), sourceUrl: shown?.source_url ?? null, explanation: `The policy names ${rightsFound} of 3 user rights and the means of making a request.`, coverage: `${rightsFound} of 3 rights named` };
    else if (best) c6 = { result: "Partially Passed", evidence: clip((shown ?? best).quote), sourceUrl: (shown ?? best).source_url, quality: "Limited", coverage: `${rightsFound} of 3 rights named`, explanation: `Only part is evidenced: ${isVerified(means) && rightsFound === 1 ? "the means of making a request and one right, but fewer rights than are needed to say the rights are explained" : isVerified(means) ? "the means of making a request, but no right is named" : rightsFound > 0 ? "rights are named, but no means of making a request" : "some wording, not a complete statement of rights and the means"}.` };
    else c6 = cutPolicyDocs(a!).length > 0 ? cutPolicyDraft(a!, "a statement of user rights and how to exercise them", noPol)
      : { result: "Failed", explanation: "No statement of user rights or of how to exercise them was found in the policy text that was read. This is not a finding that the law was broken.", sourceUrl: a!.documents[0]?.url ?? null, limitation: noPol };
  }

  // 7. contact and grievance
  let c7: Draft;
  if (!read(a)) c7 = policyCheck(a, "grievance_contact", "a privacy contact or grievance mechanism", noPol, foundUrl, unread);
  else {
    const g = find(a, "grievance_contact"), d = find(a, "dpo_contact");
    const t = find(a, "grievance_timeline");
    const gOk = isVerified(g), dOk = isVerified(d), tOk = isVerified(t);
    const best = pickBest([g, d, t]);
    if (gOk && (dOk || tOk)) c7 = { result: "Passed", evidence: clip(g!.quote), sourceUrl: g!.source_url, explanation: `A grievance contact was found together with ${dOk && tOk ? "a business contact and a response period" : dOk ? "a business contact of a Data Protection Officer or answering person" : "a response period"}.` };
    else if (best) c7 = { result: "Partially Passed", evidence: clip(best.quote), sourceUrl: best.source_url, explanation: `Only part is evidenced: ${gOk ? "a grievance contact, but neither a response period nor a business contact" : dOk ? "a business contact, but no grievance contact" : "some wording, not a complete contact"}.` };
    else c7 = cutPolicyDocs(a!).length > 0 ? cutPolicyDraft(a!, "a privacy contact, Data Protection Officer or grievance mechanism", noPol)
      : { result: "Failed", explanation: "No privacy contact, Data Protection Officer or grievance mechanism was found in the policy text that was read. This is not a finding that the law was broken: it may be published elsewhere.", sourceUrl: a!.documents[0]?.url ?? null, limitation: noPol };
  }

  // 8. cookie and tracker detection
  const detectionRan = scanned > 0 && !!ev;
  const cookies = ev?.cookies ?? [], trackers = ev?.trackers ?? [];
  const unclassified = cookies.filter((c) => !c.category && !c.vendor).length + trackers.filter((t) => !t.category && !t.vendor).length;
  const names = [...new Set([...trackers.map((t) => t.vendor).filter((v): v is string => !!v), ...cookies.map((c) => c.name)])].slice(0, 4).join(", ");
  const classNote = unclassified > 0 ? `${unclassified} observed item${unclassified === 1 ? "" : "s"} could not be classified as necessary, analytics or advertising.` : null;
  const covNote = scanned > 0 && sp?.discovered != null && sp.discovered > scanned ? `Detection covers ${scanned} of ${sp.discovered} pages.` : null;
  const lim8 = ["Absence of a tracker is not proof that none exists under all conditions.", covNote, classNote].filter(Boolean).join(" ");
  const seen8 = cookies.length + trackers.length === 0 ? "No cookie or third-party request was observed on the scanned pages." : `${cookies.length} cookie${cookies.length === 1 ? "" : "s"} and ${trackers.length} third-party request${trackers.length === 1 ? "" : "s"} observed${names ? ` (${names})` : ""}.`;
  const c8: Draft = !detectionRan
    ? { result: "Failed", completed: false, basis: scanned === 0 ? "Scan failed" : "Evidence unavailable", explanation: scanned === 0 ? "No page loaded, so cookies and trackers could not be detected." : "The scan's cookie and tracker evidence was not loaded, so the detection could not be assessed.", limitation: "Detection could not be completed." }
    : failedPages > 0
      ? { result: "Partially Passed", evidence: seen8, sourceUrl: trackers[0]?.page_url ?? null, explanation: `Detection ran on the pages that loaded, but ${failedPages} page${failedPages === 1 ? "" : "s"} did not load, so tracking there could not be observed.`, limitation: lim8 }
      : { result: "Passed", evidence: seen8, sourceUrl: trackers[0]?.page_url ?? null, explanation: "Cookies and third-party requests were detected on every scanned page. This records what the site does; it does not assess whether the policy discloses it.", limitation: lim8, coverage: covNote ? `${scanned} of ${sp!.discovered} pages` : null };

  // 9. consent behaviour
  const cons = model.visual?.consent ?? null;
  const sig = ev?.consent_signals?.[0] ?? null;
  const hasBanner = !!sig && sig.mechanism_type !== "none" && sig.banner_displayed !== false;
  // Analytics, marketing and advertising cookies and requests, from the scan's own classification, in each consent state.
  const TRACKING = /analytic|market|advert|track/i;
  const trackCookie = (state: string) => cookies.filter((c) => TRACKING.test(c.category ?? "") && c.consent_states.includes(state)).length;
  const trackReq = (state: string) => trackers.filter((t) => TRACKING.test(t.category ?? "") && t.consent_states.includes(state)).length;
  // A tag loader (the script that loads Google Tag Manager or gtag) sends nothing about the visitor; a Google request made in consent-denied mode (gcs=G100: advertising and analytics storage both
  // denied) is a cookieless ping. Neither is the same as an analytics request made with consent, so they are counted apart, and a cookieless ping is never called clean.
  const LOADER = /googletagmanager\.com\/(gtm\.js|gtag\/js)/i;
  const DENIED = /[?&]gcs=G100(&|$)/i;
  const sends = (state: string) => trackers.filter((t) => TRACKING.test(t.category ?? "") && t.consent_states.includes(state) && !LOADER.test(t.script_src));
  const deniedOnly = (state: string) => { const x = sends(state); return x.length > 0 && x.every((t) => DENIED.test(t.script_src)); };
  const preCookies = trackCookie("pre_consent"), rejectCookies = trackCookie("post_reject");
  const preTracking = preCookies + (trackers.length > 0 ? sends("pre_consent").length : trackReq("pre_consent"));
  const rejectTracking = rejectCookies + (trackers.length > 0 ? sends("post_reject").length : trackReq("post_reject"));
  const preDenied = preCookies === 0 && deniedOnly("pre_consent"), rejectDenied = rejectCookies === 0 && deniedOnly("post_reject");
  const anyTracking = cookies.some((c) => TRACKING.test(c.category ?? "")) || trackers.some((t) => TRACKING.test(t.category ?? ""));
  let c9: Draft;
  if (!cons) c9 = { result: "Failed", completed: false, basis: "Evidence unavailable", explanation: "The consent states were not loaded, so Accept and Reject could not be compared.", limitation: "Consent behaviour could not be assessed." };
  else {
    const known = (n: number) => (trackers.length > 0 ? 0 : n);   // with the request list in hand, what a request carries decides; the summary count only stands in when there is no list
    const preClean = known(cons.known[0]) === 0 && preTracking === 0, rejectClean = known(cons.known[2]) === 0 && (!cons.rejectTested || rejectTracking === 0);
    const line = `Analytics/marketing requests: before consent ${cons.known[0]}, after Accept ${cons.acceptTested ? cons.known[1] : "not tested"}, after Reject ${cons.rejectTested ? cons.known[2] : "not tested"}. Analytics/marketing cookies and requests seen before consent: ${preTracking}.`;
    const home = model.screenshot?.page_url ?? null;
    if (!hasBanner && !anyTracking && unclassified === 0 && cons.known[0] === 0 && cons.known[1] === 0 && cons.known[2] === 0) {
      c9 = { result: "Not Applicable", evidence: line, sourceUrl: home, explanation: "No consent banner was found and every cookie and request observed was classified as necessary or functional (none as analytics, marketing or advertising), so there was no consent interface to test." };
    } else if (!hasBanner) {
      c9 = { result: "Failed", basis: "Not tested", evidence: line, sourceUrl: home, explanation: "Analytics, marketing or advertising cookies or requests were observed, but no consent banner was displayed to the scanner, so Accept and Reject could not be tested.", limitation: "A banner that was not shown to the scanner may still exist." };
    } else if (!cons.acceptTested && !cons.rejectTested) {
      c9 = { result: "Failed", completed: false, basis: "Not tested", evidence: line, sourceUrl: home, explanation: "A banner was found, but neither Accept nor Reject could be clicked and confirmed, so only the state before consent was observed.", limitation: "Accept and Reject were not tested." };
    } else if (cons.acceptTested && cons.rejectTested && preClean && rejectClean) {
      c9 = { result: "Passed", evidence: line, sourceUrl: home, quality: "Limited", coverage: "Homepage only", limitation: "Accept and Reject were tested once, on the homepage. Other pages and other tracking technologies were not tested, so this does not show that every consent requirement is met across the website.", explanation: "All three states were tested on the homepage: no analytics or marketing before consent, and none after Reject." };
    } else if (cons.acceptTested && cons.rejectTested && (preClean || preDenied) && (rejectClean || rejectDenied)) {
      c9 = { result: "Partially Passed", evidence: `${line} Requests that carry data: ${sends("pre_consent").length} before consent and ${sends("post_reject").length} after Reject, sent in consent-denied mode (gcs=G100); no analytics or marketing cookie was set before consent.`, sourceUrl: home,
        explanation: "Google tags sent requests before consent and after Reject in consent-denied mode, and no analytics or marketing cookie was set. The requests still reach Google, so this is not a clean result." };
    } else if (!preClean && !rejectClean) {
      c9 = { result: "Failed", evidence: line, sourceUrl: home, explanation: "Analytics or marketing requests were observed before consent and again after Reject." };
    } else {
      c9 = { result: "Partially Passed", evidence: line, sourceUrl: home, explanation: `${!preClean ? "Analytics or marketing requests were observed before any consent action" : !rejectClean ? "Analytics or marketing requests were still observed after Reject" : "Only one of Accept and Reject could be tested"}.`, limitation: !cons.acceptTested || !cons.rejectTested ? `${!cons.acceptTested ? "Accept" : "Reject"} was not tested.` : null };
    }
  }

  const first = [
    make(1, "Privacy Policy Availability", "DPDP Act s.5(1); Rules r.3(a), 3(b)", "dpdp", c1),
    make(2, "Personal Data Collection Disclosure", "DPDP Act s.5(1)(i); Rules r.3(b)(i)", "dpdp", c2),
    make(3, "Purpose of Data Processing", "DPDP Act s.5(1)(i); Rules r.3(b)(ii)", "dpdp", c3),
    make(4, "Data Sharing and Third Parties", "DPDP Act s.8(2)", "practice", c4),
    make(5, "Data Retention Disclosure", "DPDP Act s.8(7)", "practice", c5),
    make(6, "User Rights Disclosure", "DPDP Act s.11, 12, 13, 14; Rules r.14(1)", "dpdp", c6),
    make(7, "Contact and Grievance Mechanism", "DPDP Act s.8(9), 8(10), 13; Rules r.9, 14(3)", "dpdp", c7),
    make(8, "Cookie and Tracker Detection", "Relates to DPDP Act s.6(1); Rules r.3", "technical", c8),
    make(9, "Consent Behaviour", "Relates to DPDP Act s.6(1), 6(4); Rules r.3(c)(i)", "technical", c9),
  ];

  // 10. evidence and transparency: can each positive finding of checks 1 to 9 be traced to a page the scan actually read? Check 10 is not counted in its own evidence. A finding is traceable when it carries
  // an evidence excerpt or observation AND a source address that is one of the pages the scan read (or a policy document the analysis read). A check that could not be assessed has no result to trace; it
  // is named under the limitations. A completed "not found" result records what was searched, so it is listed apart and is not counted as evidence.
  const norm = (u: string): string => u.toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/[#?].*$/, "").replace(/\/$/, "");
  const readPages = new Set<string>([
    ...(ev?.pages ?? []).filter((p) => p.discovered_via !== "failed" && p.discovered_via !== "timeout" && p.discovered_via !== "robots_disallowed").flatMap((p) => [p.url, p.final_url ?? ""]),
    ...(sp?.rows ?? []).filter((r) => r.scanned).map((r) => r.url), ...(a?.documents ?? []).map((d) => d.url), ...(ev?.policies ?? []).map((p) => p.url), model.screenshot?.page_url ?? "",
  ].filter(Boolean).map(norm));
  const inScan = (u: string | null): boolean => !!u && (readPages.size === 0 || readPages.has(norm(u)));
  const gaps = first.filter((c) => c.result === "Not Assessed").map((c) => c.id);
  const rest = first.filter((c) => c.result !== "Not Applicable" && c.result !== "Not Assessed");
  const positive = rest.filter((c) => c.result === "Passed" || c.result === "Partially Passed");
  const absence = rest.length - positive.length;
  const witnessed = positive.filter((c) => c.evidence !== "" && inScan(c.sourceUrl)).length;
  const frac = positive.length === 0 ? 0 : witnessed / positive.length;
  const gapNote = gaps.length ? ` Check${gaps.length === 1 ? "" : "s"} ${gaps.join(", ")} could not be assessed and ${gaps.length === 1 ? "is" : "are"} listed under the limitations.` : "";
  const absNote = absence > 0 ? ` ${absence} completed result${absence === 1 ? " records" : "s record"} only what was searched for and not found, and ${absence === 1 ? "is" : "are"} not counted as evidence.` : "";
  const ofLine = `${witnessed} of ${positive.length} findings (checks 1 to 9; this check is not counted in its own evidence) trace to an evidence excerpt and a page the scan read.`;
  const c10: Draft = !ev
    ? { result: "Failed", completed: false, basis: "Evidence unavailable", explanation: "The scan's evidence was not loaded, so the other results could not be traced to their source.", limitation: "Evidence unavailable." }
    : rest.length === 0
      ? { result: "Failed", completed: false, basis: "Evidence unavailable", explanation: `None of the other checks could be assessed, so there is no result to trace to its source.${gapNote}`, limitation: "No check could be assessed." }
      : positive.length === 0
        ? { result: "Failed", evidence: "", explanation: `No finding rests on positive evidence: every completed result records only that something was not found.${absNote}${gapNote}`, limitation: failedPages > 0 ? `${failedPages} page${failedPages === 1 ? "" : "s"} did not load.` : null }
        : frac >= 0.8
          ? { result: "Passed", evidence: ofLine, sourceUrl: null, quality: "Specific", explanation: `Each finding can be traced to its excerpt and to a page that was read.${absNote}${gapNote}`, coverage: scanned > 0 && sp?.discovered != null && sp.discovered > scanned ? `${scanned} of ${sp.discovered} pages read` : null }
          : frac >= 0.5
            ? { result: "Partially Passed", evidence: ofLine, quality: "Limited", explanation: `Some findings can be traced to a page that was read; the others carry no excerpt or name no page the scan read.${absNote}${gapNote}`, limitation: failedPages > 0 ? `${failedPages} page${failedPages === 1 ? "" : "s"} did not load.` : null }
            : { result: "Failed", evidence: ofLine, explanation: `Most findings cannot be traced to a page the scan read, so they cannot be independently reviewed. Missing evidence is not proof of a violation.${absNote}${gapNote}`, limitation: failedPages > 0 ? `${failedPages} page${failedPages === 1 ? "" : "s"} did not load.` : null };
  const checks = [...first, make(10, "Privacy Evidence and Transparency", null, "transparency", c10)];

  const sc = scoreChecks(checks);
  const limitations: string[] = [];
  if (pageNote) limitations.push(pageNote);
  if (failedPages > 0) limitations.push(`${failedPages} page${failedPages === 1 ? "" : "s"} did not load (see the page list).`);
  if (skippedPages > 0) limitations.push(`${skippedPages} page${skippedPages === 1 ? " was" : "s were"} found but not scanned.`);
  // Checks that could not be completed, grouped by the limit they share, so one limit is said once.
  const byLimit = new Map<string, number[]>();
  for (const c of checks) if (c.result === "Not Assessed") byLimit.set(c.limitation ?? c.explanation, [...(byLimit.get(c.limitation ?? c.explanation) ?? []), c.id]);
  for (const [text, ids] of byLimit) limitations.push(`Check${ids.length === 1 ? "" : "s"} ${ids.join(", ")} could not be assessed (left out of the score): ${text}`);
  const discovered = sp?.discovered ?? model.visual?.pagesFound ?? null;
  const assessedN = checks.filter((c) => c.max !== null).length;
  const limitedBasis = assessedN < 5;
  const where = scanned > 0 ? (discovered != null && discovered > scanned ? `${scanned} of the ${discovered} pages found` : `${scanned} ${scanned === 1 ? "page" : "pages"}`) : "no page";
  const summary = sc.score === null
    ? "No meaningful score: no check could be assessed on the evidence available. This is not a finding about the website."
    : `${sc.totalText} marks across ${assessedN} assessed check${assessedN === 1 ? "" : "s"}, from ${where} on the entered domain.${sc.counts.notAssessed ? ` ${sc.counts.notAssessed} could not be assessed and ${sc.counts.notAssessed === 1 ? "is" : "are"} left out.` : ""}${limitedBasis ? " Fewer than half the checks could be assessed, so this score is provisional." : ""} Consent was tested once, on the homepage. An indicative readiness reading, not a legal determination or certification.`;

  return {
    checks, awarded: sc.awarded, applicableMax: sc.applicableMax, score: sc.score, counts: sc.counts, formula: sc.formula, totalText: sc.totalText, scoreText: sc.scoreText,
    incomplete: sc.counts.notAssessed, limitedBasis, summary,
    recommendations: checks.filter((c) => c.result === "Partially Passed" || c.result === "Failed").map((c) => ({ id: c.id, text: ADVICE[c.id] })).concat(
      checks.some((c) => c.result === "Not Assessed") ? [{ id: 0, text: "Run the scan again when the pages it could not read are reachable, so the checks left out can be assessed." }] : []),
    pages: { discovered: sp?.discovered ?? model.visual?.pagesFound ?? null, scanned, failed: failedPages, skipped: skippedPages },
    limitations,
  };
}

/** What to do about a check that did not pass in full. Plain advice about the website's own text or behaviour; it never says the law was broken. */
const ADVICE: Record<number, string> = {
  1: "Publish a privacy policy page that is linked from the homepage and can be read by the scan.",
  2: "List each category of personal data collected, including data collected automatically.",
  3: "State the purpose for which each category of personal data is used.",
  4: "Name the kinds of recipients data is shared with (processors, analytics and advertising providers, authorities).",
  5: "State a retention period or criterion for every category of personal data, not for one.",
  6: "Explain each right (access, correction and erasure, nomination) and how to exercise it.",
  7: "Give a grievance contact, a response period and a Data Protection Officer or business contact.",
  8: "Review the cookies and trackers observed and classify each as necessary, analytics or advertising.",
  9: "Confirm that analytics and advertising stay off before consent and after Reject, on every page with a banner.",
  10: "Make sure each finding cites the page and the passage it came from.",
};

/** The words printed beside a check for its kind. A technical observation or a best practice is never presented as a required DPDP item. */
export const KIND_LABEL: Record<TenKind, string> = { dpdp: "DPDP-related", practice: "Best practice", technical: "Technical check", transparency: "Report transparency" };
