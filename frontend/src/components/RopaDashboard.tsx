import { useCallback, useEffect, useState } from "react";
import {
  ropaApi,
  type RopaChange,
  type RopaFinding,
  type RopaRecord,
  type RopaRun,
} from "../api/ropa";

type Tab = "records" | "findings" | "changes";

export function RopaDashboard() {
  const [runs, setRuns] = useState<RopaRun[]>([]);
  const [selected, setSelected] = useState<RopaRun | null>(null);
  const [records, setRecords] = useState<RopaRecord[]>([]);
  const [findings, setFindings] = useState<RopaFinding[]>([]);
  const [changes, setChanges] = useState<RopaChange[]>([]);
  const [tab, setTab] = useState<Tab>("records");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [newKey, setNewKey] = useState<string | null>(null);

  const loadRuns = useCallback(async () => {
    setError(null);
    try {
      const rows = await ropaApi.listRuns();
      setRuns(rows);
      // Preselect the newest run so the dashboard isn't empty on first open.
      if (rows.length && !selected) setSelected(rows[0]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load runs.");
    }
  }, [selected]);

  useEffect(() => {
    void loadRuns();
    // Deliberately runs once: re-running on every `selected` change would refetch
    // the whole list each time a user clicks a run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadRun = useCallback(async (run: RopaRun) => {
    setSelected(run);
    setBusy(true);
    setError(null);
    try {
      const [r, f, c] = await Promise.all([
        ropaApi.getRecords(run.id),
        ropaApi.getFindings(run.id),
        ropaApi.getChanges(run.id),
      ]);
      setRecords(r);
      setFindings(f);
      setChanges(c);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load this run.");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (selected) void loadRun(selected);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id]);

  async function decideRecord(id: string, decision: "approved" | "rejected") {
    try {
      await ropaApi.decideRecord(id, decision, "Reviewed in console");
      if (selected) await loadRun(selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Decision failed.");
    }
  }

  async function decideFinding(id: string, decision: "approved" | "rejected") {
    try {
      await ropaApi.decideFinding(id, decision, "Reviewed in console");
      if (selected) await loadRun(selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Decision failed.");
    }
  }

  async function promote() {
    if (!selected) return;
    try {
      await ropaApi.promoteBaseline(selected.id);
      setError(null);
      await loadRun(selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not promote baseline.");
    }
  }

  async function mintKey() {
    const name = window.prompt("Name this integration key (e.g. prepmyevent-prod):");
    if (!name) return;
    try {
      const created = await ropaApi.createIntegrationKey(name);
      setNewKey(created.api_key);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the key.");
    }
  }

  return (
    <div className="ropa">
      <div className="ropa-toolbar">
        <button className="secondary" onClick={() => void loadRuns()}>Refresh runs</button>
        <button className="secondary" onClick={() => void mintKey()}>New integration key</button>
        {selected && selected.status === "completed" && (
          <button className="secondary" onClick={() => void promote()}>Promote baseline</button>
        )}
      </div>

      {newKey && (
        <div className="info-box key-box">
          <strong>Integration key — copy it now. It is never shown again.</strong>
          <code className="key-value">{newKey}</code>
          <button className="secondary" onClick={() => setNewKey(null)}>Dismiss</button>
        </div>
      )}

      {error && <div className="error-box">{error}</div>}

      {runs.length === 0 ? (
        <div className="info-box">
          No discovery runs yet. A run appears here once an integration adapter posts
          evidence to <code>/api/v1/ropa/evidence</code>.
        </div>
      ) : (
        <div className="ropa-layout">
          <aside className="run-list">
            {runs.map((run) => (
              <button
                key={run.id}
                className={`run-item${selected?.id === run.id ? " active" : ""}`}
                onClick={() => setSelected(run)}
              >
                <div className="run-source">{run.source_name}</div>
                <div className="run-meta">
                  {run.status} · {run.personal_data_elements} PII · {run.tables_scanned} tables
                </div>
              </button>
            ))}
          </aside>

          <section className="run-detail">
            {selected && (
              <div className="stat-row">
                <Stat label="Tables" value={selected.tables_scanned} />
                <Stat label="Columns" value={selected.columns_scanned} />
                <Stat label="Personal data" value={selected.personal_data_elements} />
                <Stat
                  label="Confidence"
                  value={selected.overall_confidence?.toFixed(2) ?? "–"}
                />
                <Stat label="Mode" value={selected.ingest_mode} />
              </div>
            )}

            <div className="ropa-tabs">
              <TabButton current={tab} value="records" onClick={setTab}>
                ROPA Records ({records.length})
              </TabButton>
              <TabButton current={tab} value="findings" onClick={setTab}>
                Risk / Gaps ({findings.length})
              </TabButton>
              <TabButton current={tab} value="changes" onClick={setTab}>
                Changes ({changes.length})
              </TabButton>
            </div>

            {busy && <div className="info-box">Loading…</div>}

            {!busy && tab === "records" && <RecordsTable rows={records} onDecide={decideRecord} />}
            {!busy && tab === "findings" && <FindingsTable rows={findings} onDecide={decideFinding} />}
            {!busy && tab === "changes" && <ChangesTable rows={changes} />}
          </section>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

function TabButton({
  current,
  value,
  onClick,
  children,
}: {
  current: Tab;
  value: Tab;
  onClick: (t: Tab) => void;
  children: React.ReactNode;
}) {
  return (
    <button className={`ropa-tab${current === value ? " active" : ""}`} onClick={() => onClick(value)}>
      {children}
    </button>
  );
}

function RecordsTable({
  rows,
  onDecide,
}: {
  rows: RopaRecord[];
  onDecide: (id: string, d: "approved" | "rejected") => void;
}) {
  if (!rows.length) {
    return (
      <div className="info-box">
        No ROPA records — no processing activity could be established from this evidence.
      </div>
    );
  }
  return (
    <table className="ropa-table">
      <thead>
        <tr>
          <th>Processing activity</th>
          <th>Subjects</th>
          <th>Categories</th>
          <th>Retention</th>
          <th>Owner</th>
          <th>Conf</th>
          <th>Ver</th>
          <th>Status</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td>
              <strong>{r.payload.processing_activity}</strong>
              <div className="muted small">{r.payload.data_elements.join(", ") || "—"}</div>
            </td>
            <td>{r.payload.data_subjects.join(", ") || "—"}</td>
            <td>
              {r.payload.personal_data_categories.map((c) => (
                <span key={c} className="pill pill-pd">{c}</span>
              ))}
            </td>
            <td>{r.payload.retention}</td>
            <td>{r.payload.business_owner}</td>
            <td className="mono">{r.confidence?.toFixed(2) ?? "—"}</td>
            <td className="mono">v{r.version}</td>
            <td><span className={`pill pill-${r.status}`}>{r.status}</span></td>
            <td>
              {(r.status === "draft" || r.status === "in_review") && (
                <>
                  <button className="approve small" onClick={() => onDecide(r.id, "approved")}>Approve</button>
                  <button className="reject small" onClick={() => onDecide(r.id, "rejected")}>Reject</button>
                </>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FindingsTable({
  rows,
  onDecide,
}: {
  rows: RopaFinding[];
  onDecide: (id: string, d: "approved" | "rejected") => void;
}) {
  if (!rows.length) return <div className="info-box">No risk or gap findings.</div>;
  return (
    <table className="ropa-table">
      <thead>
        <tr>
          <th>Severity</th>
          <th>Status</th>
          <th>Finding</th>
          <th>Why this severity</th>
          <th>Review</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {rows.map((f) => (
          <tr key={f.id}>
            <td><span className={`pill pill-${f.severity}`}>{f.severity}</span></td>
            <td className="nowrap">{f.gap_status}</td>
            <td>
              {f.finding}
              {f.recommendation && <div className="muted small">{f.recommendation}</div>}
            </td>
            <td className="muted small">{f.severity_factors.join(" · ")}</td>
            <td><span className="pill">{f.review_status}</span></td>
            <td>
              {f.review_status === "pending" && (
                <>
                  <button className="approve small" onClick={() => onDecide(f.id, "approved")}>Accept</button>
                  <button className="reject small" onClick={() => onDecide(f.id, "rejected")}>Dismiss</button>
                </>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ChangesTable({ rows }: { rows: RopaChange[] }) {
  if (!rows.length) {
    return (
      <div className="info-box">
        No changes detected. Promote a baseline, then push a modified schema to see a diff.
      </div>
    );
  }
  return (
    <table className="ropa-table">
      <thead>
        <tr>
          <th>Change</th>
          <th>Target</th>
          <th>Was</th>
          <th>Now</th>
          <th>Material</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((c) => (
          <tr key={c.id}>
            <td><span className={`pill pill-${c.is_material ? "high" : "low"}`}>{c.change_type}</span></td>
            <td className="mono">{c.target}</td>
            <td className="mono">{c.previous_value ?? "—"}</td>
            <td className="mono">{c.current_value ?? "—"}</td>
            <td>{c.is_material ? "yes — needs review" : "no"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
