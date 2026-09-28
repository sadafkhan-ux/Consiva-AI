import { useCallback, useEffect, useState } from "react";
import {
  purposeApi,
  type Alignment,
  type Connector,
  type PurposeAssessment,
  type PurposeFinding,
  type PurposeRun,
} from "../api/purpose";

/**
 * Agent 6 (Purpose Classifier) console.
 *
 * Reuses the existing shell, styles and pill classes rather than introducing a design
 * of its own, exactly as Agents 3, 4 and 5 do.
 *
 * THE ONE THING THIS SCREEN MUST NEVER DO
 * ---------------------------------------
 * Make `undetermined` look like `aligned`. This agent's whole value rests on the
 * difference between "we compared these and they agree" and "we could not compare
 * these at all", and those two produce identical-looking green dashboards if the
 * second is quietly filtered out.
 *
 * So: the alignment summary always shows all three counts, `undetermined` is rendered
 * in its own neutral tone rather than hidden, and a run whose comparisons were mostly
 * undetermined says so above the table instead of leaving the reader to count rows.
 *
 * CONFIDENCE IS NEVER DROPPED. A mismatch at 0.6 rests on a substring hint; one at 1.0
 * rests on two identical strings. Showing the verdict without the number would assert
 * more than the backend did.
 *
 * CREDENTIALS ARE INPUTS ONLY. The connection string and the spec credential are typed
 * into the form, sent once, and never rendered back -- the backend does not store them
 * and no response type has a field for one. The inputs are `type="password"` so they
 * do not sit in plain sight on a shared screen.
 */

type Tab = "runs" | "findings" | "assess";

const ALIGNMENT_TONE: Record<Alignment, string> = {
  aligned: "ok",
  mismatch: "bad",
  undetermined: "neutral",
};

const SEVERITY_TONE: Record<string, string> = { high: "bad", medium: "warn", low: "neutral" };

const STATUS_TONE: Record<string, string> = {
  completed: "ok",
  approved: "ok",
  running: "warn",
  queued: "neutral",
  pending: "warn",
  dismissed: "neutral",
  rejected: "neutral",
  failed: "bad",
  cancelled: "neutral",
};

function Pill({ tone, children }: { tone: string; children: React.ReactNode }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

function when(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

/** Two decimals, always. A confidence rendered as "0.9" and one rendered as "0.90"
 *  invite different readings of the same number. */
function confidence(value: number): string {
  return Number.isFinite(value) ? value.toFixed(2) : "—";
}

export function PurposeConsole() {
  const [tab, setTab] = useState<Tab>("runs");
  const [runs, setRuns] = useState<PurposeRun[]>([]);
  const [findings, setFindings] = useState<PurposeFinding[]>([]);
  const [selected, setSelected] = useState<PurposeRun | null>(null);
  const [results, setResults] = useState<PurposeAssessment[]>([]);
  const [filter, setFilter] = useState<Alignment | "">("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [r, f] = await Promise.all([purposeApi.listRuns(), purposeApi.listFindings()]);
      setRuns(r);
      setFindings(f);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openRun = useCallback(async (run: PurposeRun, alignment: Alignment | "") => {
    setError(null);
    setSelected(run);
    try {
      setResults(await purposeApi.getResults(run.id, alignment || undefined));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResults([]);
    }
  }, []);

  return (
    <div className="console">
      <header className="console-header">
        <div>
          <h2>Purpose Classifier</h2>
          <div className="tagline">
            For data that already exists: is the purpose it is being used for still the
            purpose it was collected for?
          </div>
        </div>
        <nav className="agent-switch">
          <button className={tab === "runs" ? "active" : ""} onClick={() => setTab("runs")}>
            Assessments
          </button>
          <button className={tab === "findings" ? "active" : ""} onClick={() => setTab("findings")}>
            Findings
          </button>
          <button className={tab === "assess" ? "active" : ""} onClick={() => setTab("assess")}>
            Assess a source
          </button>
        </nav>
      </header>

      {error && <div className="banner banner-error">{error}</div>}

      {tab === "runs" && (
        <RunsTab
          runs={runs}
          selected={selected}
          results={results}
          filter={filter}
          onFilter={(a) => {
            setFilter(a);
            if (selected) void openRun(selected, a);
          }}
          onOpen={(run) => void openRun(run, filter)}
          onRefresh={() => void load()}
        />
      )}

      {tab === "findings" && <FindingsTab findings={findings} onChanged={() => void load()} />}

      {tab === "assess" && (
        <AssessTab
          busy={busy}
          setBusy={setBusy}
          onQueued={() => {
            setTab("runs");
            void load();
          }}
          onError={setError}
        />
      )}
    </div>
  );
}

// ── Assessments ─────────────────────────────────────────────────────────────────

function RunsTab({
  runs,
  selected,
  results,
  filter,
  onFilter,
  onOpen,
  onRefresh,
}: {
  runs: PurposeRun[];
  selected: PurposeRun | null;
  results: PurposeAssessment[];
  filter: Alignment | "";
  onFilter: (a: Alignment | "") => void;
  onOpen: (run: PurposeRun) => void;
  onRefresh: () => void;
}) {
  // Counted from the rows actually returned, so the summary can never disagree with
  // the table under it.
  const counts = results.reduce<Record<string, number>>((acc, r) => {
    acc[r.alignment] = (acc[r.alignment] ?? 0) + 1;
    return acc;
  }, {});
  const total = results.length;
  const undetermined = counts.undetermined ?? 0;

  return (
    <>
      <section className="section">
        <div className="section-head">
          <h3>Runs</h3>
          <button className="small secondary" onClick={onRefresh}>
            Refresh
          </button>
        </div>
        {runs.length === 0 ? (
          <p className="muted">
            No assessments yet. Start one from <strong>Assess a source</strong>.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Started</th>
                <th>Status</th>
                <th>Subjects</th>
                <th>Findings</th>
                <th>Note</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id} className={selected?.id === run.id ? "row-selected" : ""}>
                  <td className="mono">{when(run.started_at ?? run.completed_at)}</td>
                  <td>
                    <Pill tone={STATUS_TONE[run.status] ?? "neutral"}>{run.status}</Pill>
                  </td>
                  <td>{run.assessments_count}</td>
                  <td>{run.findings_count}</td>
                  {/* A run that completed WITH a note is not the same as a clean one.
                      Shown in the list, not only on the detail, because the list is
                      where someone decides which run to look at. */}
                  <td className="muted small">{run.note ?? "—"}</td>
                  <td>
                    <button className="small" onClick={() => onOpen(run)}>
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {selected && (
        <section className="section">
          <div className="section-head">
            <h3>Comparisons</h3>
            <div className="filters">
              {(["", "aligned", "mismatch", "undetermined"] as const).map((a) => (
                <button
                  key={a || "all"}
                  className={filter === a ? "small active" : "small secondary"}
                  onClick={() => onFilter(a)}
                >
                  {a || "all"}
                </button>
              ))}
            </div>
          </div>

          {/* All three counts, always. Dropping `undetermined` from this line is the
              single change that would make a run nobody could assess look like a run
              where everything agreed. */}
          <div className="summary-row">
            <Pill tone="ok">aligned {counts.aligned ?? 0}</Pill>
            <Pill tone="bad">mismatch {counts.mismatch ?? 0}</Pill>
            <Pill tone="neutral">undetermined {undetermined}</Pill>
          </div>

          {!filter && total > 0 && undetermined === total && (
            <div className="banner banner-warn">
              Every comparison in this run was undetermined. That is an honest result,
              not a clean one: it means no declared purpose could be matched to what was
              observed, so nothing here says the data is being used as intended.
            </div>
          )}
          {!filter && total > 0 && undetermined > 0 && undetermined < total && (
            <p className="muted small">
              {undetermined} of {total} comparisons could not be resolved. Those rows are
              listed below rather than filtered out — a run that could not compare most
              of its subjects is a different result from one where everything agreed.
            </p>
          )}

          <table className="table">
            <thead>
              <tr>
                <th>Subject</th>
                <th>Declared</th>
                <th>Observed</th>
                <th>Alignment</th>
                <th>Confidence</th>
                <th>Retention</th>
              </tr>
            </thead>
            <tbody>
              {results.map((row) => (
                <tr key={row.id}>
                  <td>
                    <div className="mono">{row.subject_label ?? row.subject_ref}</div>
                    <div className="muted small">{row.subject_type}</div>
                  </td>
                  <td>
                    {row.declared_purpose ?? <span className="muted">not declared</span>}
                    {row.declared_source && (
                      <div className="muted small">{row.declared_source}</div>
                    )}
                  </td>
                  <td>
                    {row.observed_purpose ?? <span className="muted">not established</span>}
                    {row.observed_source && (
                      <div className="muted small">{row.observed_source}</div>
                    )}
                  </td>
                  <td>
                    <Pill tone={ALIGNMENT_TONE[row.alignment]}>{row.alignment}</Pill>
                  </td>
                  {/* Never shown without the verdict, and the verdict never without
                      it. */}
                  <td className="mono">{confidence(row.confidence)}</td>
                  <td>
                    <span className="muted small">{row.retention_status.replace(/_/g, " ")}</span>
                    {row.retention_note && (
                      <div className="muted small">{row.retention_note}</div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {results.length === 0 && <p className="muted">No comparisons match this filter.</p>}
        </section>
      )}
    </>
  );
}

// ── Findings ────────────────────────────────────────────────────────────────────

function FindingsTab({
  findings,
  onChanged,
}: {
  findings: PurposeFinding[];
  onChanged: () => void;
}) {
  const [reason, setReason] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function decide(
    finding: PurposeFinding,
    decision: "approved" | "rejected" | "dismissed",
  ) {
    // Checked here as well as on the backend. The backend is what ENFORCES it; doing
    // it here means the reviewer finds out before losing what they typed.
    const text = (reason[finding.id] ?? "").trim();
    if ((decision === "rejected" || decision === "dismissed") && !text) {
      setError(
        `A reason is required to ${decision === "rejected" ? "reject" : "dismiss"} a ` +
          "finding. Setting one aside is itself a compliance decision, and one with no " +
          "recorded reasoning cannot be told apart later from one nobody read.",
      );
      return;
    }
    setError(null);
    setBusy(finding.id);
    try {
      await purposeApi.decide(finding.id, decision, text || undefined);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="section">
      <h3>Findings</h3>
      {error && <div className="banner banner-error">{error}</div>}
      {findings.length === 0 ? (
        <p className="muted">
          No findings. That means no assessment has raised one — not that none exist in
          data this agent has never been pointed at.
        </p>
      ) : (
        findings.map((f) => (
          <article key={f.id} className="card">
            <header className="card-head">
              <div>
                <strong>{f.title}</strong>
                <div className="muted small">
                  {f.finding_type.replace(/_/g, " ")} · raised {when(f.created_at)}
                </div>
              </div>
              <div>
                <Pill tone={SEVERITY_TONE[f.severity] ?? "neutral"}>{f.severity}</Pill>{" "}
                <Pill tone={STATUS_TONE[f.status] ?? "neutral"}>{f.status}</Pill>
              </div>
            </header>
            <p>{f.description}</p>
            {f.review_required && f.status === "pending" && (
              <p className="muted small">
                Needs a person. This agent does not settle its own findings.
              </p>
            )}
            {f.status === "pending" && (
              <div className="card-actions">
                <input
                  type="text"
                  placeholder="Reason (required to reject or dismiss)"
                  value={reason[f.id] ?? ""}
                  onChange={(e) => setReason({ ...reason, [f.id]: e.target.value })}
                />
                <button
                  className="small approve"
                  disabled={busy === f.id}
                  onClick={() => void decide(f, "approved")}
                >
                  Accept
                </button>
                <button
                  className="small reject"
                  disabled={busy === f.id}
                  onClick={() => void decide(f, "rejected")}
                >
                  Reject
                </button>
                <button
                  className="small secondary"
                  disabled={busy === f.id}
                  onClick={() => void decide(f, "dismissed")}
                >
                  Dismiss
                </button>
              </div>
            )}
          </article>
        ))
      )}
    </section>
  );
}

// ── Assess a source ─────────────────────────────────────────────────────────────

function AssessTab({
  busy,
  setBusy,
  onQueued,
  onError,
}: {
  busy: boolean;
  setBusy: (b: boolean) => void;
  onQueued: () => void;
  onError: (e: string | null) => void;
}) {
  const [connector, setConnector] = useState<Connector>("postgres");
  const [sourceName, setSourceName] = useState("");
  const [dsn, setDsn] = useState("");
  const [specUrl, setSpecUrl] = useState("");
  const [authHeader, setAuthHeader] = useState("");
  const [csv, setCsv] = useState("");
  const [tableName, setTableName] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    onError(null);
    setBusy(true);
    try {
      await purposeApi.assessSource({
        source_name: sourceName.trim(),
        connector,
        dsn: connector === "postgres" ? dsn : undefined,
        csv_content: connector === "csv" ? csv : undefined,
        table_name: connector === "csv" ? tableName || undefined : undefined,
        spec_url: connector === "rest" ? specUrl.trim() : undefined,
        auth_header: connector === "rest" ? authHeader.trim() || undefined : undefined,
      });
      // Cleared immediately. A credential left in a form field outlives the request
      // that needed it, on a screen somebody may well be sharing.
      setDsn("");
      setAuthHeader("");
      onQueued();
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="section">
      <h3>Assess a source</h3>
      <p className="muted">
        Reads the <strong>shape</strong> only — table and column names, or schema and
        property names. No row data, no samples, no API responses. Reading the values
        would mean this agent holding personal data in order to decide whether personal
        data is held properly.
      </p>

      <form onSubmit={(e) => void submit(e)} className="form">
        <label>
          Source name
          <input
            required
            value={sourceName}
            onChange={(e) => setSourceName(e.target.value)}
            placeholder="A name you will recognise later"
          />
        </label>

        <label>
          Connector
          <select value={connector} onChange={(e) => setConnector(e.target.value as Connector)}>
            <option value="postgres">PostgreSQL</option>
            <option value="csv">CSV (header row)</option>
            <option value="rest">REST API (OpenAPI spec)</option>
          </select>
        </label>

        {connector === "postgres" && (
          <label>
            Connection string
            <input
              required
              type="password"
              value={dsn}
              onChange={(e) => setDsn(e.target.value)}
              placeholder="postgresql://user:password@host:5432/database"
            />
            <span className="muted small">
              Used once to read <span className="mono">information_schema</span>, never
              stored on the run record.
            </span>
          </label>
        )}

        {connector === "csv" && (
          <>
            <label>
              CSV content
              <textarea
                required
                rows={6}
                value={csv}
                onChange={(e) => setCsv(e.target.value)}
                placeholder="id,email,full_name"
              />
              <span className="muted small">
                Only the header row is parsed. The body is never read.
              </span>
            </label>
            <label>
              What it represents
              <input
                value={tableName}
                onChange={(e) => setTableName(e.target.value)}
                placeholder="Defaults to the source name"
              />
            </label>
          </>
        )}

        {connector === "rest" && (
          <>
            <label>
              OpenAPI specification URL
              <input
                required
                type="url"
                value={specUrl}
                onChange={(e) => setSpecUrl(e.target.value)}
                placeholder="https://api.example.com/openapi.json"
              />
              <span className="muted small">
                The specification is fetched — the API's endpoints are not called. A
                spec is pure shape; a response body is the personal data this agent
                exists to ask questions about.
              </span>
            </label>
            <label>
              Authorization header (optional)
              <input
                type="password"
                value={authHeader}
                onChange={(e) => setAuthHeader(e.target.value)}
                placeholder="Bearer ..."
              />
              <span className="muted small">Sent once, never stored.</span>
            </label>
          </>
        )}

        <button type="submit" disabled={busy}>
          {busy ? "Queueing…" : "Assess"}
        </button>
      </form>
    </section>
  );
}
