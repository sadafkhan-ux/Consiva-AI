/**
 * The assessment and the Privacy Readiness Score, as the server serves them (GET /api/v1/consent/scans/{id}/assessment).
 *
 * There is no scoring in the browser. The one engine lives on the server (backend app/scoring/domain_score.py, the Scanner Score; the ten-check engine is kept there for rollback), so the web page, the email, the dashboard and every PDF show the same
 * result of the same function on the same stored evidence. This file holds the shape of that result, the words printed beside a check's kind, and the conversion from the API's
 * field names. A page that has no assessment (not loaded yet, or the request failed) shows that, never a number worked out here.
 */
export type TenResult = "Passed" | "Partially Passed" | "Failed" | "Not Assessed" | "Not Applicable";

/** What a check is, so a technical observation is never read as a legal requirement and a best practice is never read as a required item. */
export type TenKind = "dpdp" | "practice" | "technical" | "transparency";
/** How good the evidence behind a result is, apart from whether there is any. */
export type TenQuality = "Specific" | "General" | "Limited" | "None" | "Not assessed" | "n/a";
/** Why the result is what it is. Not found, not tested, evidence unavailable and scan failed are different things and are never merged. */
export type TenBasis =
  | "Verified" | "Partially verified" | "Not evidenced" | "Not found in scanned pages" | "Content extraction failed" | "Partially extracted" | "Access failed" | "Crawling restricted"
  | "Not tested" | "Assessment incomplete" | "Evidence unavailable" | "Scan failed" | "Not applicable";

export interface TenCheck {
  id: number;
  /** The parameter's reference in the Scanner Score ("A1", "B2"); absent under the ten-check rollback. */
  ref?: string;
  /** The scorecard's category; absent for the other engines. */
  category?: string;
  name: string;
  result: TenResult;
  /** 1, 0.5 or 0; null when the check does not apply or could not be assessed. */
  awarded: number | null;
  max: number | null;
  /** The text or technical observation the result rests on ("" when there is none). */
  evidence: string;
  /** The page or request the evidence came from. */
  sourceUrl: string | null;
  explanation: string;
  legal: string | null;
  completed: boolean;
  limitation: string | null;
  /** The marks as printed: "1 / 1", "0.5 / 1", "0 / 1", "Not assessed", or "N/A". */
  marksText: string;
  kind: TenKind;
  basis: TenBasis;
  quality: TenQuality;
  coverage: string | null;
}

/** The whole Scanner Score result as the server serves it under `scanner`: only what the pages read is typed here; the rest passes through untouched. */
export interface ScannerResult {
  version: string;
  mode: "quick" | "deep";
  status: "completed" | "restricted";
  /** A whole number 0 to 100, or null when nothing could be assessed. */
  score: number | null;
  /** Every score is out of 100, for a Quick Scan and a Deep Scan alike. */
  scale: number;
  band: string;
  bandNotes: string[];
  /** Whole percent of the points this scan type can reach that could be assessed. */
  coverage: number;
  confidence: { level: "high" | "medium" | "low"; label: string; reasons: string[] };
  explanation: string;
  disclaimer: string;
  whatWasNotAssessed: string[];
  beyondQuickScan: { ref: string; name: string; points: number }[];
  removed: { ref: string; name: string; points: number; state: string; reason: string }[];
  findings: { ref: string; title: string; outcome: string; severity: string; confidence: string; observed: string; whyItMatters: string; action: string; owner: string; effort: string; consequences: string[] }[];
  /** Statements the Act and Rules relate to a website that the Scoring Mechanism does not score: evidence and an outcome, never marks. */
  indicators?: {
    id: string; name: string; scored: false; outcome: string; found: string;
    evidence: { observed: string; page: string | null; at: string | null }[];
    statutory: { provision: string; summary: string; relation: string; commences: string; inForce: boolean }[];
  }[];
  /** The scorecard's three categories and ten rows (Scorecard engine); each row carries its detailed sub-checks. Absent for the other engines. */
  categories?: { category: string; marks: number; awarded: number; assessable: number }[];
  rows?: { id: string; name: string; outcome: string; subChecks: { id: string; name: string; result: string }[] }[];
  /** Each choice by purpose switched on alone in a fresh session (Deep Scan): what was contacted, and whether that is what the purpose's name says. */
  purposes?: {
    tested: boolean; reason: string | null; requestsOnly: boolean;
    purposes: { kind: string; label: string; observed: { vendor: string | null; category: string; host: string }[]; verdict: "as_expected" | "unexpected" }[];
  } | null;
  rules: Record<string, string>;
  scannedAt: string | null;
  perfectScoreNotice?: { text: string; movesIt: string[]; notCovered: string; recommendedCadence: string; cadenceReason: string };
}

export interface TenAssessment {
  /** Which engine produced this: the Scanner Score, or the ten-check engine after a rollback. */
  engine?: "scorecard" | "scanner" | "ten_check";
  /** The whole Scanner Score result; null or absent under the ten-check rollback. */
  scanner?: ScannerResult | null;
  checks: TenCheck[];
  awarded: number;
  applicableMax: number;
  /** 0 to 100, one decimal; null when no check applies (no meaningful score). */
  score: number | null;
  formula: string;
  /** The marks total as printed: "7.5 / 10", or "No applicable check". */
  totalText: string;
  /** The score as printed everywhere: "94.4/100", or "No meaningful score". */
  scoreText: string;
  counts: { passed: number; partial: number; failed: number; notAssessed: number; notApplicable: number; beyondScanType?: number };
  incomplete: number;
  limitedBasis: boolean;
  summary: string;
  recommendations: { id: number; text: string }[];
  pages: { discovered: number | null; scanned: number; failed: number; skipped: number };
  limitations: string[];
  /** The ten-check methodology version the numbers come from. */
  methodologyVersion: string;
  scanStatus: string;
  /** Where the policy reading stands: done, running, failed or not_run. */
  policyAnalysis: "done" | "running" | "failed" | "not_run";
  /** True only when the scan is complete and the policy reading has finished: a score shown before that could still change. */
  final: boolean;
}

/** The words printed beside a check for its kind. A technical observation or a best practice is never presented as a required DPDP item. */
export const KIND_LABEL: Record<TenKind, string> = { dpdp: "DPDP-related", practice: "Best practice", technical: "Technical check", transparency: "Report transparency" };

const camel = (k: string): string => k.replace(/_([a-z])/g, (_m, c: string) => c.toUpperCase());
const camelKeys = (v: unknown): unknown => {
  if (Array.isArray(v)) return v.map(camelKeys);
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v as Record<string, unknown>).map(([k, x]) => [camel(k), camelKeys(x)]));
  return v;
};

/** The server's assessment in this app's field names. The values are the server's, untouched. Anything that is not an assessment (a wrong response, a changed contract) is refused rather than drawn. */
export function fromApiAssessment(raw: unknown): TenAssessment {
  const a = camelKeys(raw) as Partial<TenAssessment> | null;
  if (!a || typeof a !== "object" || !Array.isArray(a.checks) || !a.counts || typeof a.scoreText !== "string") throw new Error("The response is not a ten-check assessment.");
  return a as TenAssessment;
}

/** What must always be shown together with a Scanner Score (SC-25): the band, the coverage and the evidence confidence. Null under the ten-check rollback. */
export function scoreContext(a: TenAssessment | null): { band: string; coverage: number; confidence: string; text: string } | null {
  const s = a?.scanner;
  if (!s) return null;
  return { band: s.band, coverage: s.coverage, confidence: s.confidence.label, text: `Band: ${s.band} · Coverage ${s.coverage}% · Evidence confidence: ${s.confidence.label}` };
}

/** What the report pages draw when no assessment could be loaded: no checks and no score, said so, never a number worked out here. */
export const NO_ASSESSMENT: TenAssessment = {
  checks: [], awarded: 0, applicableMax: 0, score: null, formula: "", totalText: "No applicable check", scoreText: "No meaningful score",
  counts: { passed: 0, partial: 0, failed: 0, notAssessed: 0, notApplicable: 0 }, incomplete: 0, limitedBasis: true,
  summary: "The assessment could not be loaded, so no Privacy Readiness Score is shown.", recommendations: [],
  pages: { discovered: null, scanned: 0, failed: 0, skipped: 0 }, limitations: [], methodologyVersion: "", scanStatus: "", policyAnalysis: "not_run", final: false,
};
