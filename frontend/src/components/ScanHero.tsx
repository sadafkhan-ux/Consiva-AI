import type { ScanScreenshot } from "../api/types";
import { buildHeroView, buildPreviewView } from "../report/hero";
import type { TenAssessment } from "../report/tenChecks";
import { ringNumber } from "../report/scoreFormat";

/** The ring: the score as an arc round a circle, the number inside it and "/100" beneath, as in the PDF. */
export function ScoreRing({ percent, label }: { percent: number | null; label: string }) {
  const R = 44;
  const C = 2 * Math.PI * R;
  // The words inside the ring: a longer figure ("45.5%") gets a smaller size so it keeps clear of the ring, and the baseline sits half a capital's
  // height below the centre, so the text is centred in the circle whatever its size.
  const shown = percent === null ? label : ringNumber(label);
  const size = percent === null ? 13 : shown.length >= 5 ? 26 : 32;
  const filled = percent === null ? 0 : (Math.min(100, Math.max(0, percent)) / 100) * C;
  return (
    <svg viewBox="0 0 110 110" width="124" height="124" role="img" aria-label={percent === null ? "No score" : `Privacy Readiness Score ${shown} out of 100`} data-testid="hero-ring">
      <circle cx="55" cy="55" r="52" fill="none" stroke="var(--ring-edge, #05070d)" strokeWidth="4" />
      <circle cx="55" cy="55" r={R} fill="none" stroke="var(--ring-track, #26324a)" strokeWidth="9" />
      {filled > 0 && (
        <circle cx="55" cy="55" r={R} fill="none" stroke="var(--ring-arc, #7a4dff)" strokeWidth="9" strokeLinecap="round"
          strokeDasharray={`${filled} ${C}`} transform="rotate(-90 55 55)" data-testid="hero-ring-arc" />
      )}
      <text x="55" y={percent === null ? 55 + size * 0.36 : 58} textAnchor="middle" fontSize={size} fontWeight="700" fill="currentColor" data-testid="hero-percent">{shown}</text>
      {percent !== null && <text x="55" y="76" textAnchor="middle" fontSize="11" fill="currentColor" opacity="0.7">/100</text>}
    </svg>
  );
}

/**
 * The top of the results: the Privacy Readiness Score at the left, what it means beside it, and at the right the scanned site's domain above a picture
 * of its homepage as the scanner saw it, so a reader can see at a glance that the right site was scanned. The words come from report/hero.ts,
 * the same module the PDF reads. Without a picture the same space says plainly why, in client-safe words.
 */
/**
 * `assessment` is the server's ten-check assessment (GET /assessment): its score is the Privacy Readiness Score, the same number the web page, the email and every PDF print. `pending` is true while the scan or its policy reading
 * is still running: the policy-backed requirements are not in yet, so any number would be provisional and could differ from the finished report, and none is shown.
 */
export function ScanHero({ assessment, siteUrl, screenshot, scanId = null, pending = false }: { assessment: TenAssessment | null; siteUrl: string; screenshot: ScanScreenshot | null | undefined; scanId?: string | null; pending?: boolean }) {
  if (!siteUrl) return null;
  const hero = buildHeroView(assessment);
  const preview = buildPreviewView(screenshot, siteUrl, scanId);
  return (
    <div className="scan-hero-wrap">
    <section className="section scan-hero" data-testid="scan-hero">
      <div className="hero-score" data-testid="hero-score">
        <ScoreRing percent={pending ? null : hero.percent} label={pending ? "…" : hero.percentLabel} />
        <div className="hero-score-label" data-testid="hero-score-label">
          {pending ? "Privacy Readiness Score: assessing…" : hero.percent === null ? hero.scoreLabel : `${hero.scoreLabel}: ${hero.percentLabel}`}
        </div>
      </div>
      <div className="hero-text">
        <span className={`badge ${pending ? "neutral" : hero.tone === "good" ? "ok" : hero.tone === "warn" ? "warn" : "neutral"}`} data-testid="hero-band">
          {pending ? "Assessing" : hero.band}
        </span>
        <p className="hero-headline" data-testid="hero-headline">
          {pending ? "The score is shown when the scan and the policy analysis have finished, so it is the same number the PDF report, the web page and the email show." : hero.headline}
        </p>
        {!pending && assessment && assessment.score !== null && <p className="small muted" data-testid="hero-basis">{assessment.summary}</p>}
        {!pending && assessment && assessment.score !== null && !assessment.final && <p className="small muted" data-testid="hero-provisional">The policy reading has not finished or was not run, so these marks could still change.</p>}
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
    </div>
  );
}
