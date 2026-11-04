/**
 * The one way a score is written everywhere it appears (dashboard, every PDF, the breakdown panel, comparisons): the number out of 100, to at most one decimal, with no percent sign.
 * "94.4/100". A score that is not available is words, never 0.
 */
export const fmtNum = (n: number): string => String(Math.round(n * 100) / 100);

export const formatScore = (score: number | null | undefined, unavailable = "No meaningful score"): string => (score === null || score === undefined ? unavailable : `${fmtNum(score)}/100`);

/** The score a ring draws inside it: the number alone, because the ring prints "/100" itself. Accepts a formatted score ("94.4/100", "94.4%") or a bare number. */
export const ringNumber = (label: string): string => label.replace(/(\/100|%)$/, "");
