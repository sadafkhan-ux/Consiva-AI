import type { ScanScreenshot } from "../api/types";
import type { ScoreView } from "./dpdpScore";

// The words and facts of the hero at the top of the report (the score, what it means, and the preview of the scanned homepage), worked out once.
// The dashboard and the PDF both read this, so they cannot say different things about the same scan. Pure: it only arranges what the server returned.

/** The score bands. A band is a plain reading of the number, never a legal verdict. */
export function scoreBand(score: number): string {
  if (score >= 80) return "Strong indicators";
  if (score >= 60) return "Progress needed";
  if (score >= 40) return "Improvement required";
  return "Significant gaps";
}

export type HeroTone = "good" | "warn" | "mute";

export interface HeroView {
  scored: boolean;
  /** The score as a number, or null when it is withheld or was not loaded. */
  percent: number | null;
  /** "56.5%", or "No score". */
  percentLabel: string;
  /** "Compliance Score", or "Compliance Score withheld". */
  scoreLabel: string;
  band: string;
  tone: HeroTone;
  headline: string;
  /** "Coverage 64.1%: …", or null when there is no coverage figure. */
  coverageLine: string | null;
}

export function buildHeroView(v: ScoreView | null): HeroView {
  const scoreValue = v ? v.scoreValue : null;
  const coverageValue = v ? v.coverageValue : null;
  const assessed = v ? v.assessed : [];
  const limited = !!v?.caution || (coverageValue !== null && coverageValue < 50);
  const band = !v ? "Score not available" : scoreValue === null ? "Insufficient evidence" : `${scoreBand(scoreValue)}${limited ? " · limited coverage" : ""}`;
  const tone: HeroTone = scoreValue === null ? "mute" : limited || scoreValue < 80 ? "warn" : "good";
  const headline = !v
    ? "The score could not be loaded for this report."
    : scoreValue === null
      ? `No score is shown. ${v.withheldReason ?? "There was not enough evidence."}`
      : `${assessed.length === 1 ? "The 1 check" : `The ${assessed.length} checks`} we could test scored ${v.headline}. ${v.unableToVerify.length ? `${v.unableToVerify.length} more could not be tested from outside, so read the score with the coverage.` : ""}`.trim();
  return {
    scored: !!v && v.scored,
    percent: scoreValue,
    percentLabel: v && v.scored ? v.headline : "No score",
    scoreLabel: v && v.scored ? "Compliance Score" : "Compliance Score withheld",
    band, tone, headline,
    coverageLine: v && coverageValue !== null ? `Coverage ${v.coverageHeadline}: the share of the checks that apply that a scan could test.` : null,
  };
}

/** The host of a scanned address, as entered: no scheme, path, query or trailing slash ("https://www.x.com/a?b" gives "www.x.com"). */
export function siteDomain(url: string): string {
  return url.replace(/^https?:\/\//, "").replace(/[/?#].*$/, "");
}

/** Whether the scanned address is just the site's front door (no path, query or fragment), so the domain already says everything the address does. */
export function isBareHomepage(url: string): boolean {
  try {
    const u = new URL(/^https?:\/\//.test(url) ? url : `https://${url}`);
    return (u.pathname === "/" || u.pathname === "") && !u.search && !u.hash;
  } catch {
    return false;
  }
}

/** "22 Oct 2026, 16:43 UTC". */
export function formatCaptured(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })}, ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" })} UTC`;
}

export interface PreviewView {
  /** The domain, shown above the picture. The scan's own domain (from the server), else the one in the scanned address. */
  domain: string;
  /** The picture as a data address, or null when there is none. */
  src: string | null;
  width: number;
  height: number;
  /** Plain words for the line under the picture. */
  caption: string;
  /** The scanned address, only when it is not just the homepage (so the domain is never repeated as an address). */
  address: string | null;
  /** Why there is no picture, in client-safe words; null when there is one. */
  unavailable: string | null;
}

export const PREVIEW_UNAVAILABLE = "A homepage preview is not available for this scan.";

/** `scanId` is the scan the report is about. A picture that belongs to a different scan is never shown, however it got here. The domain is spelt as the
 *  scan was entered, the same way the page header spells it. */
export function buildPreviewView(shot: ScanScreenshot | null | undefined, siteUrl: string, scanId?: string | null): PreviewView {
  const domain = siteDomain(siteUrl) || (shot?.domain && shot.domain.trim()) || "";
  const address = isBareHomepage(siteUrl) ? null : siteUrl;
  const foreign = !!shot && !!scanId && !!shot.scan_id && shot.scan_id !== scanId;
  if (foreign) return { domain, src: null, width: 16, height: 9, caption: "Homepage preview", address, unavailable: PREVIEW_UNAVAILABLE };
  if (shot && shot.available && shot.data) {
    const when = formatCaptured(shot.captured_at);
    return {
      domain, src: `data:${shot.mime || "image/jpeg"};base64,${shot.data}`, width: shot.width || 1280, height: shot.height || 720,
      caption: when ? `Homepage as scanned · ${when}` : "Homepage as scanned", address, unavailable: null,
    };
  }
  return { domain, src: null, width: 16, height: 9, caption: "Homepage preview", address, unavailable: shot?.message || PREVIEW_UNAVAILABLE };
}
