import type { PolicyAnalysis, PolicyRequirementResult } from "../api/types";
import { formatRef } from "./legalRefs";

// One reading of the privacy-policy content analysis, used by the dashboard section and the PDF alike, so the two cannot disagree about
// what the policy was found to say. Pure: it only arranges what the server returned (backend app/policy) and words the statuses.

/** The four statuses of the checklist, in the words the report uses everywhere. */
export const POLICY_STATUS_LABEL: Record<string, string> = {
  verified: "Quoted",
  partial: "Partially stated",
  not_found: "Not found",
  not_applicable: "Not applicable",
};

export const POLICY_BASIS_LABEL: Record<string, string> = {
  required: "Required by the Act or Rules",
  practice: "Good practice with a duty behind it",
};

export interface PolicyGroupView {
  name: string;
  rows: PolicyRequirementResult[];
}

/** One line of the checklist: the point, its status, whether the law requires the policy to carry it, and where the law says so. */
export interface ChecklistRow {
  id: string;
  title: string;
  group: string;
  statusKey: string;
  status: string;
  basis: string;
  provisions: string;
}

/** A gap in the policy, with what stating it would do to the policy's completeness and why it matters. */
export interface PolicyFixView {
  rank: number;
  row: PolicyRequirementResult;
  /** "Required" or "Good practice". */
  kind: string;
  impactLine: string;
  why: string;
}

export interface PolicyView {
  /** True when a result with requirements exists. */
  available: boolean;
  /** Why there is no analysis ("unavailable"), or null. */
  reason: string | null;
  headline: string;
  summaryLine: string;
  groups: PolicyGroupView[];
  /** Every point of the policy at a glance. */
  checklist: ChecklistRow[];
  /** Every gap, the required ones first and a point not found before one only partly stated. */
  fixes: PolicyFixView[];
  documents: string[];
  notes: string[];
  limitations: string[];
}

const GROUP_ORDER = ["Notice", "Consent", "Rights", "Grievance", "Transparency", "Children"];

export function buildPolicyView(a: PolicyAnalysis | null | undefined): PolicyView | null {
  if (!a || a.status === "not_run") return null;
  const documents = (a.documents ?? []).map((d) => `${d.url} (${d.policy_type.replace(/_/g, " ")}, ${d.characters.toLocaleString("en")} characters${d.truncated ? ", the first part was read" : ""}; ${d.source === "crawl" ? "read during the scan" : "fetched afterwards"})`);
  if (a.status !== "analysed" || !a.summary) {
    return {
      available: false, reason: a.reason ?? "The policy analysis is not available for this scan.", headline: "Privacy policy content analysis not available",
      summaryLine: "", groups: [], checklist: [], fixes: [], documents, notes: a.notes ?? [], limitations: a.limitations ?? [],
    };
  }
  const s = a.summary;
  // Every reference is spelt the same way wherever it is shown (report/legalRefs.ts), so the dashboard and the PDF agree.
  const requirements = a.requirements.map((r) => ({ ...r, provisions: r.provisions.map(formatRef) }));
  const groups = GROUP_ORDER.map((name) => ({ name, rows: requirements.filter((r) => r.group === name) })).filter((g) => g.rows.length > 0);
  const checklist: ChecklistRow[] = groups.flatMap((g) => g.rows).map((r) => ({
    id: r.id, title: r.title, group: r.group, statusKey: r.status, status: POLICY_STATUS_LABEL[r.status] ?? r.status,
    basis: POLICY_BASIS_LABEL[r.basis] ?? r.basis, provisions: r.provisions.join("; "),
  }));

  // What stating a missing point would do to the completeness figure: a point not found would count in full, one only partly stated would add the half it lacks.
  const earned = s.verified + 0.5 * s.partial;
  const missing = requirements.filter((r) => r.status === "not_found" || r.status === "partial");
  const rank = (r: PolicyRequirementResult) => (r.basis === "required" ? 0 : 2) + (r.status === "not_found" ? 0 : 1);
  const fixes: PolicyFixView[] = [...missing].sort((x, y) => rank(x) - rank(y)).map((r, i) => {
    const after = s.applicable ? Math.round(((earned + (r.status === "not_found" ? 1 : 0.5)) / s.applicable) * 1000) / 10 : null;
    return {
      rank: i + 1, row: r, kind: r.basis === "required" ? "Required" : "Good practice",
      impactLine: s.completeness_percent !== null && after !== null
        ? `Stating this would raise the policy's completeness from ${s.completeness_percent}% to ${after}%.` : "",
      why: r.basis === "required"
        ? `The Act or Rules require this to be given or published (${r.provisions.join("; ")}).`
        : `Good practice: a duty stands behind it (${r.provisions.join("; ")}), but the Act and Rules do not say the policy itself must state it.`,
    };
  });

  return {
    available: true,
    reason: null,
    headline: s.completeness_percent === null ? "Policy completeness not assessed" : `Policy completeness ${s.completeness_percent}%`,
    summaryLine: `${s.verified} quoted · ${s.partial} partially stated · ${s.not_found} not found · ${s.not_applicable} not applicable, of ${s.applicable + s.not_applicable} points checked`
      + (s.quotes_rejected === 1 ? ". 1 passage offered by the analysis was not an exact quotation and was rejected"
        : s.quotes_rejected > 1 ? `. ${s.quotes_rejected} passages offered by the analysis were not exact quotations and were rejected` : ""),
    groups,
    checklist,
    fixes,
    documents,
    notes: a.notes ?? [],
    limitations: a.limitations ?? [],
  };
}
