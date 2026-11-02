import type { ReportModel } from "./buildReport";
import type { CookieRow } from "./cookieInventory";
import type { ScoreRequirementView } from "./dpdpScore";
import { buildPageReport, findingPill, type PageReportView, type StatusPillView } from "./pageReports";
import { stripRuleCodes, topFindingRows } from "./reportText";
import type { ElementStatus } from "./reportVisual";

// The two-page report: page 1 is the summary (the site, the score and its coverage, the weightage of each assessed check, the pages the scan chose),
// page 2 the findings, the cookies and the evidence. Every figure is read from what the scan and the score recorded: a check's weightage is the weight
// it carries in the score and the part of it the check earned, so the total of the table is the score's own numerator and denominator.
// Nothing is filled in. A thing the scan did not observe is said to be "Not Tested" or "Unable to Verify", never given a plausible value.

export const SUMMARY_PAGES = 2;
export const FINDINGS_SHOWN = 4;
export const COOKIE_ROWS = 12;

/** Where the report invites the reader to run a scan of their own. */
export const SIGNUP_URL = "https://app.consiva.ai/register";
export const SIGNUP_TEXT = "Want to run your own Privacy Readiness Scan?";
export const SIGNUP_SUB = "Check your own website's consent banner, cookies and privacy notice against India's DPDP Act in minutes.";
export const TECH_SHOWN = 4;

/** The words a result is given: nothing else is printed as a status. */
export type StatusWord = "Passed" | "Requires Review" | "Unable to Verify" | "Not Tested" | "Not applicable";

export interface CheckRow {
  n: number;
  id: string;
  title: string;
  category: string;
  /** The Act and the Rules the check rests on, as the score names them. */
  provisions: string;
  status: { label: StatusWord; tone: StatusPillView["tone"] };
  /** True for a check that was tested and so is part of the score. */
  scored: boolean;
  /** Weightage earned and the weightage the check carries; both null for a check that was not assessed. */
  awarded: number | null;
  max: number | null;
  /** One line of what the scan saw (assessed) or why it could not tell / why it does not apply. */
  note: string;
}

export interface PageSelectionRow {
  n: number;
  url: string;
  /** Why the page was chosen (its kind and where it was found), or why the attempt did not load. */
  reason: string;
  status: string;
  scanned: boolean;
}

export interface CookieLine {
  name: string;
  domain: string;
  /** The classifier's result, with the provider when one is recognised: "Analytics · Google Analytics", or "Not classified". */
  classification: string;
  before: string;
  accept: string;
  reject: string;
}

export interface SummaryView {
  page: PageReportView;
  /** Framework readiness, counted over the checkpoints this report covers (the same ones the table lists), so the legend and the table agree. */
  framework: PageReportView["framework"];
  /** Page 1: the checks that were tested, numbered from 1, with their weightage. */
  scoredRows: CheckRow[];
  totalAwarded: number;
  totalMax: number;
  /** Page 2: every applicable check in two groups, then the checks that do not apply. */
  assessedRows: CheckRow[];
  unassessedRows: CheckRow[];
  notApplicableRows: CheckRow[];
  applicableCount: number;
  scoreChip: string;
  pagesScanned: number | null;
  limited: boolean;
  pagesLine: string;
  tiles: { value: string; label: string }[];
  selection: PageSelectionRow[];
  selectionNotes: string[];
  /** Up to COOKIE_ROWS cookies; the page shows as many as it has room for. */
  cookieLines: CookieLine[];
  cookieTotal: number;
  cookieCounts: { found: number; thirdParty: number; beforeConsent: number; afterAccept: number | null; afterReject: number | null; byCategory: { label: string; count: number }[] };
  techRows: { name: string; type: string; requests: number }[];
  techMore: number;
  requests: number | null;
  domains: number | null;
  knownRequests: number | null;
  consentRows: { label: string; cells: [string, string, string] }[];
  /** The checks and findings the scan supports from public content, most important first. A check that needs the organisation's own records is never listed here. */
  findings: { title: string; detail: string; status: { label: StatusWord; tone: StatusPillView["tone"] } }[];
  findingsMore: number;
  /** The website's public privacy elements, each with what the scan could say about it. */
  elements: { title: string; label: string; tone: StatusPillView["tone"]; note: string }[];
  /** The Grievance Officer and the Data Protection Officer / contact, only as far as the website's public content gives them; empty when it gives none. */
  contacts: { title: string; lines: string[]; source: string | null }[];
  limitations: string[];
  nextSteps: string[];
}

// ─── wording ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

const STATUS: Record<string, { label: StatusWord; tone: StatusPillView["tone"] }> = {
  met: { label: "Passed", tone: "good" },
  partially_met: { label: "Requires Review", tone: "warn" },
  not_met: { label: "Requires Review", tone: "warn" },
  unable_to_verify: { label: "Unable to Verify", tone: "mute" },
  not_applicable: { label: "Not applicable", tone: "mute" },
};

/** "4 of 5 points" -> 4. Null for "not scored" or text that is not that sentence. */
export function pointsEarned(points: string): number | null {
  const m = /^([\d.]+) of [\d.]+ points/.exec(points);
  return m ? parseFloat(m[1]) : null;
}

/** 25 -> "25", 4.5 -> "4.5": no trailing zeros, so a whole figure reads as a whole figure. */
export const weightage = (n: number): string => String(Math.round(n * 100) / 100);

const MAX_NOTE = 110;

/** One line for a table cell: cut at a word when long, the cut marked. The full text stays in the long report. */
export function shortLine(text: string, max = MAX_NOTE): string {
  const t = text.replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  const cut = t.lastIndexOf(" ", max - 1);
  return `${t.slice(0, cut > max / 2 ? cut : max - 1).replace(/[\s,;:(["“]+$/, "")}…`;
}

/** "5 (of 112 found)" -> "5 of 112", the way a page reads it at a glance; any other text is left alone. */
export const pagesGlance = (value: string): string => value.replace(/^(\d+) \(of (\d+) found\)$/, "$1 of $2");

/**
 * The provisions of a check as two short lines: "Act: Section 6(1), 6(4)" and "Rules: Rule 3(c)(i)". The score names each as "DPDP Act, 2023 s.6(1)" or "DPDP Rules, 2025 r.3"
 * (the older wording "Act, Section 6(1)" / "Rules, Rule 3" is read too); the same section named twice is listed once, in the order first named. Anything not in either
 * form is kept as it was written, never dropped.
 */
export function provisionLines(provisions: string): { label: string; text: string }[] {
  const act: string[] = [];
  const rules: string[] = [];
  const other: string[] = [];
  for (const raw of provisions.split(";").map((p) => p.trim()).filter(Boolean)) {
    const m = /^(?:DPDP )?(Act|Rules)(?:, \d{4})?(?:,\s*(?:Section|Rule)\s+|\s+(?:s|r)\.\s*)(.+)$/.exec(raw);
    const into = m ? (m[1] === "Act" ? act : rules) : other;
    const value = m ? m[2] : raw;
    if (!into.includes(value)) into.push(value);
  }
  const lines: { label: string; text: string }[] = [];
  if (act.length) lines.push({ label: "Act", text: `Section ${act.join(", ")}` });
  if (rules.length) lines.push({ label: "Rules", text: `Rule ${rules.join(", ")}` });
  if (other.length) lines.push({ label: "", text: other.join("; ") });
  return lines;
}

/**
 * The status word of a checkpoint. A check the scan could have tested from the website and did not (the banner was not shown, so Reject could not be tried) is "Not Tested";
 * a check that rests on something the scan cannot reach (the privacy policy's wording, the organisation's own records) and could not be settled is "Unable to Verify". Neither
 * is ever "Passed".
 */
function statusOf(r: ScoreRequirementView): { label: StatusWord; tone: StatusPillView["tone"] } {
  if (r.statusKey === "unable_to_verify") return r.methodKey === "website_scan" ? { label: "Not Tested", tone: "mute" } : { label: "Unable to Verify", tone: "mute" };
  return STATUS[r.statusKey] ?? STATUS.unable_to_verify;
}

function toRow(r: ScoreRequirementView, n: number): CheckRow {
  const scored = r.statusKey === "met" || r.statusKey === "partially_met" || r.statusKey === "not_met";
  // The evidence is given whole: a reader must be able to see what the status rests on.
  const note = scored ? stripRuleCodes(r.evidenceLine) : stripRuleCodes(r.reason || r.evidenceLine);
  return {
    n, id: r.id.replace("DPDP-", ""), title: r.title, category: r.category, provisions: r.provisions,
    status: statusOf(r), scored,
    awarded: scored ? pointsEarned(r.points) : null, max: scored ? r.weight : null, note: note.replace(/\s+/g, " ").trim(),
  };
}

/** The disclaimer printed at the foot of page 2. */
export const DISCLAIMER =
  "This report reflects publicly accessible website content and technical behaviour observed during the scan. Results are limited to the pages and consent states tested and do not constitute a legal opinion, certification, or guarantee of regulatory compliance.";

// ─── cookies ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

const CATEGORY_WORD: Record<string, string> = { analytics: "Analytics", marketing: "Marketing", functional: "Functional", necessary: "Necessary" };

/** The classifier's result for a cookie, and who provides it when a provider is recognised. "Unclassified" is the classifier's own answer, said as "Not classified". */
export function classificationOf(r: Pick<CookieRow, "category" | "vendor" | "provider">): string {
  const word = CATEGORY_WORD[r.category.toLowerCase()];
  if (!word) return "Not classified";
  const who = r.provider && r.provider !== "Not identified" ? r.provider : r.vendor !== "Not available" ? r.vendor : "";
  return who ? `${word} · ${who}` : word;
}

/** What a cookie did in one consent state, in the report's words: "Set", "Not seen" (the state was tested and the cookie did not appear) or "Not Tested". */
/** A cookie held by a consent pass in which no Accept or Reject was clicked: observed, but not as the result of a choice. */
export const NO_CHOICE = "Seen, no choice made";

export const stateWord = (b: string): string => (b === "Not tested" ? "Not Tested" : b);

// ─── the pages the scan chose ───────────────────────────────────────────────────────────────────────────────────────────────────

/** A reason for a page whose record carries none (a scan read from an older server): only how it was found, which the record says. */
function fallbackReason(via: string | null | undefined): string {
  if (via === "seed") return "The address that was entered: always scanned first.";
  if (via === "sitemap") return "Selected from the pages found in the sitemap.";
  if (via === "link") return "Selected from the links found on a scanned page.";
  return "How this page was found was not recorded.";
}

export const SELECTION_RULE =
  "Order of choice, on the entered domain only: the entered address, privacy and cookie pages, terms, contact and legal notices, sign-up and login, about and product, other pages, blog last; no address is guessed.";

/** The reason a page was chosen, as a short label: its kind and where it was found ("Privacy or cookie page · sitemap"). The sentence the scan records is longer; this keeps its meaning. */
export function shortReason(reason: string): string {
  const found = /found in the sitemap/i.test(reason) ? "sitemap" : /through a link/i.test(reason) ? "link" : "";
  if (/^The address that was entered/i.test(reason)) return "Entered address (always first)";
  const kind = reason.split(/[:.]/)[0].trim();
  return found ? `${kind} · ${found}` : kind;
}

function buildSelection(model: ReportModel): { rows: PageSelectionRow[]; notes: string[] } {
  const sp = model.scanPages;
  if (!sp) return { rows: [], notes: [] };
  const byUrl = new Map((model.evidence?.pages ?? []).map((p) => [p.url, p]));
  const rows = sp.rows.map((r, i) => {
    const ev = byUrl.get(r.url);
    const reason = r.scanned ? shortReason(ev?.selection_reason ?? fallbackReason(ev?.discovered_via)) : `Not scanned: ${r.status.replace("Not scanned: ", "")}`;
    return { n: i + 1, url: r.url.replace(/^https?:\/\//, ""), reason, status: r.status, scanned: r.scanned };
  });
  const notes: string[] = [];
  const found = sp.discovered;
  notes.push(`${sp.scannedCount} scanned${found !== null ? ` of ${found} found` : ""}${sp.pageLimit ? `, limit ${sp.pageLimit}` : ""}.${sp.stopReason ? ` ${sp.stopReason}` : ""}`);
  if (sp.skipped.length) notes.push(`Found but not scanned: ${sp.skipped.map((s) => `${s.count} ${s.reason}`).join("; ")}.`);
  return { rows, notes };
}

// ─── the view ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

const ELEMENT_WORD: Record<ElementStatus, { label: string; tone: StatusPillView["tone"] }> = {
  found: { label: "Found", tone: "good" }, not_found: { label: "Not found", tone: "warn" }, unverified: { label: "Unable to Verify", tone: "mute" }, not_assessed: { label: "Not Tested", tone: "mute" },
};

/** What the policy gave, as against a line saying it gave nothing: only the first is shown to a reader. */
const isAbsence = (value: string): boolean => /^(not found|not assessed|no .* found|not applicable)/i.test(value.trim()) || /not found in the policy/i.test(value);

export function buildSummaryReport(model: ReportModel): SummaryView {
  const page = buildPageReport(model, "free");
  const v = model.dpdpScore;
  const vis = model.visual;
  // The report covers the checkpoints a website scan can test: the website itself and the privacy policy's wording. A checkpoint that rests only on the organisation's own records
  // (security safeguards, breach handling) is not something the agent can check, so it is not part of this report and is not asked of the website. The score is not touched by this.
  const applicable = v ? v.allChecks.filter((c) => c.statusKey !== "not_applicable" && c.methodKey !== "organisation_records") : [];
  const rows = applicable.map((c, i) => toRow(c, i + 1));
  const assessedRows = rows.filter((r) => r.scored).map((r, i) => ({ ...r, n: i + 1 }));
  const unassessedRows = rows.filter((r) => !r.scored).map((r, i) => ({ ...r, n: assessedRows.length + i + 1 }));
  const notApplicableRows = (v ? v.allChecks.filter((c) => c.statusKey === "not_applicable") : []).map((c, i) => toRow(c, i + 1));
  const totalAwarded = assessedRows.reduce((s, r) => s + (r.awarded ?? 0), 0);
  const totalMax = assessedRows.reduce((s, r) => s + (r.max ?? 0), 0);
  const allPassed = assessedRows.length > 0 && assessedRows.every((r) => r.status.label === "Passed");

  const inv = model.cookies;
  const acceptTested = !!vis?.consent.acceptTested;
  const rejectTested = !!vis?.consent.rejectTested;
  const cookieLines: CookieLine[] = inv.rows.slice(0, COOKIE_ROWS).map((r) => ({
    name: r.name, domain: r.domain === "Not available" ? "Not recorded" : r.domain, classification: classificationOf(r),
    // A state in which no choice was made records the same page seen again, not what the site does after a choice: it is never reported as "after" that choice. A cookie
    // the jar held in such a pass is said to have been seen with no choice made; any other cookie is "Not Tested" there.
    before: stateWord(r.behaviour.before),
    accept: acceptTested ? stateWord(r.behaviour.accept) : r.states.includes("post_accept") ? NO_CHOICE : "Not Tested",
    reject: rejectTested ? stateWord(r.behaviour.reject) : r.states.includes("post_reject") ? NO_CHOICE : "Not Tested",
  }));

  const c = page.consent;
  const tested = (ok: boolean, n: number) => (ok ? String(n) : "Not Tested");
  const consentRows: SummaryView["consentRows"] = c
    ? [
        { label: "Cookies observed", cells: [String(c.cookies[0]), tested(c.acceptTested, c.cookies[1]), tested(c.rejectTested, c.cookies[2])] },
        { label: "Analytics/marketing requests", cells: [String(c.known[0]), tested(c.acceptTested, c.known[1]), tested(c.rejectTested, c.known[2])] },
        { label: "Total third-party requests", cells: [String(c.all[0]), tested(c.acceptTested, c.all[1]), tested(c.rejectTested, c.all[2])] },
      ]
    : [];

  const selection = buildSelection(model);

  // Findings: what the website and the scan support. A check that needs the organisation's own records is not a finding about the website (topFindingRows leaves those
  // out), and how many pages were scanned is a limit, which the report states once, under the limitations.
  const allFindings = topFindingRows(model).filter((f) => !/^pages beyond the first/i.test(f.title));
  const findings = allFindings.slice(0, FINDINGS_SHOWN).map((f) => ({ title: f.title, detail: shortLine(f.detail, 170), status: findingStatus(f) }));

  // The Grievance Officer and the Data Protection Officer / contact: only what the website's own public text gives. A section the policy did not answer is left out, not
  // shown as a gap.
  const contacts: SummaryView["contacts"] = model.insights
    .filter((i) => i.key === "grievance" || i.key === "dpo")
    .map((i) => ({
      title: i.key === "grievance" ? "Grievance Officer" : "Data Protection Officer / contact",
      lines: i.facts.filter((f) => !isAbsence(f.value) && f.tone !== "mute").map((f) => `${f.label}: ${f.value}`),
      source: i.quote?.source ?? null,
    }))
    .filter((x) => x.lines.length > 0);

  const nextSteps: string[] = [];
  nextSteps.push(allFindings.length > 0 ? "Review the findings above with the person who looks after the website's privacy." : "Scan again after any change to the website's privacy notice, cookies or consent banner.");
  const missing = page.elements.filter((e) => e.status === "not_found").map((e) => e.title.toLowerCase());
  if (missing.length > 0) nextSteps.push(`Publish what was not found on the pages scanned (${missing.slice(0, 3).join(", ")}).`);
  if (c && (!c.acceptTested || !c.rejectTested)) nextSteps.push("Make the consent banner reachable by the scanner, or test Accept and Reject by hand: they could not be tested in this scan.");
  if (page.limits.some((l) => /not an assessment of the whole site/.test(l))) nextSteps.push("Run a full scan to cover the pages this scan did not reach.");

  const kindCount = (k: string) => applicable.filter((c) => c.statusKey === k).length;
  const counts = { met: kindCount("met"), partly: kindCount("partially_met"), notMet: kindCount("not_met"), unverified: kindCount("unable_to_verify"), notApplicable: notApplicableRows.length };
  const total = counts.met + counts.partly + counts.notMet + counts.unverified + counts.notApplicable;
  const seg = (key: string, label: string, n: number) => ({ key, label, count: n, pct: total ? (n / total) * 100 : 0 });
  const framework: PageReportView["framework"] = {
    ...page.framework, counts,
    segments: [seg("met", "met", counts.met), seg("partly", "partly met", counts.partly), seg("notMet", "not met", counts.notMet), seg("unverified", "could not be verified", counts.unverified), seg("na", "not applicable", counts.notApplicable)],
  };

  return {
    page, framework, scoredRows: assessedRows, totalAwarded, totalMax, assessedRows, unassessedRows, notApplicableRows,
    applicableCount: applicable.length,
    scoreChip: allPassed ? "All assessed checks passed." : page.hero.headline,
    pagesScanned: vis ? vis.pagesScanned : null, limited: !!vis?.pageLimit, pagesLine: pagesGlance(page.pagesLine),
    tiles: page.tiles.map((t, i) => (i === 0 ? { ...t, value: pagesGlance(t.value) } : t)),
    selection: selection.rows, selectionNotes: selection.notes,
    cookieLines, cookieTotal: inv.rows.length,
    cookieCounts: {
      found: inv.summary.found, thirdParty: inv.summary.thirdParty, beforeConsent: inv.summary.beforeConsent,
      afterAccept: acceptTested ? inv.rows.filter((r) => r.behaviour.accept === "Set").length : null,
      afterReject: rejectTested ? inv.summary.afterReject : null,
      byCategory: inv.byCategory.map((g) => ({ label: CATEGORY_WORD[g.label.toLowerCase()] ?? "Not classified", count: g.count })),
    },
    techRows: page.techRows.slice(0, TECH_SHOWN).map((t) => ({ name: t.name, type: t.type, requests: t.requests })),
    techMore: Math.max(0, page.techRows.length - TECH_SHOWN) + page.techRowsMore,
    requests: vis ? vis.requests : null, domains: vis ? vis.domains : null, knownRequests: vis ? vis.knownRequests : null,
    consentRows,
    findings, findingsMore: Math.max(0, allFindings.length - findings.length),
    elements: page.elements.map((e) => ({ title: e.title, ...ELEMENT_WORD[e.status], note: e.status === "not_found" ? "Not found on the pages scanned" : shortLine(e.note ?? "", 60) })),
    contacts,
    limitations: [
      ...page.limits.filter((l) => !/could not be tested from outside/i.test(l)),
      "This report covers the checkpoints a website scan can test: the website's public pages and the privacy policy's wording.",
    ],
    nextSteps,
  };
}

/** The status word of a finding in the report's vocabulary. */
export function findingStatus(f: Parameters<typeof findingPill>[0]): { label: StatusWord; tone: StatusPillView["tone"] } {
  const p = findingPill(f);
  return /verify/i.test(p.label) ? { label: "Unable to Verify", tone: "mute" } : { label: "Requires Review", tone: "warn" };
}
