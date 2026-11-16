// Small display helpers for the Enterprise workspace. They exist so that no screen ever prints "undefined", "NaN" or a raw null.

import type { ParameterState } from "../api/enterprise";

export const NOT_RECORDED = "not recorded yet";

/** A count from the server: `null` (or anything that is not a finite number) is "not recorded yet", never 0. */
export function count(n: number | null | undefined): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString("en-US") : NOT_RECORDED;
}

/** A number with at most one decimal (points). */
export function points(n: number | null | undefined): string {
  return typeof n === "number" && Number.isFinite(n) ? String(Math.round(n * 10) / 10) : NOT_RECORDED;
}

export function percent(n: number | null | undefined): string {
  return typeof n === "number" && Number.isFinite(n) ? `${Math.round(n)}%` : NOT_RECORDED;
}

/** Text that may be missing. */
export function text(s: unknown, fallback = "-"): string {
  if (typeof s === "string") return s.trim() ? s : fallback;
  if (typeof s === "number" && Number.isFinite(s)) return String(s);
  return fallback;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "-" : d.toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function dateOnly(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "-" : d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

export function bytes(n: number | null | undefined): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return NOT_RECORDED;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function words(s: string | null | undefined): string {
  return s ? s.replace(/_/g, " ") : "-";
}

export function asList<T>(v: { items: T[] } | T[] | null | undefined): T[] {
  if (Array.isArray(v)) return v;
  return v && Array.isArray(v.items) ? v.items : [];
}

export interface Tone {
  label: string;
  tone: "ok" | "warn" | "bad" | "info" | "muted";
}

/** How a parameter state looks. Only met, partly met and not met read as a result; the rest are visibly "no result yet" (dashed outline, neutral colour). */
export const PARAM_STATE: Record<ParameterState, Tone & { assessed: boolean }> = {
  met: { label: "Met", tone: "ok", assessed: true },
  partly_met: { label: "Partly met", tone: "warn", assessed: true },
  not_met: { label: "Not met", tone: "bad", assessed: true },
  not_assessed: { label: "Not assessed", tone: "muted", assessed: false },
  needs_records: { label: "Needs records", tone: "muted", assessed: false },
  human_review: { label: "Human review", tone: "info", assessed: false },
  not_applicable: { label: "Not applicable", tone: "muted", assessed: false },
};

export function paramState(state: string): Tone & { assessed: boolean } {
  return PARAM_STATE[state as ParameterState] ?? { label: words(state), tone: "muted", assessed: false };
}

export const COMPLETION_LABEL: Record<string, Tone> = {
  queued: { label: "Queued", tone: "muted" },
  crawling: { label: "Crawling", tone: "info" },
  crawled: { label: "Crawled", tone: "info" },
  analysing: { label: "Analysing", tone: "info" },
  partial: { label: "Partial", tone: "warn" },
  complete: { label: "Complete", tone: "ok" },
  failed: { label: "Failed", tone: "bad" },
  cancelled: { label: "Cancelled", tone: "muted" },
};

export function completionTone(c: string | null | undefined): Tone {
  return (c && COMPLETION_LABEL[c]) || { label: c ? words(c) : "Unknown", tone: "muted" };
}

/** States in which the scan can still change: poll while in one of these. */
export function isLive(completion: string | null | undefined, crawl?: string, analysis?: string): boolean {
  if (completion === "queued" || completion === "crawling" || completion === "analysing") return true;
  return crawl === "queued" || crawl === "running" || analysis === "running";
}
