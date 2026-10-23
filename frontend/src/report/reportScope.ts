/**
 * What every generated report says about itself and what it leaves out. One place, so the PDF cannot drift from the score the server sends
 * (backend scoring/dpdp_score.py DISCLAIMER carries the same text; a test pins the two together).
 */
export const REPORT_DISCLAIMER =
  "This report is for informational purposes only and does not constitute legal advice. It reflects an automated scan of the pages listed, measured against Consiva's own checks, and is not a certification or guarantee of compliance with any law. Consult a qualified legal professional before relying on it.";

/**
 * Compliance areas the report does not show: people's data-rights handling and security / breach practice. A website scan cannot observe
 * either, so they are never assessed. The score, its coverage and its checks are untouched: these duties stay in the counts as not tested.
 */
export const HIDDEN_REPORT_CATEGORIES: ReadonlySet<string> = new Set(["Data principal rights", "Security and breach"]);

export const isReportCategory = (category: string): boolean => !HIDDEN_REPORT_CATEGORIES.has(category);

/** True for a ledger metric name such as "Data principal rights: score". */
export const isReportMetric = (name: string): boolean => ![...HIDDEN_REPORT_CATEGORIES].some((c) => name.startsWith(`${c}:`));

/** Said once in the appendix, so the omitted duties do not make the counts look wrong. */
export const OMITTED_DUTIES_NOTE =
  "Duties that depend on an organisation's own records, such as how requests from people are handled and internal security practice, cannot " +
  "be observed from a website and are not listed here. They are left out of the score and count as not tested in the coverage, so the " +
  "counts above include them.";
