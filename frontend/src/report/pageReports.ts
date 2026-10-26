import type { ScanEvidenceResponse } from "../api/types";
import type { ReportModel } from "./buildReport";
import { buildHeroView, buildPreviewView, type HeroView, type PreviewView } from "./hero";
import { resolveCta, type Cta } from "./reportCta";
import { isReportCategory } from "./reportScope";
import { pagesCoverageLine } from "./scanPages";
import { checkContribution, stripRuleCodes, topFindingRows, type FindingRow, type RowTone } from "./reportText";
import type { ConsentComparison, ElementView, TechnologyRow } from "./reportVisual";
import { registrableDomain } from "./trackerSummary";

// What the two fixed-page reports (the Free Scan report, two pages, and the Full Scan report, eight pages) draw. Everything is arranged here once from the
// ReportModel the dashboard and the long report already read, so a page never works out a count of its own and the two reports cannot disagree. A value
// the scan does not have stays null or says so in words; nothing is filled in.

export type ReportVariant = "free" | "full";

export const FREE_PAGES = 2;
export const FULL_PAGES = 8;

export interface StatusPillView { label: string; tone: RowTone }
export interface AreaView { name: string; scoreLabel: string; pct: number | null; coverageLabel: string }
export interface FrameworkView {
  name: string;
  /** "94.7%", or "Not available". */
  scoreLabel: string;
  counts: { met: number; partly: number; notMet: number; unverified: number; notApplicable: number };
  /** The five segments of the bar, in order, as a share of 100 (all zero when nothing is counted). */
  segments: { key: string; label: string; count: number; pct: number }[];
}
export interface TechCard { name: string; type: string; requests: number; seen: string; category: string }
export interface CheckRow { id: string; title: string; evidence: string; weight: number; status: StatusPillView; contribution: string }
export interface FixRow { rank: number; title: string; priority: string; tone: RowTone; action: string; related: string }
export interface SeriesPoint { state: string; tested: boolean; analytics: number; functional: number; other: number }

export interface PageReportView {
  variant: ReportVariant;
  domain: string;
  scanId: string | null;
  /** FR-20261022-7843 (Free) or FS-20261022-7843 (Full): the date of the scan and the first four characters of its id, so it is the scan's own and repeats. */
  reportId: string;
  scannedAt: string;
  /** "5 of 112 pages scanned": the page header's pages-scanned words, the same as the flowing pages' print header. */
  headerPages: string;
  hero: HeroView;
  preview: PreviewView;
  coverageLabel: string;
  coveragePct: number | null;
  testedLine: string;
  tiles: { value: string; label: string }[];
  framework: FrameworkView;
  areas: AreaView[];
  consent: ConsentComparison | null;
  techCards: TechCard[];
  techMore: number;
  /** Technologies beyond the ones the Full report's table lists (it lists up to 8; the Free cards list 5). */
  techRowsMore: number;
  techRows: TechCard[];
  domains: { shown: string[]; others: number };
  series: SeriesPoint[];
  elements: ElementView[];
  findings: FindingRow[];
  findingsMore: number;
  checks: CheckRow[];
  checksMore: number;
  fixes: FixRow[];
  cta: Cta;
  /** The Full report's last-page context: the dates on which duties start, the finding groups in one line, and the policies and forms the scan found. */
  upcoming: string[];
  groupCounts: string | null;
  policies: { type: string; url: string }[];
  forms: { title: string; fields: string }[];
  /** Which of the pages the report's own limits leave unsaid: the plain words under "Scope and limits". */
  limits: string[];
  /** Quick numbers for the cover of the Full report. */
  pagesLine: string;
}

const STATES = ["pre_consent", "post_accept", "post_reject"] as const;
const STATE_LABEL = { pre_consent: "Before Consent", post_accept: "After Accept", post_reject: "After Reject" } as const;

export const verdictPill = (verdict: string): StatusPillView => {
  switch (verdict) {
    case "Pass": return { label: "Pass", tone: "good" };
    case "Partial": return { label: "Partially met", tone: "warn" };
    case "Fail": return { label: "Not met", tone: "bad" };
    case "N/A": return { label: "Not applicable", tone: "mute" };
    default: return { label: "Could not verify", tone: "mute" };
  }
};

/** The pill beside a top finding: the finding's own outcome in the scope's status words. */
export function findingPill(f: FindingRow): StatusPillView {
  if (/partial/i.test(f.label)) return { label: "Partially met", tone: "warn" };
  if (/not met/i.test(f.label)) return { label: "Not met", tone: "bad" };
  if (/review/i.test(f.label)) return { label: "Requires review", tone: "warn" };
  if (/verify/i.test(f.label)) return { label: "Could not verify", tone: "mute" };
  return { label: f.label, tone: f.tone };
}

/** "22 Oct 2026, 20:08 UTC" to 20261022. Null when the text is not a date. */
function compactDate(text: string): string | null {
  const t = Date.parse(text.replace(/,/g, "").replace(/ UTC$/, " UTC"));
  if (Number.isNaN(t)) return null;
  const d = new Date(t);
  return `${d.getUTCFullYear()}${String(d.getUTCMonth() + 1).padStart(2, "0")}${String(d.getUTCDate()).padStart(2, "0")}`;
}

export function reportIdFor(variant: ReportVariant, scanId: string | null, scannedAt: string): string {
  const prefix = variant === "free" ? "FR" : "FS";
  const day = compactDate(scannedAt);
  const tail = (scanId ?? "").replace(/-/g, "").slice(0, 4).toUpperCase();
  return [prefix, day, tail].filter(Boolean).join("-");
}

function seriesFrom(ev: ScanEvidenceResponse | null, consent: ConsentComparison | null): SeriesPoint[] {
  const tested = (s: (typeof STATES)[number]) => (s === "pre_consent" ? true : s === "post_accept" ? !!consent?.acceptTested : !!consent?.rejectTested);
  return STATES.map((s) => {
    const row: SeriesPoint = { state: STATE_LABEL[s], tested: tested(s), analytics: 0, functional: 0, other: 0 };
    if (!ev || !row.tested) return row;
    for (const t of ev.trackers) {
      if (!t.consent_states.includes(s)) continue;
      if (t.category === "analytics" || t.category === "marketing") row.analytics += 1;
      else if (t.category === "functional") row.functional += 1;
      else row.other += 1;
    }
    return row;
  });
}

const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

export function buildPageReport(model: ReportModel, variant: ReportVariant): PageReportView {
  const v = model.dpdpScore;
  const vis = model.visual;
  const ev = model.evidence;
  const hero = buildHeroView(v);
  const preview = buildPreviewView(model.screenshot, model.siteUrl, model.scanId);
  const scannedAt = v ? v.scanTime : model.generatedAt;
  const assessed = v ? v.assessed : [];
  const unable = v ? v.unableToVerify : [];
  const count = (k: string) => assessed.filter((r) => r.statusKey === k).length;
  const counts = { met: count("met"), partly: count("partially_met"), notMet: count("not_met"), unverified: unable.length, notApplicable: v ? v.excluded.length : 0 };
  const total = counts.met + counts.partly + counts.notMet + counts.unverified + counts.notApplicable;
  const seg = (key: string, label: string, n: number) => ({ key, label, count: n, pct: total ? (n / total) * 100 : 0 });
  const framework: FrameworkView = {
    name: "India DPDP Act, 2023 and Rules, 2025",
    scoreLabel: v && v.scoreValue !== null ? v.headline : "Not available",
    counts,
    segments: [seg("met", "met", counts.met), seg("partly", "partly met", counts.partly), seg("notMet", "not met", counts.notMet), seg("unverified", "could not be verified", counts.unverified), seg("na", "not applicable", counts.notApplicable)],
  };

  const areas: AreaView[] = v
    ? v.categories.filter((c) => isReportCategory(c.name)).slice(0, 4).map((c) => {
        const pct = c.score === "Not assessed" ? null : parseFloat(c.score);
        return { name: c.name, scoreLabel: pct === null ? "Not assessed" : c.score, pct, coverageLabel: pct === null ? "Cannot be tested from outside" : c.coverage.replace("coverage", "Coverage") };
      })
    : [];

  // One category per service, found through the registrable domain of its requests: the service catalogue's own word, never a guess.
  const serviceCategory = new Map<string, string>();
  for (const s of ev?.third_party_services ?? []) for (const d of s.domains) if (s.category) serviceCategory.set(registrableDomain(`https://${d}`), capital(s.category));
  const domainOf = new Map<string, string>();
  for (const t of ev?.trackers ?? []) domainOf.set(t.vendor ?? registrableDomain(t.script_src), registrableDomain(t.script_src));
  const toCard = (r: TechnologyRow): TechCard => ({ ...r, category: serviceCategory.get(domainOf.get(r.name) ?? "") ?? "Not classified" });
  const allTech = vis ? vis.technologies.rows.map(toCard) : [];
  const techMore = vis ? vis.technologies.more : 0;

  const hosts = [...new Set((ev?.trackers ?? []).map((t) => registrableDomain(t.script_src)))].sort();
  const SHOWN_DOMAINS = 7;
  const consent = vis ? vis.consent : null;

  const rows = topFindingRows(model);
  const findings = rows.slice(0, 3);

  const listedChecks = v ? v.allChecks.filter((c) => isReportCategory(c.category) && c.statusKey !== "not_applicable") : [];
  const CHECK_ROWS = 9;
  const checks: CheckRow[] = listedChecks.slice(0, CHECK_ROWS).map((r) => ({
    id: r.id.replace("DPDP-", ""), title: r.title, evidence: stripRuleCodes(r.evidenceLine), weight: r.weight, status: verdictPill(r.verdict), contribution: checkContribution(r),
  }));

  const fixes: FixRow[] = v
    ? v.remediationPlan.slice(0, 4).map((f) => ({
        rank: f.rank, title: f.title, priority: f.priority.replace(/ priority$/i, ""), tone: f.timing === "Current" ? "bad" : f.priority.startsWith("High") ? "warn" : f.priority.startsWith("Low") ? "mute" : "warn",
        action: f.action, related: f.title,
      }))
    : [];

  // The score rates a finding High, Medium or Low (scoring/findings.py); it has no "Critical". A confirmed finding rated High is the one that outranks a high score.
  const critical = !!v?.groups.find((g) => g.key === "confirmed")?.entries.some((e) => /critical|high/i.test(e.severity));
  const cta = resolveCta({ score: v ? v.scoreValue : null, coverage: v ? v.coverageValue : null, unassessed: unable.length, critical, subscriber: variant === "full" });

  const pagesLine = vis ? (vis.pagesFound !== null && vis.pagesFound >= vis.pagesScanned ? `${vis.pagesScanned} of ${vis.pagesFound}` : String(vis.pagesScanned)) : "Not recorded";
  const tiles: { value: string; label: string }[] = vis
    ? [
        { value: pagesLine, label: vis.pageLimit ? `Pages scanned (${variant === "free" ? "Free scan limit" : "limit"} ${vis.pageLimit})` : "Pages scanned" },
        { value: String(vis.statesTested), label: "Consent states tested" },
        { value: String(vis.requests), label: "Third-party requests" },
        { value: String(vis.knownRequests), label: "Analytics and marketing requests" },
        { value: String(vis.cookies), label: "Cookies set" },
        { value: String(vis.domains), label: "Third-party domains" },
      ]
    : model.counts.slice(0, 6).map((c) => ({ value: c.value, label: c.label }));

  const limits: string[] = [];
  if (vis) {
    limits.push(`${vis.pagesScanned} ${vis.pagesScanned === 1 ? "page was" : "pages were"} scanned${vis.pagesFound !== null && vis.pagesFound > vis.pagesScanned ? ` of ${vis.pagesFound} found on the main domain` : ""}. This is not an assessment of the whole site.`);
    limits.push(`Accept and Reject were tested once, on the homepage only${vis.consent.acceptTested && vis.consent.rejectTested ? "" : "; at least one of them could not be tested"}.`);
  }
  limits.push("A request shows the browser contacted a service. It does not show that personal data was sent or that a rule was broken.");
  if (v && unable.length) limits.push(`${unable.length} applicable ${unable.length === 1 ? "check" : "checks"} could not be tested from outside and ${unable.length === 1 ? "is" : "are"} not counted as passed.`);

  return {
    variant, domain: preview.domain, scanId: model.scanId, reportId: reportIdFor(variant, model.scanId, scannedAt), scannedAt,
    headerPages: model.scanPages ? model.scanPages.coverageLine : vis ? pagesCoverageLine(vis.pagesScanned, vis.pagesFound) : "", hero, preview,
    coverageLabel: v ? v.coverageHeadline : "n/a", coveragePct: v ? v.coverageValue : null,
    testedLine: v ? `${assessed.length} of ${assessed.length + unable.length} applicable checks tested from outside.` : "",
    tiles, framework, areas, consent, techCards: allTech.slice(0, 5), techMore: techMore + Math.max(0, allTech.length - 5), techRowsMore: techMore, techRows: allTech,
    domains: { shown: hosts.slice(0, SHOWN_DOMAINS), others: Math.max(0, hosts.length - SHOWN_DOMAINS) },
    series: seriesFrom(ev, consent), elements: vis ? vis.elements : [],
    findings, findingsMore: Math.max(0, rows.length - findings.length),
    checks, checksMore: Math.max(0, listedChecks.length - checks.length), fixes, cta, limits, pagesLine,
    upcoming: v ? v.upcomingLines : [], groupCounts: v ? v.groupCountsLine : null,
    policies: (ev?.policies ?? []).map((p) => ({ type: p.policy_type.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()), url: p.url })),
    forms: (ev?.forms ?? []).map((f, i) => ({ title: `Form ${i + 1}${f.purpose_guess ? `: ${f.purpose_guess.replace(/_/g, " ")}` : ""}`, fields: f.fields.length ? `${f.fields.slice(0, 6).map((x) => x.name || "(unnamed)").join(", ")}${f.fields.length > 6 ? ` and ${f.fields.length - 6} more` : ""}` : "No fields recorded" })),
  };
}
