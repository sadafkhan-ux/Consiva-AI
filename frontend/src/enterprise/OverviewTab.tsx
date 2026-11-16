import type { Declaration, ScanSummary, EnterpriseScore } from "../api/enterprise";
import { completionTone, count, dateTime, percent, points, text, words, type Tone } from "./format";
import { Pill } from "./ui";

const CRAWL_TONE: Record<string, Tone["tone"]> = { queued: "muted", running: "info", completed: "ok", failed: "bad", cancelled: "muted" };
const ANALYSIS_TONE: Record<string, Tone["tone"]> = { not_started: "muted", running: "info", completed: "ok", partial: "warn", failed: "bad" };

/** The assessment stage, read from the declaration and the score: the contract carries no separate field, so this states only what those say. */
export function assessmentStage(score: EnterpriseScore | null, d: Declaration | null): { label: string; tone: Tone["tone"]; detail: string } {
  const pr = d?.parameters;
  const detail = pr ? `${count(pr.assessed)} of ${count(pr.total)} parameters assessed` : "parameters: not recorded yet";
  if (!score) return { label: "Not started", tone: "muted", detail };
  if (score.score === null) return { label: "Not scored", tone: "warn", detail };
  const open = [pr?.not_assessed, pr?.needs_records, pr?.awaiting_human_review].reduce<number>((a, n) => a + (typeof n === "number" ? n : 0), 0);
  return open > 0 ? { label: "Partial", tone: "warn", detail } : { label: "Complete", tone: "ok", detail };
}

function Stage({ name, label, tone, detail, testId }: { name: string; label: string; tone: Tone["tone"]; detail: string; testId: string }) {
  return (
    <div className="stat" data-testid={testId} style={{ minWidth: 0 }}>
      <div className="stat-label">{name}</div>
      <div style={{ margin: "6px 0" }}><Pill tone={tone}>{label}</Pill></div>
      <div className="stat-label">{detail}</div>
    </div>
  );
}

function CountGrid({ title, rows }: { title: string; rows: [string, number | null | undefined][] }) {
  return (
    <section aria-label={title} style={{ marginBottom: 14 }}>
      <h4 style={{ margin: "0 0 8px" }}>{title}</h4>
      <div className="stat-row">
        {rows.map(([label, n]) => (
          <div className="stat" key={label}>
            <div className={typeof n === "number" ? "stat-value" : "stat-label"} style={typeof n === "number" ? undefined : { fontStyle: "italic" }}>{count(n)}</div>
            <div className="stat-label">{label}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

export function ScoreCard({ s, legal }: { s: EnterpriseScore; legal: string | null }) {
  const readiness = s.basis === "readiness";
  const groups = Object.entries(s.by_group ?? {});
  const cov = s.coverage;
  return (
    <section className="finding-card" data-testid="score-card" aria-label="Score">
      <div className="finding-head">
        <strong>{text(s.name, "Score")}</strong>{" "}
        <Pill tone={readiness ? "info" : "ok"} testId="score-basis">{readiness ? "Readiness score" : "Assessment score"}</Pill>
      </div>
      {s.score === null ? (
        <p data-testid="no-score" role="status">
          <strong>No score yet.</strong> {text(s.reason_no_score, "The server did not give a reason.")}
        </p>
      ) : (
        <p style={{ fontSize: 30, fontWeight: 700, margin: "6px 0" }} data-testid="score-value">
          {s.score}<span className="muted" style={{ fontSize: 14, fontWeight: 400 }}> / 100</span>
        </p>
      )}
      <p className="muted small" data-testid="score-coverage">
        Coverage {percent(cov)}. Points earned {points(s.earned_points)} of {points(s.points_available ?? 100)}; a parameter not assessed or not applicable earns nothing.
      </p>
      {readiness && s.not_yet_in_force.length > 0 && (
        <p className="muted small" data-testid="not-in-force">Not yet in force on {text(s.as_of)} and so not counted as failures: {s.not_yet_in_force.join(", ")}.</p>
      )}
      {groups.length > 0 && (
        <div className="check-table-wrap">
          <table className="ropa-table" data-testid="group-table">
            <caption className="muted small" style={{ textAlign: "left" }}>Score by group</caption>
            <thead><tr><th>Group</th><th>Points earned</th><th>Points in play</th><th>Points available</th></tr></thead>
            <tbody>
              {groups.map(([k, g]) => (
                <tr key={k}><td>{text(g.label, words(k))}</td><td>{points(g.earned_points)}</td><td>{points(g.points_in_play)}</td><td>{points(g.points_available)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">Version {text(s.version)}, as of {text(s.as_of)}. Calculated {dateTime(s.calculated_at)}.</p>
      {legal && <p className="small" data-testid="legal-review"><Pill tone="warn">Legal review</Pill> {legal}</p>}
    </section>
  );
}

export function OverviewTab({ s }: { s: ScanSummary }) {
  const d = s.declaration;
  const ct = completionTone(s.completion);
  const asm = assessmentStage(s.score, d);
  const u = d?.urls;
  return (
    <div data-testid="overview">
      <div className="banner banner-warn" role="note" data-testid="not-certification">
        This is a website-observable readiness assessment. It is not a certification, an audit opinion or legal advice, and it does not find that any law has been breached.
      </div>

      <h4 style={{ margin: "12px 0 8px" }}>Completion <Pill tone={ct.tone} testId="completion-word">{ct.label}</Pill></h4>
      {s.completion === "partial" && <p className="muted small">Partial: some of the crawl, analysis or assessment is outstanding. See the three stages below and the counts under them.</p>}
      <div className="stat-row" data-testid="stages">
        <Stage testId="stage-crawl" name="Crawl" label={words(s.crawl_status)} tone={CRAWL_TONE[s.crawl_status] ?? "muted"} detail={`${count(u?.crawled)} crawled of ${count(u?.eligible)} eligible`} />
        <Stage testId="stage-analysis" name="Analysis" label={words(s.analysis_status)} tone={ANALYSIS_TONE[s.analysis_status] ?? "muted"} detail={`${count(u?.analysed)} analysed of ${count(u?.eligible)} eligible`} />
        <Stage testId="stage-assessment" name="Assessment" label={asm.label} tone={asm.tone} detail={asm.detail} />
      </div>
      {s.error && <div className="banner banner-error" role="alert">{s.error}</div>}

      {s.score ? <ScoreCard s={s.score} legal={s.legal_review_status} /> : (
        <section className="finding-card" data-testid="score-card"><p role="status" data-testid="no-score"><strong>No score yet.</strong> No score has been calculated for this scan.</p>{s.legal_review_status && <p className="small" data-testid="legal-review"><Pill tone="warn">Legal review</Pill> {s.legal_review_status}</p>}</section>
      )}

      <h3 style={{ margin: "16px 0 8px" }}>Coverage declaration</h3>
      {!d ? <p className="empty-note">The coverage declaration has not been recorded yet.</p> : (
        <div data-testid="declaration">
          <CountGrid title="Pages" rows={[
            ["Discovered", u?.discovered], ["Eligible", u?.eligible], ["Crawled", u?.crawled], ["Analysed", u?.analysed], ["Pending", u?.pending], ["Excluded", u?.excluded], ["Failed", u?.failed],
          ]} />
          <CountGrid title="Documents" rows={[["Discovered", d.documents.discovered], ["Extracted", d.documents.extracted], ["Partly extracted", d.documents.partly_extracted], ["Unreadable", d.documents.unreadable]]} />
          <CountGrid title="Evidence and parameters" rows={[
            ["Evidence records", d.evidence.records], ["Parameters assessed", d.parameters.assessed], ["Awaiting human review", d.parameters.awaiting_human_review], ["Needs records", d.parameters.needs_records], ["Not assessed", d.parameters.not_assessed],
          ]} />
          {u && (u.not_recorded || u.pages_with_limits || u.pages_not_fully_analysed || u.pages_not_sent_to_model) ? (
            <CountGrid title="Where reading was limited" rows={[
              ["Pages with a read limit", u.pages_with_limits], ["Pages not fully analysed", u.pages_not_fully_analysed], ["Pages not sent to the model", u.pages_not_sent_to_model], ["Links not recorded (record cap)", u.not_recorded],
            ]} />
          ) : null}
          {u?.excluded_by_reason && Object.keys(u.excluded_by_reason).length > 0 && (
            <p className="muted small" data-testid="excluded-reasons">Excluded by reason: {Object.entries(u.excluded_by_reason).map(([k, n]) => `${words(k)} ${count(n)}`).join(", ")}.</p>
          )}
        </div>
      )}
      <p className="muted small">Created {dateTime(s.created_at)}. Started {dateTime(s.started_at)}. Finished {dateTime(s.completed_at)}.</p>
    </div>
  );
}
