import type { CrawlConfig } from "../api/types";

/**
 * The crawl controls of a whole-site scan, checked in the browser before anything is sent. The server checks them again and clamps each to its own ceiling:
 * this only saves a round trip and says what is wrong. The ranges below match the API schema (api/v1/schemas/consent_agent.py ScanOptions).
 */
export interface CrawlField { key: keyof CrawlConfig; label: string; hint: string; min: number; max: number; step?: number; integer: boolean }

export const CRAWL_FIELDS: CrawlField[] = [
  { key: "max_depth", label: "Maximum crawl depth", hint: "Link levels from the entered address (0 = that page only). Empty = no limit.", min: 0, max: 1000, integer: true },
  { key: "max_urls", label: "Maximum URLs", hint: "Most distinct URLs to record. Empty = the server's default.", min: 1, max: 10_000_000, integer: true },
  { key: "concurrency", label: "Overall concurrency", hint: "Pages loaded at once.", min: 1, max: 256, integer: true },
  { key: "per_domain_concurrency", label: "Per-domain concurrency", hint: "Pages loaded at once from the site. Never above overall concurrency.", min: 1, max: 256, integer: true },
  { key: "requests_per_second", label: "Requests per second", hint: "Page loads started per second against the site.", min: 0.1, max: 1000, step: 0.1, integer: false },
  { key: "memory_budget_mb", label: "Memory budget (MB)", hint: "Pause loading pages while the scanner uses more than this. 0 or empty = no budget.", min: 0, max: 1_048_576, integer: true },
];

export type CrawlForm = Partial<Record<keyof CrawlConfig, string>>;

export interface CrawlValidation { value: CrawlConfig; errors: Partial<Record<keyof CrawlConfig, string>>; ok: boolean }

/** Turns what was typed into a request. An empty field is "not set"; anything else must be a number inside its range. */
export function validateCrawlConfig(form: CrawlForm): CrawlValidation {
  const value: CrawlConfig = {};
  const errors: CrawlValidation["errors"] = {};
  for (const f of CRAWL_FIELDS) {
    const raw = (form[f.key] ?? "").trim();
    if (raw === "") continue;
    const n = Number(raw);
    if (!Number.isFinite(n)) errors[f.key] = "Enter a number.";
    else if (f.integer && !Number.isInteger(n)) errors[f.key] = "Enter a whole number.";
    else if (n < f.min) errors[f.key] = `Must be at least ${f.min}.`;
    else if (n > f.max) errors[f.key] = `Must be at most ${f.max.toLocaleString("en-IN")}.`;
    else value[f.key] = n;
  }
  if (value.per_domain_concurrency != null && value.concurrency != null && value.per_domain_concurrency > value.concurrency && !errors.per_domain_concurrency) {
    errors.per_domain_concurrency = "Cannot be higher than overall concurrency.";
    delete value.per_domain_concurrency;
  }
  return { value, errors, ok: Object.keys(errors).length === 0 };
}
