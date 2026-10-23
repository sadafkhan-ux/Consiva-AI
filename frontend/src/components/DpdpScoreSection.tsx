import type { DpdpScoreResponse, SectorAssessment } from "../api/types";
import { buildScoreView, isDpdpScore, type FindingGroupView, type FindingView, type LedgerView, type ScoreRequirementView } from "../report/dpdpScore";
import { formatRefs } from "../report/legalRefs";
import { isReportCategory, isReportMetric, OMITTED_DUTIES_NOTE } from "../report/reportScope";

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

/** Every number on the score card: what it counts, its numerator and denominator, the formula and the result. */
function ScoreLedger({ l }: { l: LedgerView }) {
  const categoryMetrics = l.categoryMetrics.filter((m) => isReportMetric(m.name));
  const metricTable = (rows: LedgerView["metrics"], testId: string) => (
    <div className="check-table-wrap">
      <table className="check-table" data-testid={testId}>
        <thead><tr><th>Metric</th><th>Numerator</th><th>Denominator</th><th>Result</th><th>What it measures</th></tr></thead>
        <tbody>
          {rows.map((m) => (
            <tr key={m.name}><td><b>{m.name}</b></td><td className="num">{m.numerator}</td><td className="num">{m.denominator}</td>
              <td className="num"><b>{m.result}</b></td><td className="small">{m.meaning}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
  return (
    <div data-testid="dpdp-ledger">
      <h3>Where each number comes from</h3>
      <p className="muted small">Counts of checks and weights are different things. A check count says how many checks; a weight says how much each is worth.</p>
      <div className="small"><b>Checks (counts)</b></div>
      <div className="ledger-grid" data-testid="dpdp-ledger-counts">
        {l.counts.map((c) => <div className="ledger-item" key={c.label}><span>{c.label}</span><b>{c.value}</b><p>{c.note}</p></div>)}
      </div>
      <div className="small"><b>Weights (points)</b></div>
      <div className="ledger-grid" data-testid="dpdp-ledger-weights">
        {l.weights.map((c) => <div className="ledger-item" key={c.label}><span>{c.label}</span><b>{c.value}</b><p>{c.note}</p></div>)}
      </div>
      <div className="small"><b>Percentages</b></div>
      {metricTable(l.metrics, "dpdp-ledger-metrics")}
      <details data-testid="dpdp-ledger-categories">
        <summary className="small">Percentages by category</summary>
        {metricTable(categoryMetrics, "dpdp-ledger-category-metrics")}
      </details>
      <p className="muted small">{l.checkNote}</p>
    </div>
  );
}

/** One row per check, all of them, directly under the score card: area, what is checked, requirement, weight, status, evidence, contribution. */
function CheckTable({ checks }: { checks: ScoreRequirementView[] }) {
  return (
    <div data-testid="dpdp-check-table-block">
      <h3>Every check behind the score ({checks.length})</h3>
      <p className="muted small">
        Green is a pass, red a fail, amber a check a scan could not verify (not a confirmed violation), grey a check that does not apply.
        Amber outline is a partly met check.
      </p>
      <div className="check-table-wrap">
        <table className="check-table" data-testid="dpdp-check-table">
          <thead>
            <tr><th>Check ID</th><th>Compliance area</th><th>What is being checked</th><th>Applicable requirement</th><th className="num">Weight</th>
              <th>Status</th><th>Evidence / observation</th><th>Score contribution</th></tr>
          </thead>
          <tbody>
            {checks.map((r) => (
              <tr key={r.id} className={r.tone} data-testid={`dpdp-check-row-${r.id}`}>
                <td><b>{r.id}</b></td>
                <td>{r.category}<div className="muted small">{r.checkType}</div></td>
                <td>{r.title}</td>
                <td className="small">
                  {r.provisions}
                  <details data-testid={`dpdp-legal-${r.id}`}>
                    <summary>Legal basis and limits</summary>
                    <p style={{ margin: "4px 0" }}><b>Basis:</b> {r.legalNote}</p>
                    <p style={{ margin: "4px 0" }}><b>The requirement:</b> {r.description}</p>
                    <p style={{ margin: "4px 0" }}><b>Why it applies here:</b> {r.applicability}</p>
                    <p style={{ margin: "4px 0" }}><b>In force:</b> {r.inForce === "in force" ? "yes" : `no, ${r.inForce}`}</p>
                    <p style={{ margin: "4px 0" }}><b>Evidence expected:</b> {r.expectedEvidence}</p>
                    {r.limitation && <p style={{ margin: "4px 0" }}><b>Limit of a scan:</b> {r.limitation}</p>}
                    <p style={{ margin: "4px 0" }}><b>Confidence:</b> {r.confidence}</p>
                  </details>
                </td>
                <td className="num">{r.weight}</td>
                <td><span className={`verdict ${r.tone}`} data-testid={`dpdp-verdict-${r.id}`}>{r.verdict}</span></td>
                <td className="small">
                  {r.evidenceLine}
                  {r.reason && r.reason !== r.evidenceLine && <div className="muted">Reason: {r.reason}</div>}
                  {r.pages.length > 0 && <div className="mono muted">{r.pages.slice(0, 2).join(", ")}{r.pages.length > 2 ? ` and ${r.pages.length - 2} more` : ""}</div>}
                </td>
                <td className="small" data-testid={`dpdp-contribution-${r.id}`}>{r.contribution}<div className="muted">Penalty: {r.penalty}</div></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** How each status is treated by the score, and which checks of this scan carry it. */
function PenaltyRules({ l, hiddenIds }: { l: LedgerView; hiddenIds: ReadonlySet<string> }) {
  return (
    <div data-testid="dpdp-penalty-rules">
      <h3>Penalty and exclusion rules</h3>
      <p className="small">{l.penaltyNote}</p>
      <div className="check-table-wrap">
        <table className="check-table">
          <thead><tr><th>Status</th><th>Rule</th><th>Score numerator</th><th>Score denominator</th><th>Coverage</th><th className="num">Checks</th><th className="num">Weight</th><th>Which checks</th></tr></thead>
          <tbody>
            {l.statusRules.map((r) => (
              <tr key={r.label}><td><b>{r.label}</b></td><td className="small">{r.rule}</td><td className="small">{r.numerator}</td><td className="small">{r.denominator}</td>
                <td className="small">{r.coverage}</td><td className="num">{r.count}</td><td className="num">{r.weight}</td><td className="mono small">{r.checks.split(", ").filter((id) => !hiddenIds.has(id)).join(", ") || "none"}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      {l.notApplicable.length > 0 && (
        <ul className="small" data-testid="dpdp-na-reasons">
          {l.notApplicable.map((n) => <li key={n.id}><b>{n.id}:</b> {n.reason}</li>)}
        </ul>
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

/** Which kind of website this looks like and which provisions depend on it. Informational: the score is not changed by it. */
export function SectorSection({ sector }: { sector: SectorAssessment | null | undefined }) {
  if (!sector) return null;
  return (
    <section className="section" data-testid="sector-assessment">
      <h3>Sector and applicability</h3>
      <p className="small" data-testid="sector-summary">
        <b>{sector.label}</b> <span className="muted">({sector.confidence === "declared" ? "declared" : `${sector.confidence} confidence`})</span>. {sector.summary}
        {sector.also_possible.length > 0 && <> Also possible: {sector.also_possible.join(", ")}.</>}
      </p>
      {sector.signals.length > 0 && (
        <p className="muted small" data-testid="sector-signals">
          Seen in the scan: {sector.signals.map((x) => `"${x.match}" (${x.source})`).join(", ")}.
        </p>
      )}
      {sector.notes.map((n) => (
        <div key={n.id} className="info-box" data-testid="sector-note" style={{ marginTop: 8 }}>
          <b>{n.title}</b>
          <div className="muted small">{formatRefs(n.provisions)}</div>
          <p className="small" style={{ margin: "4px 0" }}>{n.text}</p>
          {n.observed.map((o) => <p key={o} className="small" style={{ margin: "2px 0" }}><b>Observed:</b> {o}</p>)}
          <p className="small" style={{ margin: "4px 0 0" }}><b>What to check:</b> {n.action}</p>
        </div>
      ))}
      {sector.declarations.length > 0 && (
        <div className="small" data-testid="sector-declarations" style={{ marginTop: 8 }}>
          <b>Worth declaring</b> (these requirements stay excluded from the score until declared):
          <ul style={{ margin: "4px 0" }}>
            {sector.declarations.map((d) => (
              <li key={d.flag}>
                {d.requirement_id}: {d.label} — {d.declared === null ? "not declared" : d.declared ? "declared applicable" : "declared not applicable"}. <span className="muted">{d.why}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="muted small" data-testid="sector-limits">{sector.limitations.join(" ")}</p>
    </section>
  );
}

/**
 * The proposed DPDP Compliance Score. It renders a view built once (report/dpdpScore.ts) that the PDF also uses, from numbers
 * computed once on the server. When the score is withheld it says "Insufficient Evidence" and why, never a number.
 */
export function DpdpScoreSection({ score, analysisNote = null }: { score: DpdpScoreResponse | null | undefined; analysisNote?: string | null }) {
  if (!isDpdpScore(score)) return null;
  const v = buildScoreView(score);
  // The score card follows the report: data-rights and security duties are not shown (a website scan cannot observe them). They stay in
  // the score and the counts as not tested, and a note under the check table says so.
  const hiddenIds = new Set(v.allChecks.filter((c) => !isReportCategory(c.category)).map((c) => c.id));
  const shownChecks = v.allChecks.filter((c) => !hiddenIds.has(c.id));
  const groups = v.groups.map((g) => {
    const entries = g.entries.filter((e) => !hiddenIds.has(e.id));
    return { ...g, entries, count: entries.length };
  });
  const groupCountsLine = groups.map((g) => `${g.title} ${g.count}`).join(" · ");
  const shownUnable = v.unableToVerify.filter((r) => !hiddenIds.has(r.id));
  // "13 May 2027: 8 duties start (DPDP-C1, ...)": list, and count, only the duties the card shows.
  const upcoming = v.upcomingLines.map((l) => l.replace(/: (\d+) (duties start|duty starts) \(([^)]*)\)/, (_m, _n, _w, ids: string) => {
    const kept = ids.split(", ").filter((id) => !hiddenIds.has(id));
    return `: ${kept.length} ${kept.length === 1 ? "duty starts" : "duties start"} (${kept.join(", ")})`;
  }));
  return (
    <section className="section" data-testid="dpdp-score">
      <h2>{v.title}</h2>
      <div className="muted small" data-testid="dpdp-scope">
        {v.scopeLines.map((l) => <p key={l} style={{ margin: "2px 0" }}>{l}</p>)}
        <p style={{ margin: "2px 0" }} data-testid="dpdp-scan-time">{v.scanTimeLine}</p>
        <p style={{ margin: "2px 0" }} data-testid="dpdp-versions">{v.versionsLine}</p>
      </div>
      <p className="hint" data-testid="dpdp-nature">{v.natureNote}</p>
      <SectorSection sector={v.sector} />
      <div className="score-layout">
      <div className="score-main">
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontSize: 34, fontWeight: 700 }} data-testid="dpdp-headline">{v.headline}</span>
        <span className="muted small">DPDP Compliance Score</span>
        <span style={{ fontSize: 24, fontWeight: 600, marginLeft: 12 }} data-testid="dpdp-coverage-figure">{v.coverageHeadline}</span>
        <span className="muted small">Assessment coverage (separate from the score)</span>
      </div>
      {analysisNote && <p className="hint" data-testid="dpdp-analysis-note">{analysisNote}</p>}
      {v.caution && <p className="hint" data-testid="dpdp-caution">{v.caution}</p>}
      {v.withheldReason && (
        <p className="hint" data-testid="dpdp-withheld">
          A score is not shown because the evidence is not sufficient: {v.withheldReason} Missing or untested evidence is never counted as compliance.
        </p>
      )}
      <p className="muted small" data-testid="dpdp-basis">{v.basisLine}</p>
      <p className="muted small" data-testid="dpdp-inforce">{v.inForceLine}</p>
      {v.unableLine && <p className="small" data-testid="dpdp-unable">{v.unableLine}</p>}

      <CheckTable checks={shownChecks} />
      <p className="muted small" data-testid="dpdp-omitted-note">{OMITTED_DUTIES_NOTE}</p>
      {/* The score card shows the final percentages only. The arithmetic behind them is one click away, for anyone auditing it. */}
      <details data-testid="dpdp-calculation-details">
        <summary><b>Calculation details</b> <span className="muted small">counts, weights and penalty rules, for audit</span></summary>
        <ScoreLedger l={v.ledger} />
        <PenaltyRules l={v.ledger} hiddenIds={hiddenIds} />
      </details>

      <h3>What the scan found</h3>
      <p className="small" data-testid="dpdp-breakdown">{v.breakdownLine}</p>
      <p className="small" data-testid="dpdp-status-counts">{v.statusCountsLine}</p>
      <p className="muted small" data-testid="dpdp-check-types">{v.checkTypesLine}</p>
      <p className="small" data-testid="dpdp-group-counts">{groupCountsLine}</p>
      {upcoming.map((l) => <p key={l} className="muted small" data-testid="dpdp-upcoming">{l}</p>)}
      {groups.filter((g) => g.key !== "needs_review").map((g) => <GroupBlock key={g.key} g={g} />)}
      <div data-testid="dpdp-signals">
        <div className="small"><b>Technical signals, counted apart</b></div>
        <ul className="small">{v.evidenceLines.map((l) => <li key={l}>{l}</li>)}</ul>
        <p className="muted small">{v.evidenceNote}</p>
      </div>

      <h3>By category</h3>
      <table className="table" data-testid="dpdp-categories">
        <thead><tr><th>Category</th><th>Score</th><th>Coverage</th><th>Assessed</th></tr></thead>
        <tbody>
          {v.categories.filter((c) => isReportCategory(c.name)).map((c) => (
            <tr key={c.name}><td>{c.name}</td><td>{c.score}</td><td>{c.coverage}</td><td>{c.weights}</td></tr>
          ))}
        </tbody>
      </table>

      <h3>Assessed requirements ({v.assessed.length})</h3>
      {v.assessed.map((r) => <Row key={r.id} r={r} />)}

      {shownUnable.length > 0 && (
        <details data-testid="dpdp-unable-list">
          <summary>Unable to verify ({shownUnable.length}): not in the score</summary>
          {shownUnable.map((r) => <Row key={r.id} r={r} />)}
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
            {p.scoreLine && <p className="small" style={{ margin: "4px 0" }} data-testid={`dpdp-step-${p.rank}-impact`}><b>Impact on the score:</b> {p.scoreLine}</p>}
            {p.impact && <p className="small" style={{ margin: "4px 0" }}><b>Why it matters:</b> {p.impact}</p>}
            <p className="small" style={{ margin: "4px 0" }}><b>How to verify:</b> {p.howToVerify}</p>
            <p className="muted small" style={{ margin: "2px 0" }}>{p.legal}</p>
          </div>
        ))}
      </div>

      <details data-testid="dpdp-methodology">
        <summary><b>{v.methodology.title}</b> <span className="muted small" data-testid="dpdp-version">{v.methodology.versionLine}</span></summary>
        <p className={v.methodology.custom ? "hint" : "muted small"} data-testid="dpdp-weights-note">{v.methodology.weightsNote}</p>
        <ul className="small" data-testid="dpdp-rules">{v.methodology.rules.map((l) => <li key={l}>{l}</li>)}</ul>
        <div className="small"><b>Category weights</b></div>
        <table className="table" data-testid="dpdp-category-weights">
          <thead><tr><th>Category</th><th>Weight</th><th>Share</th><th>Why</th></tr></thead>
          <tbody>{v.methodology.categories.filter((c) => isReportCategory(c.name)).map((c) => (
            <tr key={c.name}><td>{c.name}</td><td>{c.weight}</td><td>{c.share}</td><td className="small">{c.rationale}</td></tr>
          ))}</tbody>
        </table>
        <div className="small"><b>Requirement weights</b></div>
        <table className="table" data-testid="dpdp-requirement-weights">
          <thead><tr><th>ID</th><th>Requirement</th><th>Type</th><th>Weight</th><th>Why this weight</th><th>Applies when</th></tr></thead>
          <tbody>{v.methodology.weights.filter((w) => !hiddenIds.has(w.id)).map((w) => (
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
        <div className="small" data-testid="dpdp-review-uncertain-head"><b>{groups[1].title} ({groups[1].count})</b></div>
        <p className="muted small">{groups[1].intro} {groups[1].note}</p>
        {groups[1].entries.map((f) => <FindingCard key={f.id} f={f} />)}
      </aside>
      </div>
    </section>
  );
}
