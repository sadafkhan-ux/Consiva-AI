// One way of writing a DPDP reference, wherever the report prints one. The score, the sector notes and the policy analysis each
// carry references in their own short form ("DPDP Act, 2023 s.6(4)", "DPDP Rules r.3(c)(i)", "Fourth Schedule Part A item 3");
// this turns them all into the form a reader expects to see cited: "DPDP Act, 2023, Section 6(4)", "DPDP Rules, 2025, Rule 3(c)(i)",
// "Fourth Schedule, Part A, item 3". It only rewrites how a reference is spelt, never which provision it names.

const ACT = "DPDP Act, 2023";
const RULES = "DPDP Rules, 2025";

/** A full reference: "DPDP Act, 2023, Section 5(1)(i)". Text that is not a recognised reference is returned as it was. */
export function formatRef(ref: string): string {
  return ref
    .replace(/\bDPDP Act(?:, 2023)?,?\s+s\.\s*(\d+[\w()]*)/g, `${ACT}, Section $1`)
    .replace(/\bDPDP Rules(?:, 2025)?,?\s+r\.\s*(\d+[\w()]*)/g, `${RULES}, Rule $1`)
    .replace(/\bDPDP Act(?:, 2023)?,?\s+Section\s+(\d+[\w()]*)/g, `${ACT}, Section $1`)
    .replace(/(?<![\w.])s\.\s*(\d+[\w()]*)/g, "Section $1")   // a bare follow-on in a list: "s.8(7)"
    .replace(/(?<![\w.])r\.\s*(\d+[\w()]*)/g, "Rule $1")      // "r.8(2)"
    .replace(/\b(First|Second|Third|Fourth|Fifth) Schedule\s+Part\s+([A-Z])\b/g, "$1 Schedule, Part $2")
    .replace(/\bPart ([A-Z])\s+items?\s+/g, (m, p) => `Part ${p}, ${/items\s*$/.test(m) ? "items" : "item"} `)
    .replace(/\s{2,}/g, " ")
    .trim();
}

/** The short form for a narrow table column: "Act, Section 5(1)(i)", "Rules, Rule 3(b)(i)". */
export function formatRefShort(ref: string): string {
  return formatRef(ref).replace(new RegExp(ACT.replace(/[.,]/g, "\\$&"), "g"), "Act").replace(new RegExp(RULES.replace(/[.,]/g, "\\$&"), "g"), "Rules");
}

/** A list of references joined for display. */
export const formatRefs = (refs: string[], sep = " · "): string => refs.map(formatRef).join(sep);
