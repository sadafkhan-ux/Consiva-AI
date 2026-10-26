import type { TransparencyView } from "../scanTransparency";
import "./transparency.css";

const KIND_CLASS = { scored: "tp-good", partial: "tp-warn", zero_from_failures: "tp-bad", insufficient: "tp-mute", unavailable: "tp-mute" } as const;
const STATUS_CLASS: Record<string, string> = { tested: "tp-good", "partly tested": "tp-warn", failed: "tp-bad", skipped: "tp-mute", "could not be verified": "tp-mute" };

/**
 * The scan transparency section, the same in every report: what the scan found, tried, finished and could not verify; how long it took; the three coverages,
 * kept apart; which consent states were actually exercised; and why the score is what it is. Everything is read from the one TransparencyView.
 * `compact` drops the stage timings and the failed and skipped lists, for the fixed pages of the Full report (which lists them in the appendix).
 */
export function ScanTransparency({ view, compact = false }: { view: TransparencyView; compact?: boolean }) {
  const { pages, timing, coverage, consent, scoreReason } = view;
  const req = coverage.requirements;
  return (
    <div className="tp" data-testid="scan-transparency">
      <p className={`tp-reason ${KIND_CLASS[scoreReason.kind]}`} data-testid="tp-score-reason" data-kind={scoreReason.kind}>{scoreReason.text}</p>
      {view.framedWrapper && (
        <p className="tp-frame" data-testid="tp-frame">
          The scanned address only displays another site ({view.framedWrapper}) in a frame. What the scan saw is the wrapper page. A requirement is limited only where its answer
          rests on something not being seen; what the scan did see is kept.
        </p>
      )}
      <div className="tp-grid">
        <section data-testid="tp-pages">
          <h4>Pages</h4>
          {pages ? (
            <dl>
              <dt>Discovered</dt><dd>{pages.discovered ?? "Not recorded"}</dd>
              <dt>Selected for scanning</dt><dd>{pages.selected ?? "Not recorded"}{pages.limit ? ` (limit ${pages.limit})` : ""}</dd>
              <dt>Scanned successfully</dt><dd>{pages.scanned ?? "Not recorded"}</dd>
              <dt>Partly scanned</dt><dd>Not recorded</dd>
              <dt>Failed or inaccessible</dt><dd>{pages.failed.length}</dd>
              <dt>Found but not scanned</dt><dd>{pages.skipped.reduce((n, s) => n + s.count, 0)}</dd>
            </dl>
          ) : <p>The page records were not loaded.</p>}
          {pages && <p className="tp-note">{pages.distinction}</p>}
          {pages?.stopReason && <p className="tp-note">{pages.stopReason}</p>}
          {!compact && pages && pages.failed.length > 0 && (
            <ul className="tp-list">{pages.failed.slice(0, 6).map((f) => <li key={f.url}>{f.url}: {f.reason}</li>)}</ul>
          )}
          {!compact && pages && pages.skipped.length > 0 && (
            <ul className="tp-list">{pages.skipped.map((s) => <li key={s.reason}>{s.count} {s.count === 1 ? "page" : "pages"} {s.reason}</li>)}</ul>
          )}
        </section>
        <section data-testid="tp-coverage">
          <h4>Coverage, three different things</h4>
          <dl>
            <dt>Page coverage</dt><dd>{coverage.page.value}</dd>
            <dt>Requirement coverage</dt><dd>{coverage.requirement.value}</dd>
            <dt>Consent-state coverage</dt><dd>{coverage.consentStates.value}</dd>
          </dl>
          {req && (
            <p className="tp-note" data-testid="tp-requirements">
              {req.assessed} requirement{req.assessed === 1 ? "" : "s"} assessed · {req.notAssessed} not assessed (insufficient evidence) · {req.notApplicable} not applicable ·
              {" "}{req.needsReview} need human review · {req.documentReview} need document review.
            </p>
          )}
          {!compact && (
            <ul className="tp-list">
              <li><b>Page:</b> {coverage.page.definition}</li>
              <li><b>Requirement:</b> {coverage.requirement.definition}</li>
              <li><b>Consent states:</b> {coverage.consentStates.definition}</li>
            </ul>
          )}
        </section>
        <section data-testid="tp-timing">
          <h4>Duration</h4>
          {timing ? (
            <>
              <dl>
                <dt>Total</dt><dd>{timing.total ?? "Not recorded"}</dd>
                <dt>Started</dt><dd>{timing.started ?? "Not recorded"}</dd>
                <dt>Completed</dt><dd>{timing.completed ?? "Not recorded"}</dd>
              </dl>
              {!compact && timing.stages.length > 0 && (
                <ul className="tp-list tp-stages">{timing.stages.map((s) => <li key={s.stage}><span>{s.label}</span><span>{s.text}</span></li>)}</ul>
              )}
              {timing.waiting && <p className="tp-note">{timing.waiting} is the wait between steps, not time inside a stage.</p>}
            </>
          ) : <p>Stage timings were not recorded for this scan.</p>}
        </section>
      </div>
      <h4>Consent checks</h4>
      <p className="tp-note" data-testid="tp-banner">{consent.banner}</p>
      <table className="tp-table" data-testid="tp-consent">
        <thead><tr><th>State</th><th>Result</th><th>What happened</th></tr></thead>
        <tbody>{consent.rows.map((r) => (
          <tr key={r.state}><td>{r.state}</td><td><span className={`tp-pill ${STATUS_CLASS[r.status]}`}>{r.status}</span></td><td>{r.detail}</td></tr>
        ))}</tbody>
      </table>
    </div>
  );
}
