import type { EvidenceConsentSignalItem } from "../api/types";

/** A consent tool whose script is on the page but whose banner the scanner never saw, and neither Accept nor Reject was clicked. The same test the backend applies (consent_rules.py).
 *  A scan stored before the banner-observation flag existed has no `banner_ui_observed`, which reads as unknown, not as "not displayed". */
export function bannerNotDisplayed(signal: EvidenceConsentSignalItem | null | undefined): boolean {
  if (!signal || signal.mechanism_type !== "cmp") return false;
  if (signal.banner_displayed !== undefined) return signal.banner_displayed === false;     // the backend decided it; the test below is only for an API that does not say
  const e = signal.evidence ?? {};
  return e.banner_ui_observed === false && e.accept_interaction !== "clicked" && e.reject_interaction !== "clicked";
}

/** The consent tool's own script failing to load (the backend records it: `cmp_script_failure`) is why no banner appeared; null when it did not fail. */
export function scriptFailure(signal: EvidenceConsentSignalItem | null | undefined): { url: string; error: string } | null {
  const f = signal?.evidence?.cmp_script_failure as { url?: unknown; error?: unknown } | undefined;
  return f && typeof f.url === "string" && f.url ? { url: f.url, error: typeof f.error === "string" && f.error ? f.error : "the request failed" } : null;
}

/** The sentence for a report: why no banner appeared, when the tool's script did not load. Empty otherwise. */
export function scriptFailureNote(signal: EvidenceConsentSignalItem | null | undefined): string {
  const f = scriptFailure(signal);
  return f ? ` Its script (${f.url}) could not be loaded (${f.error}), which is why no banner appeared.` : "";
}

/** What the banner is said to offer (reject-all, per-purpose choices) is a fact about a banner that was seen. When none was displayed it is not observed, whatever an older scan recorded. */
export function observedBannerFact(signal: EvidenceConsentSignalItem | null | undefined, value: boolean | null | undefined): boolean | null {
  if (bannerNotDisplayed(signal)) return null;
  return value ?? null;
}

/** The text for such a fact in a table: Yes / No when it was observed, and the reason it was not when it was not. */
export function bannerFactText(signal: EvidenceConsentSignalItem | null | undefined, value: boolean | null | undefined): string {
  if (bannerNotDisplayed(signal)) return "Not observed: no banner was displayed to the scanner";
  return value === null || value === undefined ? "Not recorded" : value ? "Yes" : "No";
}
