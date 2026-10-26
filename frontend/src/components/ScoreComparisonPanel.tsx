import { useState } from "react";
import { api } from "../api/client";
import { ApiError, type ScoreComparison } from "../api/types";

const CAUSE_WORDS: Record<string, string> = {
  scan_coverage: "scan coverage (what the scan could test)",
  website_behaviour: "the website's behaviour",
  scoring_configuration: "a check's weight",
  unexplained: "no recorded cause (a data-quality question)",
};

const score = (n: number | null) => (n === null ? "no score" : `${n}%`);
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" }) + " UTC" : "not recorded");

/**
 * Why this scan's score differs from the previous scan of the same website, check by check. It asks the server (scoring/comparison.py), which
 * re-assesses both scans from their stored evidence with the same methodology: nothing is read from an old report and no history is invented.
 */
export function ScoreComparisonPanel({ scanId }: { scanId: string | null }) {
  const [data, setData] = useState<ScoreComparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!scanId) return null;

  async function load() {
    setBusy(true);
    setError(null);
    try {
      setData(await api.compareDpdpScore(scanId as string));
    } catch (e) {
      setData(null);
      setError(e instanceof ApiError && e.status === 409 ? "There is no earlier completed scan of this website to compare with." : e instanceof Error ? e.message : "Could not compare the scans.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="section" data-testid="score-comparison">
      <h2>Why the score differs from the previous scan</h2>
      <p className="muted small">Compares this scan with the previous completed scan of the same website, requirement by requirement. Both are assessed now from their own stored evidence.</p>
      <button className="secondary" onClick={load} disabled={busy} data-testid="score-comparison-load">{busy ? "Comparing…" : data ? "Compare again" : "Compare with the previous scan"}</button>
      {error && <div className="error-box" data-testid="score-comparison-error">{error}</div>}
      {data && (
        <div data-testid="score-comparison-result">
          <p><strong data-testid="score-comparison-summary">{data.summary}</strong></p>
          <table>
            <thead><tr><th></th><th>Previous scan</th><th>This scan</th></tr></thead>
            <tbody>
              <tr><td>Scanned</td><td>{when(data.previous.scanned_at)}</td><td>{when(data.latest.scanned_at)}</td></tr>
              <tr><td>Score</td><td>{score(data.previous.score_percent)}</td><td>{score(data.latest.score_percent)}{data.score_change !== null ? ` (${data.score_change > 0 ? "+" : ""}${data.score_change} points)` : ""}</td></tr>
              <tr><td>Requirement coverage</td><td>{data.previous.coverage_percent ?? "n/a"}%</td><td>{data.latest.coverage_percent ?? "n/a"}%</td></tr>
              <tr><td>Weights assessed / applicable</td><td>{data.denominators.previous.assessed_weight} / {data.denominators.previous.applicable_weight}</td><td>{data.denominators.latest.assessed_weight} / {data.denominators.latest.applicable_weight}{data.denominators.changed ? " (changed)" : ""}</td></tr>
            </tbody>
          </table>
          {data.changes.length > 0 && (
            <table data-testid="score-comparison-changes">
              <thead><tr><th>Requirement</th><th>Before</th><th>Now</th><th>Effect on the score</th><th>Cause</th></tr></thead>
              <tbody>{data.changes.map((c) => (
                <tr key={c.id}>
                  <td><strong>{c.id}</strong> {c.title}</td>
                  <td>{c.previous?.status_label ?? "n/a"}</td>
                  <td>{c.latest?.status_label ?? "n/a"}<div className="muted small">{c.latest?.evidence[0]}</div></td>
                  <td>{c.score_becomes_available ? "a score becomes available" : c.score_becomes_withheld ? "the score is withheld" : c.score_change === null ? "not scored" : `${c.score_change > 0 ? "+" : ""}${c.score_change} points`}</td>
                  <td>{CAUSE_WORDS[c.cause] ?? c.cause}<div className="muted small">{c.explanation}</div></td>
                </tr>
              ))}</tbody>
            </table>
          )}
          {data.changed_inputs.length > 0 && (
            <p className="muted small" data-testid="score-comparison-inputs">What the scans recorded differently: {data.changed_inputs.map((i) => `${i.name.replace(/_/g, " ")} (${String(i.previous)} → ${String(i.latest)})`).join("; ")}.</p>
          )}
          <p className="muted small">{data.attribution.sums_to_total ? "The steps add up to the whole change, switching each requirement in turn, heaviest first; with several changes the split depends on that order." : "The steps do not add up to the whole change; treat the split as indicative."}</p>
          <p className="muted small">{data.methodology_note}</p>
        </div>
      )}
    </section>
  );
}
