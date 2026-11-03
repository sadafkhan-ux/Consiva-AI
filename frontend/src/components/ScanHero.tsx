import type { ScanScreenshot } from "../api/types";
import { buildHeroView, buildPreviewView } from "../report/hero";
import type { ScoreView } from "../report/dpdpScore";

/** The ring: the score as an arc round a circle, the percentage inside it. Drawn from the one number the server sent. */
export function ScoreRing({ percent, label }: { percent: number | null; label: string }) {
  const R = 44;
  const C = 2 * Math.PI * R;
  // The words inside the ring: a longer figure ("45.5%") gets a smaller size so it keeps clear of the ring, and the baseline sits half a capital's
  // height below the centre, so the text is centred in the circle whatever its size.
  const size = percent === null ? 13 : label.length >= 5 ? 18 : 22;
  const filled = percent === null ? 0 : (Math.min(100, Math.max(0, percent)) / 100) * C;
  return (
    <svg viewBox="0 0 110 110" width="124" height="124" role="img" aria-label={percent === null ? "No score" : `Compliance score ${label}`} data-testid="hero-ring">
      <circle cx="55" cy="55" r="52" fill="none" stroke="var(--ring-edge, #05070d)" strokeWidth="4" />
      <circle cx="55" cy="55" r={R} fill="none" stroke="var(--ring-track, #26324a)" strokeWidth="9" />
      {filled > 0 && (
        <circle cx="55" cy="55" r={R} fill="none" stroke="var(--ring-arc, #7a4dff)" strokeWidth="9" strokeLinecap="round"
          strokeDasharray={`${filled} ${C}`} transform="rotate(-90 55 55)" data-testid="hero-ring-arc" />
      )}
      <text x="55" y={55 + size * 0.36} textAnchor="middle" fontSize={size} fontWeight="700" fill="currentColor" data-testid="hero-percent">{label}</text>
    </svg>
  );
}

/**
 * The top of the results: the compliance score at the left, what it means beside it, and at the right the scanned site's domain above a picture
 * of its homepage as the scanner saw it, so a reader can see at a glance that the right site was scanned. The words come from report/hero.ts,
 * the same module the PDF reads. Without a picture the same space says plainly why, in client-safe words.
 */
/** The 10-check Privacy Readiness Score (report/tenChecks.ts): the one the summary PDF prints, so the dashboard and the PDF show the same number for the same scan. */
export interface TenScoreView { score: number | null; totalText: string; scoreText: string; passed: number; applicable: number }

export function ScanHero({ score, siteUrl, screenshot, scanId = null, tenScore = null }: { score: ScoreView | null; siteUrl: string; screenshot: ScanScreenshot | null | undefined; scanId?: string | null; tenScore?: TenScoreView | null }) {
  if (!siteUrl) return null;
  const hero = buildHeroView(score);
  const preview = buildPreviewView(screenshot, siteUrl, scanId);
  return (
    <section className="section scan-hero" data-testid="scan-hero">
      <div className="hero-score" data-testid="hero-score">
        <ScoreRing percent={tenScore ? tenScore.score : hero.percent} label={tenScore ? (tenScore.score === null ? "" : String(tenScore.score)) : hero.percentLabel} />
        <div className="hero-score-label" data-testid="hero-score-label">{tenScore ? `Privacy Readiness Score: ${tenScore.scoreText} (${tenScore.totalText})` : hero.scoreLabel}</div>
      </div>
      <div className="hero-text">
        <span className={`badge ${tenScore ? (tenScore.passed === tenScore.applicable ? "ok" : tenScore.passed === 0 ? "neutral" : "warn") : hero.tone === "good" ? "ok" : hero.tone === "warn" ? "warn" : "neutral"}`} data-testid="hero-band">{tenScore ? (tenScore.score === null ? "No meaningful score" : `${tenScore.passed} of ${tenScore.applicable} checks passed`) : hero.band}</span>
        <p className="hero-headline" data-testid="hero-headline">{tenScore ? "The 10-check assessment, the same score the summary PDF prints. It is an internal readiness measure, not a certified DPDP compliance score." : hero.headline}</p>
      </div>
      <figure className="hero-preview" data-testid="hero-preview">
        <figcaption className="hero-domain" data-testid="hero-domain">{preview.domain}</figcaption>
        {preview.src ? (
          <img src={preview.src} alt={`Homepage of ${preview.domain} as the scanner saw it`} width={preview.width} height={preview.height} data-testid="hero-screenshot" />
        ) : (
          <div className="hero-preview-missing" data-testid="hero-preview-missing">
            <b>Homepage preview not available</b>
            <span>{preview.unavailable}</span>
          </div>
        )}
        <div className="muted small" data-testid="hero-caption">{preview.caption}</div>
        {preview.address && <div className="muted small hero-address" data-testid="hero-address">{preview.address}</div>}
      </figure>
    </section>
  );
}
