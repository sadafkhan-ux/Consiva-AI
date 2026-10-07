import { useState } from "react";
import {
  ropaApi,
  type CreateSourceInput,
  type RopaDataSource,
  type RopaRun,
} from "../api/ropa";

const CONNECTOR_LABELS: Record<string, string> = {
  postgres: "PostgreSQL",
  rest_api: "REST API",
};

function connectorLabel(connector: string): string {
  return CONNECTOR_LABELS[connector] ?? connector;
}

function latestRunFor(sourceId: string, runs: RopaRun[]): RopaRun | null {
  const matches = runs.filter((r) => r.data_source_id === sourceId);
  if (!matches.length) return null;
  // Runs come back newest-first from the API; this stays correct even if a
  // caller ever passes an unsorted list.
  return [...matches].sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
}

export function SourcesPanel({
  sources,
  connectors,
  runs,
  busySourceIds,
  onSourceCreated,
  onDiscover,
  onToggleEnabled,
  onError,
}: {
  sources: RopaDataSource[];
  connectors: string[];
  runs: RopaRun[];
  /** Source ids with a discovery already in flight -- disables a second
   * Discover click client-side, on top of the backend's own 409. */
  busySourceIds: Set<string>;
  onSourceCreated: (source: RopaDataSource) => void;
  onDiscover: (source: RopaDataSource) => void;
  onToggleEnabled: (source: RopaDataSource) => void;
  onError: (message: string) => void;
}) {
  const [showForm, setShowForm] = useState(false);

  return (
    <section className="sources-panel">
      <div className="sources-header">
        <h3>Data Sources</h3>
        <button className="secondary small" onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Cancel" : "+ Add Data Source"}
        </button>
      </div>

      {showForm && (
        <AddSourceForm
          connectors={connectors}
          onError={onError}
          onCreated={(source) => {
            onSourceCreated(source);
            setShowForm(false);
          }}
        />
      )}

      {sources.length === 0 ? (
        <div className="info-box">
          No data sources yet. Add one to let Consiva connect directly and discover its
          schema, or push evidence from your own adapter via{" "}
          <code>/api/v1/ropa/evidence</code> instead.
        </div>
      ) : (
        <table className="ropa-table sources-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Connector</th>
              <th>Credential</th>
              <th>Last discovery</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sources.map((source) => {
              const run = latestRunFor(source.id, runs);
              const discovering = busySourceIds.has(source.id) || (run != null && isActive(run));
              return (
                <tr key={source.id}>
                  <td>
                    <strong>{source.name}</strong>
                    {!source.enabled && <div className="muted small">disabled</div>}
                  </td>
                  <td>{connectorLabel(source.connector)}</td>
                  <td>
                    {source.has_stored_credential || source.credential_ref ? (
                      <span className="pill pill-ok">configured</span>
                    ) : (
                      <span className="pill pill-warn">not set</span>
                    )}
                  </td>
                  <td className="mono small">
                    {source.last_verified_at
                      ? new Date(source.last_verified_at).toLocaleString()
                      : "never"}
                  </td>
                  <td>
                    {run ? (
                      <RunStatusPill run={run} />
                    ) : (
                      <span className="pill pill-muted">no runs yet</span>
                    )}
                    {run?.error && <div className="muted small run-error">{run.error}</div>}
                  </td>
                  <td>
                    <button
                      className="secondary small"
                      disabled={discovering || !source.enabled}
                      onClick={() => onDiscover(source)}
                    >
                      {discovering ? "Discovering…" : "Discover"}
                    </button>
                    <button
                      className="secondary small"
                      disabled={discovering}
                      onClick={() => onToggleEnabled(source)}
                    >
                      {source.enabled ? "Disable" : "Enable"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function isActive(run: RopaRun): boolean {
  return run.status === "pending" || run.status === "discovering" || run.status === "analyzing";
}

export function RunStatusPill({ run }: { run: RopaRun }) {
  const tone =
    run.status === "completed" ? "ok" : run.status === "failed" ? "bad" : "warn";
  return <span className={`pill pill-${tone}`}>{run.status}</span>;
}

type PostgresFields = {
  host: string;
  port: string;
  dbname: string;
  user: string;
  sslmode: string;
};

type RestFields = {
  base_url: string;
  auth_scheme: string;
  endpoints: { name: string; path: string }[];
};

function AddSourceForm({
  connectors,
  onCreated,
  onError,
}: {
  connectors: string[];
  onCreated: (source: RopaDataSource) => void;
  onError: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [connector, setConnector] = useState(connectors[0] ?? "");
  const [sourceType, setSourceType] = useState<"database" | "api">(
    connectors[0] === "rest_api" ? "api" : "database"
  );
  const [pg, setPg] = useState<PostgresFields>({
    host: "", port: "5432", dbname: "", user: "", sslmode: "require",
  });
  const [rest, setRest] = useState<RestFields>({
    base_url: "", auth_scheme: "Bearer", endpoints: [{ name: "", path: "" }],
  });
  const [secret, setSecret] = useState("");
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);

  function selectConnector(value: string) {
    setConnector(value);
    setSourceType(value === "rest_api" ? "api" : "database");
    setTestResult(null);
  }

  function buildConfig(): Record<string, unknown> {
    if (connector === "postgres") {
      return {
        host: pg.host.trim(),
        port: Number(pg.port) || 5432,
        dbname: pg.dbname.trim(),
        user: pg.user.trim(),
        sslmode: pg.sslmode,
      };
    }
    if (connector === "rest_api") {
      return {
        base_url: rest.base_url.trim(),
        auth_scheme: rest.auth_scheme.trim() || "Bearer",
        endpoints: rest.endpoints
          .filter((e) => e.name.trim() && e.path.trim())
          .map((e) => ({ name: e.name.trim(), path: e.path.trim() })),
      };
    }
    return {};
  }

  function configIsComplete(): boolean {
    if (connector === "postgres") return !!(pg.host.trim() && pg.dbname.trim() && pg.user.trim());
    if (connector === "rest_api") {
      return !!rest.base_url.trim() && rest.endpoints.some((e) => e.name.trim() && e.path.trim());
    }
    return false;
  }

  async function testConnection() {
    setTesting(true);
    setTestResult(null);
    onError("");
    try {
      const result = await ropaApi.testConnection({
        connector,
        config: buildConfig(),
        secret: secret || undefined,
      });
      setTestResult(result);
    } catch (err) {
      setTestResult({ ok: false, message: err instanceof Error ? err.message : String(err) });
    } finally {
      setTesting(false);
    }
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || !connector) return;
    setSaving(true);
    onError("");
    try {
      const input: CreateSourceInput = {
        name: name.trim(),
        connector,
        source_type: sourceType,
        config: buildConfig(),
      };
      const source = await ropaApi.createSource(input);
      if (secret.trim()) {
        await ropaApi.setSourceCredential(source.id, secret.trim());
      }
      // A credential left in a form field outlives the request that needed
      // it, on a screen somebody may well be sharing.
      setSecret("");
      onCreated(source);
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="source-form" onSubmit={(e) => void save(e)}>
      <label>
        Source name
        <input
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. prepmyevent-production"
        />
      </label>

      <label>
        Connector
        <select
          value={connector}
          onChange={(e) => selectConnector(e.target.value)}
          disabled={connectors.length === 0}
        >
          {connectors.length === 0 && <option value="">No connectors available</option>}
          {connectors.map((c) => (
            <option key={c} value={c}>
              {connectorLabel(c)}
            </option>
          ))}
        </select>
      </label>

      {connector === "postgres" && (
        <>
          <label>
            Host
            <input required value={pg.host} onChange={(e) => setPg({ ...pg, host: e.target.value })} />
          </label>
          <label>
            Port
            <input
              value={pg.port}
              onChange={(e) => setPg({ ...pg, port: e.target.value })}
              inputMode="numeric"
            />
          </label>
          <label>
            Database name
            <input required value={pg.dbname} onChange={(e) => setPg({ ...pg, dbname: e.target.value })} />
          </label>
          <label>
            Username
            <input required value={pg.user} onChange={(e) => setPg({ ...pg, user: e.target.value })} />
          </label>
          <label>
            SSL mode
            <select value={pg.sslmode} onChange={(e) => setPg({ ...pg, sslmode: e.target.value })}>
              <option value="require">require</option>
              <option value="prefer">prefer</option>
              <option value="disable">disable</option>
            </select>
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete="new-password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder="Encrypted immediately; never shown again"
            />
            <span className="muted small">
              Stored encrypted (not in plain text), or leave blank and set
              <code> credential_ref</code> to an environment variable instead.
            </span>
          </label>
        </>
      )}

      {connector === "rest_api" && (
        <>
          <label>
            Base URL
            <input
              required
              type="url"
              value={rest.base_url}
              onChange={(e) => setRest({ ...rest, base_url: e.target.value })}
              placeholder="https://api.example.com"
            />
          </label>
          <label>
            Auth scheme
            <input
              value={rest.auth_scheme}
              onChange={(e) => setRest({ ...rest, auth_scheme: e.target.value })}
              placeholder="Bearer"
            />
          </label>
          <fieldset className="endpoints-fieldset">
            <legend>Endpoints to introspect</legend>
            {rest.endpoints.map((endpoint, i) => (
              <div className="endpoint-row" key={i}>
                <input
                  aria-label={`Endpoint ${i + 1} name`}
                  placeholder="name (e.g. attendees)"
                  value={endpoint.name}
                  onChange={(e) => {
                    const next = [...rest.endpoints];
                    next[i] = { ...next[i], name: e.target.value };
                    setRest({ ...rest, endpoints: next });
                  }}
                />
                <input
                  aria-label={`Endpoint ${i + 1} path`}
                  placeholder="/v1/attendees"
                  value={endpoint.path}
                  onChange={(e) => {
                    const next = [...rest.endpoints];
                    next[i] = { ...next[i], path: e.target.value };
                    setRest({ ...rest, endpoints: next });
                  }}
                />
                {rest.endpoints.length > 1 && (
                  <button
                    type="button"
                    className="secondary small"
                    onClick={() =>
                      setRest({ ...rest, endpoints: rest.endpoints.filter((_, j) => j !== i) })
                    }
                  >
                    Remove
                  </button>
                )}
              </div>
            ))}
            <button
              type="button"
              className="secondary small"
              onClick={() => setRest({ ...rest, endpoints: [...rest.endpoints, { name: "", path: "" }] })}
            >
              + Add endpoint
            </button>
          </fieldset>
          <label>
            API key
            <input
              type="password"
              autoComplete="new-password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder="Encrypted immediately; never shown again"
            />
          </label>
        </>
      )}

      {testResult && (
        <div className={testResult.ok ? "info-box test-ok" : "error-box"}>
          {(testResult.ok ? "✓ " : "✗ ") + testResult.message}
        </div>
      )}

      <div className="source-form-actions">
        <button
          type="button"
          className="secondary"
          disabled={testing || !connector || !configIsComplete()}
          onClick={() => void testConnection()}
        >
          {testing ? "Testing…" : "Test Connection"}
        </button>
        <button type="submit" disabled={saving || !name.trim() || !connector || !configIsComplete()}>
          {saving ? "Saving…" : "Save Data Source"}
        </button>
      </div>
    </form>
  );
}
