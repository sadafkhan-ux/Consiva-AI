import type { ScoreResponse } from "../api/types";

/**
 * The Privacy Readiness Score: ten checks and the one score they give. CALCULATED BY THE BACKEND (backend/app/scoring/ten_checks.py, GET
 * /api/v1/consent/scans/{id}/score), which is the only place a score is worked out; this file only holds its types and turns the API's
 * response into the shape the dashboard and the PDF reports render. To add or change a check, change the backend engine.
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
  /** The score as printed: "75%", or "No meaningful score" when no check applies. */
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

/** The words printed beside a check for its kind. A technical observation or a best practice is never presented as a required DPDP item. */
export const KIND_LABEL: Record<TenKind, string> = { dpdp: "DPDP-related", practice: "Best practice", technical: "Technical check", transparency: "Report transparency" };

/** True for a well-formed score response; anything else (an error body, an older server) is treated as no score. */
export const isScoreResponse = (r: unknown): r is ScoreResponse =>
  !!r && typeof r === "object" && Array.isArray((r as ScoreResponse).checks) && (r as ScoreResponse).checks.length === 10;

/** The backend's score response, in the shape the dashboard and the reports render. */
export function fromScoreResponse(r: ScoreResponse): TenAssessment {
  return {
    checks: r.checks.map((c) => ({
      id: c.id, name: c.name, result: c.result as TenResult, awarded: c.awarded, max: c.max, evidence: c.evidence, sourceUrl: c.source_url,
      explanation: c.explanation, legal: c.legal, completed: c.completed, limitation: c.limitation, marksText: c.marks_text,
      kind: c.kind as TenKind, basis: c.basis as TenBasis, quality: c.quality as TenQuality, coverage: c.coverage,
    })),
    awarded: r.awarded, applicableMax: r.applicable_max, score: r.score, formula: r.formula, totalText: r.total_text, scoreText: r.score_text,
    counts: { passed: r.counts.passed, partial: r.counts.partial, failed: r.counts.failed, notAssessed: r.counts.not_assessed, notApplicable: r.counts.not_applicable },
    incomplete: r.incomplete, limitedBasis: r.limited_basis, summary: r.summary, recommendations: r.recommendations, pages: r.pages, limitations: r.limitations,
  };
}

const CHECK_NAMES = [
  "Privacy Policy Availability", "Personal Data Collection Disclosure", "Purpose of Data Processing", "Data Sharing and Third Parties", "Data Retention Disclosure",
  "User Rights Disclosure", "Contact and Grievance Mechanism", "Cookie and Tracker Detection", "Consent Behaviour", "Privacy Evidence and Transparency",
];
const CHECK_KINDS: TenKind[] = ["dpdp", "dpdp", "dpdp", "practice", "practice", "dpdp", "dpdp", "technical", "technical", "transparency"];

/** What is shown when the score could not be loaded: every check Not Assessed and no score, never a made-up number. */
export const SCORE_UNAVAILABLE: TenAssessment = {
  checks: CHECK_NAMES.map((name, i) => ({
    id: i + 1, name, result: "Not Assessed", awarded: null, max: null, evidence: "", sourceUrl: null, explanation: "The score could not be loaded for this scan.",
    legal: null, completed: false, limitation: "The score could not be loaded.", marksText: "Not assessed", kind: CHECK_KINDS[i], basis: "Evidence unavailable",
    quality: "Not assessed", coverage: null,
  })),
  awarded: 0, applicableMax: 0, score: null, formula: "No check applies to this website, so no meaningful score can be calculated.",
  totalText: "No applicable check", scoreText: "No meaningful score",
  counts: { passed: 0, partial: 0, failed: 0, notAssessed: 10, notApplicable: 0 }, incomplete: 10, limitedBasis: true,
  summary: "No meaningful score: the score could not be loaded for this scan. This is not a finding about the website.",
  recommendations: [], pages: { discovered: null, scanned: 0, failed: 0, skipped: 0 }, limitations: ["The score could not be loaded."],
};
