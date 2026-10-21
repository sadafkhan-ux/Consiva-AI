import type { DpdpScoreResponse } from "../api/types";
import { buildScoreView, isDpdpScore, type FindingGroupView, type FindingView, type ScoreRequirementView } from "../report/dpdpScore";

function Row({ r }: { r: ScoreRequirementView }) {
  return (
    <div className="finding-card" data-testid={`dpdp-req-${r.id}`}>
      <div className="finding-head">
        <strong>{r.id}</strong> <span>{r.title}</span> <span className="badge neutral" data-testid={`dpdp-type-${r.id}`}>{r.checkType}</span>
        <span className={`badge ${r.statusKey === "met" ? "ok" : r.statusKey === "not_met" ? "bad" : "warn"}`} data-testid={`dpdp-status-${r.id}`}>
          {r.status}
        </span>
      </div>
      <div className="muted small">
        {r.provisions} · weight {r.weight} · {r.points} · {r.inForce}
      </div>
      {r.timingNote && <p className="small" style={{ margin: "4px 0" }}><span className="badge warn">{r.timingNote}</span></p>}
      {r.observations.map((o) => <p key={o} className="small" style={{ margin: "4px 0" }}>{o}</p>)}
      {r.note && <p className="muted small" style={{ margin: "4px 0" }}>{r.note}</p>}
      <details data-testid={`dpdp-record-${r.id}`}>
        <summary className="small">Check definition and evidence record</summary>
        <p className="small" style={{ margin: "4px 0" }}><b>Checks:</b> {r.description}</p>
        <p className="small" style={{ margin: "4px 0" }}><b>Applies when:</b> {r.applicability}</p>
        <p className="small" style={{ margin: "4px 0" }}><b>Assessment criteria:</b> {r.criteria}</p>
        <p className="small" style={{ margin: "4px 0" }}><b>Evidence expected:</b> {r.expectedEvidence}</p>
        {r.pages.length > 0 && <p className="small mono" style={{ margin: "4px 0" }}><b>Pages:</b> {r.pages.join(", ")}</p>}
        {r.sufficiency && <p className="small" style={{ margin: "4px 0" }}><b>{r.sufficiency.split(":")[0]}:</b>{r.sufficiency.slice(r.sufficiency.indexOf(":") + 1)}</p>}
        {r.limitation && <p className="muted small" style={{ margin: "4px 0" }}>{r.limitation}</p>}
        <p className="small" style={{ margin: "4px 0" }}><b>Confidence:</b> {r.confidence}</p>
        <p className="small" style={{ margin: "4px 0" }}><b>Scoring rationale:</b> {r.scoringRationale}</p>
      </details>
      {(r.evidenceIds.length > 0 || r.rules.length > 0) && (
        <p className="muted small mono" style={{ margin: "4px 0" }}>
          {r.rules.length > 0 && <>Related rules: {r.rules.join(", ")}. </>}
          {r.evidenceIds.length > 0 && <>Evidence: {r.evidenceIds.slice(0, 6).join(", ")}{r.evidenceIds.length > 6 ? ` and ${r.evidenceIds.length - 6} more` : ""}.</>}
        </p>
      )}
    </div>
  );
}

function FindingCard({ f }: { f: FindingView }) {
  return (
    <div className="finding-card" data-testid={`dpdp-finding-${f.id}`}>
      <div className="finding-head">
        <strong>{f.id.startsWith("DPDP-") ? f.id : ""}</strong> <span>{f.title}</span>
        <span className="badge neutral">{f.outcome}</span>
        <span className="badge neutral" data-testid={`dpdp-severity-${f.id}`}>{f.severity}</span>
      </div>
      {f.timingNote && <p className="small" style={{ margin: "4px 0" }}><span className="badge warn">{f.timingNote}</span></p>}
      <p className="muted small" style={{ margin: "2px 0" }}>{f.severityNote}</p>
      {f.legal && <p className="small" style={{ margin: "4px 0" }}><b>Legal reference:</b> {f.legal}</p>}
      <p className="small mono" style={{ margin: "4px 0" }}>
        <b>Affected page:</b> {f.pages.length ? f.pages.join(", ") : "none recorded"} · <b>Consent state:</b> {f.consentState}
      </p>
      {f.observed.length > 0 && <div className="small"><b>What was observed</b></div>}
      {f.observed.map((o) => <p key={o} className="small" style={{ margin: "2px 0" }}>{o}</p>)}
      {(f.evidenceIds.length > 0 || f.rules.length > 0) && (
        <p className="muted small mono" style={{ margin: "4px 0" }}>
          {f.rules.length > 0 && <>Related rules: {f.rules.join(", ")}. </>}
          {f.evidenceIds.length > 0 && <>Evidence: {f.evidenceIds.slice(0, 6).join(", ")}{f.evidenceIds.length > 6 ? ` and ${f.evidenceIds.length - 6} more` : ""}.</>}
        </p>
      )}
      {f.caveat && <p className="muted small" style={{ margin: "4px 0" }}>{f.caveat}</p>}
      {f.businessImpact && <p className="small" style={{ margin: "4px 0" }}><b>Business impact:</b> {f.businessImpact}</p>}
      {f.explanation && <p className="small" style={{ margin: "4px 0" }}><b>Why this group:</b> {f.explanation}</p>}
      {f.whatToReview.length > 0 && <p className="small" style={{ margin: "4px 0" }}><b>What must be reviewed:</b> {f.whatToReview.join(" ")}</p>}
      {f.missing.length > 0 && (
        <>
          <div className="small"><b>What is missing</b></div>
          <ul className="small">{f.missing.map((m) => <li key={m}>{m}</li>)}</ul>
        </>
      )}
      {f.remediation && <p className="small" style={{ margin: "4px 0" }}><b>Recommended remediation:</b> {f.remediation}</p>}
      {f.howToVerify && <p className="small" style={{ margin: "4px 0" }}><b>How to verify:</b> {f.howToVerify}</p>}
      {f.related && <p className="muted small" style={{ margin: "4px 0" }}>Related: {f.related}</p>}
    </div>
  );
}

function GroupBlock({ g }: { g: FindingGroupView }) {
  return (
    <div data-testid={`dpdp-group-${g.key}`} style={{ marginTop: 12 }}>
      <h3>{g.title} ({g.count})</h3>
      <p className="muted small">{g.intro} {g.note}</p>
      {g.entries.length === 0 ? <p className="empty-note small">None.</p> : g.entries.map((f) => <FindingCard key={f.id} f={f} />)}
    </div>
  );
}

/**
 * The proposed DPDP Compliance Score. It renders a view built once (report/dpdpScore.ts) that the PDF also uses, from numbers
 * computed once on the server. When the score is withheld it says "Insufficient Evidence" and why, never a number.
 */
export function DpdpScoreSection({ score }: { score: DpdpScoreResponse | null | undefined }) {
  if (!isDpdpScore(score)) return null;
  const v = buildScoreView(score);
  return (
    <section className="section" data-testid="dpdp-score">
      <h2>{v.title}</h2>
      <div className="muted small" data-testid="dpdp-scope">
        {v.scopeLines.map((l) => <p key={l} style={{ margin: "2px 0" }}>{l}</p>)}
        <p style={{ margin: "2px 0" }} data-testid="dpdp-scan-time">{v.scanTimeLine}</p>
        <p style={{ margin: "2px 0" }} data-testid="dpdp-versions">{v.versionsLine}</p>
      </div>
      <p className="hint" data-testid="dpdp-nature">{v.natureNote}</p>
      <div className="score-layout">
      <div className="score-main">
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontSize: 34, fontWeight: 700 }} data-testid="dpdp-headline">{v.headline}</span>
        <span className="muted small">DPDP Compliance Score</span>
        <span style={{ fontSize: 24, fontWeight: 600, marginLeft: 12 }} data-testid="dpdp-coverage-figure">{v.coverageHeadline}</span>
        <span className="muted small">Assessment coverage (separate from the score)</span>
      </div>
      <div>
        <span className="muted small" data-testid="dpdp-coverage">{v.coverageLine}</span>
      </div>
      {v.caution && <p className="hint" data-testid="dpdp-caution">{v.caution}</p>}
      {v.withheldReason && (
        <p className="hint" data-testid="dpdp-withheld">
          A score is not shown because the evidence is not sufficient: {v.withheldReason} Missing or untested evidence is never counted as compliance.
        </p>
      )}
      <p className="muted small" data-testid="dpdp-basis">{v.basisLine}</p>
      <p className="muted small" data-testid="dpdp-inforce">{v.inForceLine}</p>
      <p className="muted small">{v.formulaLine}</p>
      {v.unableLine && <p className="small" data-testid="dpdp-unable">{v.unableLine}</p>}

      <h3>What the scan found</h3>
      <p className="small" data-testid="dpdp-breakdown">{v.breakdownLine}</p>
      <p className="small" data-testid="dpdp-status-counts">{v.statusCountsLine}</p>
      <p className="muted small" data-testid="dpdp-check-types">{v.checkTypesLine}</p>
      <p className="small" data-testid="dpdp-group-counts">{v.groupCountsLine}</p>
      {v.upcomingLines.map((l) => <p key={l} className="muted small" data-testid="dpdp-upcoming">{l}</p>)}
      {v.groups.filter((g) => g.key !== "needs_review").map((g) => <GroupBlock key={g.key} g={g} />)}
      <div data-testid="dpdp-signals">
        <div className="small"><b>Technical signals, counted apart</b></div>
        <ul className="small">{v.evidenceLines.map((l) => <li key={l}>{l}</li>)}</ul>
        <p className="muted small">{v.evidenceNote}</p>
      </div>

      <h3>Why this result</h3>
      <ul className="small" data-testid="dpdp-explanation">{v.explanationLines.map((l) => <li key={l}>{l}</li>)}</ul>

      <h3>By category</h3>
      <table className="table" data-testid="dpdp-categories">
        <thead><tr><th>Category</th><th>Score</th><th>Coverage</th><th>Assessed</th></tr></thead>
        <tbody>
          {v.categories.map((c) => (
            <tr key={c.name}><td>{c.name}</td><td>{c.score}</td><td>{c.coverage}</td><td>{c.weights}</td></tr>
          ))}
        </tbody>
      </table>

      <h3>Assessed requirements ({v.assessed.length})</h3>
      {v.assessed.map((r) => <Row key={r.id} r={r} />)}

      {v.unableToVerify.length > 0 && (
        <details data-testid="dpdp-unable-list">
          <summary>Unable to verify ({v.unableToVerify.length}): not in the score</summary>
          {v.unableToVerify.map((r) => <Row key={r.id} r={r} />)}
        </details>
      )}
      {v.excluded.length > 0 && (
        <details data-testid="dpdp-excluded-list">
          <summary>Excluded from the score ({v.excluded.length}): not applicable or counted elsewhere</summary>
          {v.excluded.map((r) => <Row key={r.id} r={r} />)}
        </details>
      )}
      <h3>Prioritized remediation ({v.remediationPlan.length})</h3>
      <p className="muted small">{v.remediationNote}</p>
      <div data-testid="dpdp-remediation">
        {v.remediationPlan.length === 0 ? <p className="empty-note small">No remediation actions: no check failed with sufficient evidence.</p> : v.remediationPlan.map((p) => (
          <div className="finding-card" key={p.rank} data-testid={`dpdp-step-${p.rank}`}>
            <div className="finding-head"><strong>{p.rank}.</strong> <span>{p.title}</span> <span className="badge neutral">{p.priority}</span></div>
            <p className="muted small" style={{ margin: "2px 0" }}>{p.outcome} · {p.timing} · {p.priorityReason}</p>
            <p className="small" style={{ margin: "4px 0" }}><b>Action:</b> {p.action}</p>
            <p className="small" style={{ margin: "4px 0" }}><b>How to verify:</b> {p.howToVerify}</p>
            <p className="muted small" style={{ margin: "2px 0" }}>{p.legal}</p>
          </div>
        ))}
      </div>

      <details open data-testid="dpdp-methodology">
        <summary><b>{v.methodology.title}</b> <span className="muted small" data-testid="dpdp-version">{v.methodology.versionLine}</span></summary>
        <p className={v.methodology.custom ? "hint" : "muted small"} data-testid="dpdp-weights-note">{v.methodology.weightsNote}</p>
        <ul className="small" data-testid="dpdp-rules">{v.methodology.rules.map((l) => <li key={l}>{l}</li>)}</ul>
        <div className="small"><b>Category weights</b></div>
        <table className="table" data-testid="dpdp-category-weights">
          <thead><tr><th>Category</th><th>Weight</th><th>Share</th><th>Why</th></tr></thead>
          <tbody>{v.methodology.categories.map((c) => (
            <tr key={c.name}><td>{c.name}</td><td>{c.weight}</td><td>{c.share}</td><td className="small">{c.rationale}</td></tr>
          ))}</tbody>
        </table>
        <div className="small"><b>Requirement weights</b></div>
        <table className="table" data-testid="dpdp-requirement-weights">
          <thead><tr><th>ID</th><th>Requirement</th><th>Type</th><th>Weight</th><th>Why this weight</th><th>Applies when</th></tr></thead>
          <tbody>{v.methodology.weights.map((w) => (
            <tr key={w.id} data-testid={`dpdp-weight-${w.id}`}>
              <td>{w.id}</td><td>{w.title}<div className="muted small">{w.applies}</div></td><td>{w.type}</td><td>{w.weight}</td>
              <td className="small">{w.rationale}</td><td className="small">{w.applicability}</td>
            </tr>
          ))}</tbody>
        </table>
        <div className="small"><b>Legal mapping</b> <span data-testid="dpdp-legal-version">{v.versionsLine}</span></div>
        <ul className="small" data-testid="dpdp-legal-sources">{v.legalSources.map((l) => <li key={l}>{l}</li>)}</ul>
        <div className="small"><b>Duplicate rules</b></div>
        <ul className="small" data-testid="dpdp-dedup">{v.methodology.dedup.map((l) => <li key={l}>{l}</li>)}</ul>
      </details>
      <p className="muted small">{v.methodologyLine}</p>
      <p className="muted small" data-testid="dpdp-disclaimer">{v.disclaimer}</p>
      </div>
      <aside className="review-box" data-testid="dpdp-review-box" aria-label={v.reviewTitle}>
        <h3>{v.reviewTitle}</h3>
        <p className="small" data-testid="dpdp-review-statement">{v.reviewStatement}</p>
        <div className="small" data-testid="dpdp-review-uncertain-head"><b>{v.groups[1].title} ({v.groups[1].count})</b></div>
        <p className="muted small">{v.groups[1].intro} {v.groups[1].note}</p>
        {v.groups[1].entries.map((f) => <FindingCard key={f.id} f={f} />)}
      </aside>
      </div>
    </section>
  );
}
