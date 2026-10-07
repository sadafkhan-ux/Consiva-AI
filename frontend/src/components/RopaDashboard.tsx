import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import {
  ACTIVE_RUN_STATUSES,
  ropaApi,
  type IntegrationKeySummary,
  type RopaChange,
  type RopaClassification,
  type RopaDataSource,
  type RopaFinding,
  type RopaRecord,
  type RopaRun,
} from "../api/ropa";
import { RunStatusPill, SourcesPanel } from "./RopaSources";

type Tab = "records" | "findings" | "changes" | "classifications";

// How often to re-poll the run list while something is still in flight. Fast
// enough that a Discover click feels live, slow enough not to hammer the API
// from an idle tab with nothing actually running.
const ACTIVE_RUN_POLL_MS = 4000;

export function RopaDashboard() {
  const [runs, setRuns] = useState<RopaRun[]>([]);
  const [sources, setSources] = useState<RopaDataSource[]>([]);
  const [connectors, setConnectors] = useState<string[]>([]);
  const [busySourceIds, setBusySourceIds] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<RopaRun | null>(null);
  const [records, setRecords] = useState<RopaRecord[]>([]);
  const [findings, setFindings] = useState<RopaFinding[]>([]);
  const [changes, setChanges] = useState<RopaChange[]>([]);
  const [classifications, setClassifications] = useState<RopaClassification[]>([]);
  const [tab, setTab] = useState<Tab>("records");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [integrationKeys, setIntegrationKeys] = useState<IntegrationKeySummary[]>([]);
  const [showKeys, setShowKeys] = useState(false);
  // Mutable mirror of `selected`, read inside the polling interval below so
  // that effect does not need `selected` in its dependency list (which would
  // tear down and restart the whole poll loop on every run click).
  const selectedRef = useRef<RopaRun | null>(null);
  selectedRef.current = selected;

  const loadRuns = useCallback(async () => {
    setError(null);
    try {
      const rows = await ropaApi.listRuns();
      setRuns(rows);
      // Preselect the newest run so the dashboard isn't empty on first open.
      setSelected((prev) => prev ?? (rows.length ? rows[0] : null));
      // If the run currently open just changed status (e.g. discovering ->
      // completed), refresh its own detail panel so the records/findings
      // that just got persisted actually show up without a manual click.
      const current = selectedRef.current;
      if (current) {
        const updated = rows.find((r) => r.id === current.id);
        if (updated && updated.status !== current.status) setSelected(updated);
      }
      return rows;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load runs.");
      return [];
    }
  }, []);

  const loadSources = useCallback(async () => {
    try {
      setSources(await ropaApi.listSources());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load data sources.");
    }
  }, []);

  useEffect(() => {
    void loadRuns();
    void loadSources();
    ropaApi
      .listConnectors()
      .then((r) => setConnectors(r.connectors))
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load connectors."));
    // Deliberately runs once: re-running on every `selected` change would refetch
    // the whole list each time a user clicks a run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live status: while any run is still pending/discovering/analyzing, keep
  // polling the run list so a queued discovery's status visibly advances
  // without the user having to hit "Refresh runs" themselves.
  useEffect(() => {
    const hasActiveRun = runs.some((r) => ACTIVE_RUN_STATUSES.has(r.status));
    if (!hasActiveRun) return;
    const id = setInterval(() => void loadRuns(), ACTIVE_RUN_POLL_MS);
    return () => clearInterval(id);
  }, [runs, loadRuns]);

  const loadRun = useCallback(async (run: RopaRun) => {
    setSelected(run);
    setBusy(true);
    setError(null);
    try {
      const [r, f, c, k] = await Promise.all([
        ropaApi.getRecords(run.id),
        ropaApi.getFindings(run.id),
        ropaApi.getChanges(run.id),
        ropaApi.getClassifications(run.id),
      ]);
      setRecords(r);
      setFindings(f);
      setChanges(c);
      setClassifications(k);
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

  const loadIntegrationKeys = useCallback(async () => {
    try {
      setIntegrationKeys(await ropaApi.listIntegrationKeys());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load integration keys.");
    }
  }, []);

  async function mintKey() {
    const name = window.prompt("Name this integration key (e.g. prepmyevent-prod):");
    if (!name) return;
    try {
      const created = await ropaApi.createIntegrationKey(name);
      setNewKey(created.api_key);
      await loadIntegrationKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the key.");
    }
  }

  async function revokeKey(key: IntegrationKeySummary) {
    if (!window.confirm(`Revoke "${key.name}" (${key.key_prefix}…)? This cannot be undone.`)) return;
    try {
      await ropaApi.revokeIntegrationKey(key.id);
      await loadIntegrationKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not revoke the key.");
    }
  }

  async function toggleSourceEnabled(source: RopaDataSource) {
    setError(null);
    try {
      const updated = await ropaApi.setSourceEnabled(source.id, !source.enabled);
      setSources((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update the data source.");
    }
  }

  async function discover(source: RopaDataSource) {
    setError(null);
    setBusySourceIds((prev) => new Set(prev).add(source.id));
    try {
      const run = await ropaApi.discoverSource(source.id);
      // The backend returns immediately with status="pending" -- the active-run
      // poll effect above picks it up and keeps refreshing until it finishes.
      const rows = await loadRuns();
      setSelected(rows.find((r) => r.id === run.id) ?? run);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start discovery.");
    } finally {
      setBusySourceIds((prev) => {
        const next = new Set(prev);
        next.delete(source.id);
        return next;
      });
    }
  }

  return (
    <div className="ropa">
      <SourcesPanel
        sources={sources}
        connectors={connectors}
        runs={runs}
        busySourceIds={busySourceIds}
        onSourceCreated={(source) => setSources((prev) => [source, ...prev])}
        onDiscover={(source) => void discover(source)}
        onToggleEnabled={(source) => void toggleSourceEnabled(source)}
        onError={(message) => setError(message || null)}
      />

      <div className="ropa-toolbar">
        <button className="secondary" onClick={() => void loadRuns()}>Refresh runs</button>
        <button className="secondary" onClick={() => void mintKey()}>New integration key</button>
        <button
          className="secondary"
          onClick={() => {
            setShowKeys((v) => !v);
            if (!showKeys) void loadIntegrationKeys();
          }}
        >
          {showKeys ? "Hide integration keys" : "Manage integration keys"}
        </button>
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

      {showKeys && <IntegrationKeysPanel keys={integrationKeys} onRevoke={(k) => void revokeKey(k)} />}

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
              <>
                <div className="run-header">
                  <RunStatusPill run={selected} />
                  <span className="mono small muted">run {selected.id}</span>
                </div>
                {selected.error && <div className="error-box">{selected.error}</div>}
                <div className="stat-row">
                  <Stat label="Tables" value={selected.tables_scanned} />
                  <Stat label="Columns" value={selected.columns_scanned} />
                  <Stat label="Personal data" value={selected.personal_data_elements} />
                  <Stat
                    label="Confidence"
                    value={selected.overall_confidence?.toFixed(2) ?? "–"}
                  />
                  <Stat label="Mode" value={selected.ingest_mode} />
                  <Stat label="Started" value={formatTime(selected.started_at)} />
                  <Stat label="Completed" value={formatTime(selected.completed_at)} />
                  <Stat
                    label="Duration"
                    value={formatDuration(selected.started_at, selected.completed_at)}
                  />
                  <Stat label="Findings" value={findings.length} />
                  <Stat label="Changes" value={changes.length} />
                </div>
              </>
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
              <TabButton current={tab} value="classifications" onClick={setTab}>
                Column Evidence ({classifications.length})
              </TabButton>
            </div>

            {busy && <div className="info-box">Loading…</div>}

            {!busy && tab === "records" && <RecordsTable rows={records} onDecide={decideRecord} />}
            {!busy && tab === "findings" && <FindingsTable rows={findings} onDecide={decideFinding} />}
            {!busy && tab === "changes" && <ChangesTable rows={changes} />}
            {!busy && tab === "classifications" && <ClassificationsTable rows={classifications} />}
          </section>
        </div>
      )}
    </div>
  );
}

function formatTime(iso: string | null): string {
  return iso ? new Date(iso).toLocaleTimeString() : "–";
}

/** `completed_at` is null for a run still in flight, not an error -- shown as
 * "running…" rather than a dash so it reads as active, not missing. */
function formatDuration(startedIso: string | null, completedIso: string | null): string {
  if (!startedIso) return "–";
  if (!completedIso) return "running…";
  const ms = new Date(completedIso).getTime() - new Date(startedIso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "–";
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const remSeconds = Math.round(seconds % 60);
  return `${minutes}m ${remSeconds}s`;
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
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  if (!rows.length) {
    return (
      <div className="info-box">
        No ROPA records — no processing activity could be established from this evidence.
      </div>
    );
  }

  function toggle(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <table className="ropa-table">
      <thead>
        <tr>
          <th />
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
        {rows.map((r) => {
          const isOpen = expanded.has(r.id);
          return (
            <Fragment key={r.id}>
              <tr>
                <td>
                  <button className="secondary small" onClick={() => toggle(r.id)} aria-label="Toggle data flows">
                    {isOpen ? "▾" : "▸"}
                  </button>
                </td>
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
              {isOpen && (
                <tr className="record-detail-row">
                  <td />
                  <td colSpan={8}>
                    <RecordDataFlowDetail record={r} />
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

function RecordDataFlowDetail({ record }: { record: RopaRecord }) {
  const { storage_locations, processors, recipients, data_flows, transfer_information } = record.payload;
  const hasAnything =
    storage_locations.length || processors.length || recipients.length || data_flows.length;

  if (!hasAnything) {
    return (
      <div className="muted small">
        No data flow established from evidence — no storage location or processor/vendor was identified.
      </div>
    );
  }

  return (
    <div className="data-flow-detail">
      {storage_locations.length > 0 && (
        <div>
          <strong>Storage:</strong> {storage_locations.join(", ")}
        </div>
      )}
      {processors.length > 0 && (
        <div>
          <strong>Processors / vendors:</strong>{" "}
          {processors.map((p) => (
            <span key={p.name} className="pill pill-muted">
              {p.name}
              {p.role ? ` (${p.role})` : ""}
              {p.location ? ` · ${p.location}` : ""}
              {p.dpa_status && p.dpa_status !== "Unknown" ? ` · DPA: ${p.dpa_status}` : ""}
            </span>
          ))}
        </div>
      )}
      {recipients.length > 0 && (
        <div>
          <strong>Recipients:</strong> {recipients.join(", ")}
        </div>
      )}
      {data_flows.length > 0 && (
        <div className="data-flow-paths">
          <strong>Flows:</strong>
          {data_flows.map((step, i) => (
            <div key={i} className="mono small">
              {step.from_node} → {step.to_node}
            </div>
          ))}
        </div>
      )}
      {transfer_information && transfer_information.review_required && (
        <div className="muted small">
          Transfer status: {transfer_information.destination_country ?? "not established"} — requires review.
        </div>
      )}
    </div>
  );
}

function ClassificationsTable({ rows }: { rows: RopaClassification[] }) {
  if (!rows.length) {
    return <div className="info-box">No per-column classification evidence for this run.</div>;
  }
  return (
    <table className="ropa-table">
      <thead>
        <tr>
          <th>Source</th>
          <th>Table</th>
          <th>Column</th>
          <th>Classification</th>
          <th>Subject</th>
          <th>Conf</th>
          <th>Why / evidence</th>
          <th>Review</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((c) => (
          <tr key={c.id}>
            <td className="mono small">{c.source}</td>
            <td className="mono small">{[c.schema, c.table].filter(Boolean).join(".")}</td>
            <td className="mono small">{c.column}</td>
            <td><span className="pill pill-pd">{c.classification}</span></td>
            <td>{c.data_subject}</td>
            <td className="mono">{c.confidence.toFixed(2)}</td>
            <td className="muted small">{c.review_reason ?? c.evidence.join(" · ")}</td>
            <td>
              {c.review_required ? (
                <span className="pill pill-warn">needs review</span>
              ) : (
                <span className="pill pill-ok">resolved</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function IntegrationKeysPanel({
  keys,
  onRevoke,
}: {
  keys: IntegrationKeySummary[];
  onRevoke: (key: IntegrationKeySummary) => void;
}) {
  return (
    <section className="integration-keys-panel info-box">
      <strong>Integration keys</strong>
      {keys.length === 0 ? (
        <div className="muted small">No integration keys issued yet.</div>
      ) : (
        <table className="ropa-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Prefix</th>
              <th>Scopes</th>
              <th>Status</th>
              <th>Last used</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => (
              <tr key={k.id}>
                <td>{k.name}</td>
                <td className="mono small">{k.key_prefix}…</td>
                <td className="muted small">{k.scopes.join(", ")}</td>
                <td>
                  {k.revoked_at ? (
                    <span className="pill pill-bad">revoked</span>
                  ) : k.enabled ? (
                    <span className="pill pill-ok">active</span>
                  ) : (
                    <span className="pill pill-muted">disabled</span>
                  )}
                </td>
                <td className="mono small">{k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "never"}</td>
                <td>
                  {!k.revoked_at && (
                    <button className="reject small" onClick={() => onRevoke(k)}>Revoke</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
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
