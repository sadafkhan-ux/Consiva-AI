// Small wording helpers shared by the PDF drawn in the browser (downloadReport.ts) and the printable report (report/print/).

/** A hidden internal rule code is never shown to a reader: "... (rule R-001)". */
export const stripRuleCodes = (t: string): string => t.replace(/\s*\(rule R-\d+\)/g, "");
/** "DPDP-C1 No analytics ..." shows without its check id where the id has its own column. */
export const stripId = (t: string): string => t.replace(/^DPDP-\w+\s+/, "");
/** Review items about legal timing or which duties apply stay on the dashboard, not in the report. */
export const LEGAL_ONLY_REVIEW: ReadonlySet<string> = new Set(["not-yet-in-force", "entity-applicability"]);

import type { ReportModel } from "./buildReport";

/** The most pages the report's own table lists (a scan is limited to 5; an older one can hold more). */
export const MAX_LISTED_PAGES = 8;

/** Where the call-to-action buttons point. Placeholders until the product team supplies the real destinations. */
export const CTA_URLS = {
  improve: "https://consiva.ai",
  plan: "https://consiva.ai",
  platform: "https://consiva.ai",
};

export type RowTone = "good" | "warn" | "mute" | "bad";
export interface FindingRow { title: string; detail: string; label: string; tone: RowTone }

/** Every finding the summary can show, in the order it shows them: confirmed, future readiness, needs review, could not verify. The report
 *  takes the first three and counts the rest. One function for the PDF drawn in the browser and the report the server prints. */
export function topFindingRows(model: ReportModel): FindingRow[] {
  const v = model.requirements;
  const rows: FindingRow[] = [];
  if (!v) {
    for (const p of model.problems) rows.push({ title: p.text, detail: "", label: p.risk, tone: "warn" });
    return rows;
  }
  const detailOf = (e: { observed: string[]; explanation: string; missing: string[] }, unable: boolean) =>
    stripRuleCodes(unable ? (e.missing[0] ?? e.explanation) : (e.observed[0] ?? e.explanation));
  const group = (k: string) => v.groups.find((g) => g.key === k)?.entries ?? [];
  for (const e of group("confirmed")) rows.push({ title: stripId(e.title), detail: detailOf(e, false), label: e.outcome, tone: "bad" });
  for (const e of group("future_readiness")) rows.push({ title: stripId(e.title), detail: detailOf(e, false), label: e.outcome, tone: "warn" });
  for (const e of group("needs_review").filter((x) => !LEGAL_ONLY_REVIEW.has(x.id))) rows.push({ title: stripId(e.title), detail: detailOf(e, false), label: "Needs review", tone: "warn" });
  const business = new Set(v.unableToVerify.filter((r) => r.checkType === "Business check").map((r) => r.id));
  for (const e of group("unable_to_verify").filter((x) => !business.has(x.id))) rows.push({ title: stripId(e.title), detail: detailOf(e, true), label: "Could not verify", tone: "mute" });
  return rows;
}
