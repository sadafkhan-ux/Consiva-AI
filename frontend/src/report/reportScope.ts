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

const NONE_ASSESSED: ReadonlySet<string> = new Set();

/**
 * A hidden category is shown as soon as one of its checks was actually assessed (a verdict of Met, Partially Met or Not Met, for example from what the privacy
 * policy says): its points are in the score, so the tables must show them. `assessed` is the set from `assessedCategories`. Left out, the category stays hidden.
 */
export const isReportCategory = (category: string, assessed: ReadonlySet<string> = NONE_ASSESSED): boolean => !HIDDEN_REPORT_CATEGORIES.has(category) || assessed.has(category);

/** True for a ledger metric name such as "Data principal rights: score" that the report shows. */
export const isReportMetric = (name: string, assessed: ReadonlySet<string> = NONE_ASSESSED): boolean =>
  ![...HIDDEN_REPORT_CATEGORIES].some((c) => name.startsWith(`${c}:`) && !assessed.has(c));

/** The categories with at least one assessed check (one that counts in the score). */
export const assessedCategories = (checks: readonly { category: string; statusKey: string }[]): ReadonlySet<string> =>
  new Set(checks.filter((c) => c.statusKey === "met" || c.statusKey === "partially_met" || c.statusKey === "not_met").map((c) => c.category));

/** Said once in the appendix, so the omitted duties do not make the counts look wrong. */
export const OMITTED_DUTIES_NOTE =
  "Duties that depend on an organisation's own records, such as how requests from people are handled and internal security practice, cannot " +
  "be observed from a website and are not listed here. They are left out of the score and count as not tested in the coverage, so the " +
  "counts above include them.";

/** The legal context the Full report's last pages state. Each date is from the notifications the product's score is built on (backend scoring/dpdp_score.py,
 *  legal sources): the Act's key provisions and the Board from 13 Nov 2025, the Rules notified as G.S.R. 846(E) of 13 Nov 2025 and corrected by G.S.R. 892(E)
 *  of 10 Dec 2025, and the duties that start after 18 months (13 May 2027). This is context for the reader, not a legal opinion. */
export const CONSIVA_UPDATES = {
  actStatus:
    "The Digital Personal Data Protection Act, 2023 is in force with several key sections notified from 13 November 2025. The Data Protection Board of India has been established and will consist of four members (Gazette notifications dated 13 November 2025). Most duties on data fiduciaries start 18 months later, on 13 May 2027, so a gap in one of them is shown as readiness, not as a breach.",
  cards: [
    { title: "DPDP Act, 2023 (22 of 2023)", status: "Enacted", when: "Key sections from 13 Nov 2025" },
    { title: "Key provisions in force", status: "In force", when: "From 13 Nov 2025" },
    { title: "Data Protection Board of India", status: "Established", when: "4 members" },
    { title: "DPDP Rules, 2025", status: "Notified", when: "G.S.R. 846(E), 13 Nov 2025" },
    { title: "Corrigendum", status: "G.S.R. 892(E)", when: "10 Dec 2025" },
  ],
  scope:
    "This report checks website-observable requirements aligned to the DPDP Act, 2023 and the Rules, 2025 on the latest notifications (13 Nov 2025 and the corrigendum of 10 Dec 2025). It does not assess duties that depend on an organisation's own records.",
} as const;
