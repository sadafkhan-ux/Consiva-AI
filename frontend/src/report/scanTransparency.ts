import type { ScanEvidenceResponse, StageResponse } from "../api/types";
import { serverSpanMs, summariseStages, unaccountedMs } from "../components/stageTiming";
import type { ScoreView } from "./dpdpScore";
import type { ScanPagesView } from "./scanPages";
import type { ReportVisual } from "./reportVisual";

// What the scan found, tried, finished and could not verify, worked out ONCE from the data the report already holds, so every report says it the same way.
// Nothing is estimated: a figure the scan did not record is null and is said not to be recorded; a percentage is only made when its denominator exists.
//
// Three different "coverages" are kept apart on purpose and never added together:
//   page coverage         pages scanned out of the pages of the domain the scan FOUND (a statement about the crawl);
//   requirement coverage  the share of the applicable requirements' weight the scan could assess (the score's own figure, scoring/dpdp_score.py);
//   consent-state coverage which of Before Consent, After Accept and After Reject were actually exercised.

export interface ScanTiming {
  startedAt: string | null;
  completedAt: string | null;
  totalMs: number | null;
  stages: { stage: string; durationMs: number | null; attempts: number; status: string }[];
  waitingMs: number | null;
}

export type ScoreReasonKind = "scored" | "partial" | "zero_from_failures" | "insufficient" | "unavailable";

export interface ConsentStateRow {
  state: "Before Consent" | "After Accept" | "After Reject";
  /** "tested", "partly tested", "failed", "skipped" or "could not be verified": the application's own words for what happened. */
  status: "tested" | "partly tested" | "failed" | "skipped" | "could not be verified";
  detail: string;
}

export interface TransparencyView {
  pages: {
    discovered: number | null;
    selected: number | null;
    scanned: number | null;
    /** The scan records a page as scanned or not scanned, never as part-scanned. */
    partial: string;
    failed: { url: string; reason: string }[];
    skipped: { reason: string; count: number }[];
    limit: number | null;
    stopReason: string | null;
    distinction: string;
  } | null;
  timing: {
    total: string | null;
    started: string | null;
    completed: string | null;
    stages: { stage: string; label: string; text: string }[];
    waiting: string | null;
  } | null;
  coverage: {
    page: { value: string; definition: string };
    requirement: { value: string; definition: string };
    consentStates: { value: string; definition: string };
    requirements: { assessed: number; notAssessed: number; notApplicable: number; needsReview: number; documentReview: number } | null;
  };
  consent: { banner: string; rows: ConsentStateRow[] };
  scoreReason: { kind: ScoreReasonKind; text: string };
  /** The framed site, when the scanned address only displays another site. */
  framedWrapper: string | null;
}

export const STAGE_LABEL: Record<string, string> = {
  url_validation: "URL validation", website_scan: "Website scan", data_structuring: "Saving the scan evidence", classification: "Cookie and tracker classification",
  evidence_loading: "Loading the evidence", policy_analysis: "Privacy policy analysis", rules_check: "Compliance rules check", rag_retrieval: "Regulatory provision retrieval",
  llm_analysis: "Compliance reasoning", output_validation: "Output validation", findings_generated: "Findings", rules_findings: "Findings from the rules",
  human_review_gate: "Human review gate", audit_saved: "Audit record",
};

/** The scan's own clock: from the first stage's start to the last stage's end, as the server recorded them. Null when the server sent no stages. */
export function buildTiming(stages: StageResponse[], nowMs = Date.now()): ScanTiming | null {
  if (!stages.length) return null;
  const summary = summariseStages(stages);
  const total = serverSpanMs(stages, nowMs);
  const starts = stages.map((s) => s.started_at).filter((x): x is string => !!x).sort();
  const ends = stages.filter((s) => s.status !== "running" && s.status !== "pending").map((s) => s.completed_at).filter((x): x is string => !!x).sort();
  return {
    startedAt: starts[0] ?? null,
    completedAt: ends.length ? ends[ends.length - 1] : null,
    totalMs: total,
    stages: Object.entries(summary).map(([stage, s]) => ({ stage, durationMs: s.durationMs, attempts: s.attempts, status: s.status })),
    waitingMs: unaccountedMs(total, summary),
  };
}

const seconds = (ms: number) => (ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`);

function when(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })}, ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "UTC" })} UTC`;
}

const OUTCOME: Record<string, { status: ConsentStateRow["status"]; text: string }> = {
  cmp_not_found: { status: "skipped", text: "no consent banner was found, so there was nothing to click" },
  cmp_not_automatable: { status: "could not be verified", text: "a consent tool is present but the scanner could not operate it" },
  click_failed: { status: "failed", text: "the control was found but the click did not work" },
  page_unreachable: { status: "failed", text: "the page could not be reached during this pass" },
  not_found: { status: "skipped", text: "no consent banner was found, so there was nothing to click" },
};

/** The framed site named in the scan's evidence, when the scanned address only displays another site (rule R-012). */
export function framedSiteOf(evidence: ScanEvidenceResponse | null | undefined): string | null {
  const sites = evidence?.consent_signals?.[0]?.evidence?.framed_sites;
  return Array.isArray(sites) && typeof sites[0] === "string" ? sites[0] : null;
}

export function buildTransparency(input: {
  scanPages: ScanPagesView | null;
  visual: ReportVisual | null;
  score: ScoreView | null;
  evidence: ScanEvidenceResponse | null;
  timing: ScanTiming | null;
}): TransparencyView {
  const { scanPages: sp, visual: vis, score: v, evidence: ev, timing } = input;
  const signal = ev?.consent_signals?.[0] ?? null;
  const e = (signal?.evidence ?? {}) as Record<string, unknown>;
  const framed = framedSiteOf(ev);

  const pages = sp
    ? {
        discovered: sp.discovered, selected: sp.attempted, scanned: sp.scannedCount,
        partial: "Not recorded: the scan records a page as scanned or not scanned, never as partly scanned.",
        failed: sp.failed, skipped: sp.skipped, limit: sp.pageLimit, stopReason: sp.stopReason,
        distinction: "A discovered page is a page the scan saw a link or sitemap entry for. Only a scanned page contributed evidence: discovered pages are not scanned pages.",
      }
    : null;

  // Consent states.
  const states = {
    before: !!ev && !!vis,
    accept: e.accept_interaction === "clicked",
    reject: e.reject_interaction === "clicked" && e.reject_click_confirmed === true,
  };
  const bannerNotShown = signal?.mechanism_type === "cmp" && e.banner_ui_observed === false && !states.accept && e.reject_interaction !== "clicked";
  const banner = !signal ? "No consent mechanism information was recorded."
    : framed && signal.mechanism_type === "none" ? `No banner was seen on the wrapper page; the framed site (${framed}) may have its own, which was not scanned.`
      : signal.mechanism_type === "none" ? "No consent banner was detected on the scanned pages."
        : bannerNotShown ? `A consent tool${signal.cmp_vendor ? ` (${signal.cmp_vendor})` : ""} is loaded but no banner was shown to the scanner, so Accept and Reject could not be tested.`
          : `A consent mechanism was detected${signal.cmp_vendor ? ` (${signal.cmp_vendor})` : ""}.`;

  const action = (name: "accept" | "reject"): ConsentStateRow => {
    const label = name === "accept" ? "After Accept" : "After Reject";
    const outcome = e[`${name}_interaction`] as string | undefined;
    if (outcome === undefined) return { state: label, status: "could not be verified", detail: "The scan did not record whether this state was exercised." };
    if (outcome === "clicked") {
      if (name === "reject" && e.reject_click_confirmed !== true) {
        return { state: label, status: "partly tested", detail: "Reject was clicked, but the banner did not visibly record the choice, so what happened afterwards is not evidence of what Reject does." };
      }
      return {
        state: label, status: "tested",
        detail: name === "reject" ? "Reject was clicked and the banner confirmed it; what the page did afterwards was observed."
          : "Accept was clicked; what the page did afterwards was observed.",
      };
    }
    const o = OUTCOME[outcome] ?? { status: "could not be verified" as const, text: `outcome: ${outcome}` };
    return { state: label, status: o.status, detail: `Not tested: ${o.text}.${framed ? " The scanned address is a wrapper." : ""}` };
  };
  const rows: ConsentStateRow[] = [
    states.before
      ? { state: "Before Consent", status: "tested", detail: `Observed on ${vis ? `the ${vis.pagesScanned} scanned ${vis.pagesScanned === 1 ? "page" : "pages"}` : "the scanned pages"}, before any choice.` }
      : { state: "Before Consent", status: "could not be verified", detail: "The scan evidence was not loaded." },
    action("accept"), action("reject"),
  ];
  const tested = rows.filter((r) => r.status === "tested").length;

  // Coverage.
  const assessed = v ? v.assessed.length : null;
  const unable = v ? v.unableToVerify.length : null;
  const group = (k: string) => v?.groups.find((g) => g.key === k)?.entries.length ?? 0;
  const requirements = v
    ? {
        assessed: assessed ?? 0, notAssessed: unable ?? 0, notApplicable: v.excluded.length, needsReview: group("needs_review"),
        documentReview: v.unableToVerify.filter((r) => r.checkType === "Business check").length,
      }
    : null;
  const pageValue = !sp ? "Not recorded"
    : sp.discovered === null ? `${sp.scannedCount} scanned; the number of pages found was not recorded`
      : `${sp.scannedCount} of ${sp.discovered} pages found (${(Math.round((sp.scannedCount / Math.max(1, sp.discovered)) * 1000) / 10).toFixed(1)}%)`;
  const coverage = {
    page: { value: pageValue, definition: "Pages scanned out of the pages of the entered domain the scan found. It says how much of the site was visited, not how much of the law was checked." },
    requirement: {
      value: v && v.coverageValue !== null ? `${v.coverageHeadline}${requirements ? ` (${requirements.assessed} assessed, ${requirements.notAssessed} not assessable from outside)` : ""}` : "Not available",
      definition: "The share of the applicable requirements' weight that the scan could assess. It is the score's own coverage figure and has nothing to do with the number of pages.",
    },
    consentStates: { value: `${tested} of 3 states tested`, definition: "Which of Before Consent, After Accept and After Reject were actually exercised and observed." },
    requirements,
  };

  // Why the score is what it is.
  let scoreReason: TransparencyView["scoreReason"];
  if (!v) {
    scoreReason = { kind: "unavailable", text: "The score could not be loaded for this report, so none is shown. This is not a score of zero." };
  } else if (!v.scored) {
    scoreReason = { kind: "insufficient", text: `No score is shown: ${v.withheldReason ?? "too few requirements could be assessed from outside"} This is not a score of zero, and it does not mean the site passed.${framed ? ` The scanned address only displays another site (${framed}).` : ""}` };
  } else if (v.scoreValue === 0) {
    scoreReason = { kind: "zero_from_failures", text: `The score is 0% because all ${assessed} assessed requirement${assessed === 1 ? " was" : "s were"} Not Met. This is a result, not a missing score.` };
  } else if (v.caution) {
    scoreReason = { kind: "partial", text: `${v.caution}${requirements ? ` ${requirements.notAssessed} requirement${requirements.notAssessed === 1 ? " was" : "s were"} not assessed because the evidence was insufficient.` : ""}` };
  } else {
    scoreReason = { kind: "scored", text: `The score rests on ${assessed} assessed requirement${assessed === 1 ? "" : "s"}${v.coverageValue !== null ? ` and ${v.coverageHeadline} requirement coverage` : ""}.` };
  }

  const timingView = timing
    ? {
        total: timing.totalMs === null ? null : seconds(timing.totalMs),
        started: when(timing.startedAt), completed: when(timing.completedAt),
        stages: timing.stages.filter((s) => s.durationMs !== null).map((s) => ({
          stage: s.stage, label: STAGE_LABEL[s.stage] ?? s.stage.replace(/_/g, " "),
          text: `${seconds(s.durationMs as number)}${s.attempts > 1 ? ` (${s.attempts} attempts)` : ""}${s.status === "failed" ? ", failed" : ""}`,
        })),
        waiting: timing.waitingMs === null || timing.waitingMs < 500 ? null : seconds(timing.waitingMs),
      }
    : null;

  return { pages, timing: timingView, coverage, consent: { banner, rows }, scoreReason, framedWrapper: framed };
}
