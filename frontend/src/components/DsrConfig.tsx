import { useCallback, useEffect, useState } from "react";
import {
  dsrApi,
  type DsrRetentionRule,
  type DsrSourceAuthorization,
  type RetentionRuleInput,
  type SourceAuthorizationInput,
} from "../api/dsr";
import { ropaApi, type RopaDataSource } from "../api/ropa";

/**
 * Agent 3's admin config screen: what each data source is allowed to do
 * (search / return / erase, which columns, whether execution is allowed at
 * all) and how long each table's rows must be kept before an erasure is
 * permitted to touch them. Both already exist as full backend endpoints
 * (PUT /config/sources, PUT /config/retention) with zero frontend -- until
 * now an administrator had to call the API by hand to set either one up.
 *
 * View is open to anyone signed in; only the mutating calls require
 * role === "admin" (enforced server-side by _require_admin). The UI mirrors
 * that split rather than inventing a stricter rule of its own.
 */
const MUTATING_OPERATIONS = ["update_field", "anonymize_field", "delete_record"] as const;
const OPERATION_LABELS: Record<string, string> = {
  update_field: "Correct a field",
  anonymize_field: "Anonymise a field",
  delete_record: "Delete the record",
};

function parseJsonObject(
  raw: string,
  label: string,
): { value: Record<string, unknown>; error: string | null } {
  if (!raw.trim()) return { value: {}, error: null };
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return { value: {}, error: `${label} must be a JSON object, e.g. {"users": ["email"]}` };
    }
    return { value: parsed as Record<string, unknown>, error: null };
  } catch {
    return { value: {}, error: `${label} isn't valid JSON.` };
  }
}

function parseList(raw: string): string[] {
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

export function DsrConfig({ isAdmin }: { isAdmin: boolean }) {
  const [sources, setSources] = useState<RopaDataSource[]>([]);
  const [authorizations, setAuthorizations] = useState<DsrSourceAuthorization[]>([]);
  const [rules, setRules] = useState<DsrRetentionRule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [s, a, r] = await Promise.all([
        ropaApi.listSources(),
        dsrApi.listSourceAuthorizations(),
        dsrApi.listRetentionRules(),
      ]);
      setSources(s);
      setAuthorizations(a);
      setRules(r);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load DSR configuration.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (loading) return <p className="muted">Loading configuration…</p>;

  return (
    <div className="dsr dsr-config">
      {!isAdmin && (
        <div className="banner banner-warn">
          You can see the current configuration here, but changing it requires an
          administrator.
        </div>
      )}
      {error && <div className="banner banner-error">{error}</div>}
      {notice && <div className="banner banner-ok">{notice}</div>}

      <SourceAuthorizationSection
        sources={sources}
        authorizations={authorizations}
        isAdmin={isAdmin}
        onSaved={(msg) => {
          setNotice(msg);
          void load();
        }}
        onError={setError}
      />

      <RetentionSection
        sources={sources}
        rules={rules}
        isAdmin={isAdmin}
        onSaved={(msg) => {
          setNotice(msg);
          void load();
        }}
        onError={setError}
      />
    </div>
  );
}

function SourceAuthorizationSection({
  sources,
  authorizations,
  isAdmin,
  onSaved,
  onError,
}: {
  sources: RopaDataSource[];
  authorizations: DsrSourceAuthorization[];
  isAdmin: boolean;
  onSaved: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [editingSourceId, setEditingSourceId] = useState<string | null>(null);

  return (
    <section className="card">
      <h2>Data source authorization</h2>
      <p className="muted small">
        What DSR is allowed to search, return, and erase in each connected source.
        Everything below is an allowlist — a source left unconfigured permits nothing.
      </p>

      {authorizations.length === 0 ? (
        <div className="info-box">No source has a DSR authorization configured yet.</div>
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>Source</th>
              <th>Searchable tables</th>
              <th>Identity tables</th>
              <th>Execution</th>
              <th>Write credential</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {authorizations.map((a) => (
              <tr key={a.id}>
                <td>
                  <strong>{a.source_name ?? "(deleted source)"}</strong>
                </td>
                <td className="small">{a.searchable_tables.join(", ") || "—"}</td>
                <td className="small">{a.identity_tables.join(", ") || "—"}</td>
                <td>
                  <span className={`pill pill-${a.allow_execution ? "warn" : "muted"}`}>
                    {a.allow_execution ? "can act" : "read only"}
                  </span>
                </td>
                <td>
                  {a.write_credential_configured ? (
                    <span className="pill pill-ok">set</span>
                  ) : (
                    <span className="pill pill-muted">none</span>
                  )}
                </td>
                <td>
                  <span className={`pill pill-${a.enabled ? "ok" : "muted"}`}>
                    {a.enabled ? "enabled" : "disabled"}
                  </span>
                </td>
                <td>
                  {isAdmin && (
                    <button className="secondary small" onClick={() => setEditingSourceId(a.data_source_id)}>
                      Edit
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {isAdmin && sources.length === 0 && (
        <div className="info-box">
          No ROPA data sources exist yet — add one from the ROPA agent first, then
          authorize it here.
        </div>
      )}

      {isAdmin && sources.length > 0 && (
        <>
          <div className="sources-header" style={{ marginTop: 16 }}>
            <h3>{editingSourceId ? "Edit authorization" : "Authorize a source"}</h3>
            {!editingSourceId && (
              <select
                defaultValue=""
                onChange={(e) => {
                  if (e.target.value) setEditingSourceId(e.target.value);
                }}
              >
                <option value="" disabled>
                  Choose a source…
                </option>
                {sources.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            )}
          </div>
          {editingSourceId && (
            <SourceAuthorizationForm
              key={editingSourceId}
              sourceId={editingSourceId}
              sourceName={sources.find((s) => s.id === editingSourceId)?.name ?? editingSourceId}
              existing={authorizations.find((a) => a.data_source_id === editingSourceId) ?? null}
              onCancel={() => setEditingSourceId(null)}
              onSaved={(msg) => {
                setEditingSourceId(null);
                onSaved(msg);
              }}
              onError={onError}
            />
          )}
        </>
      )}
    </section>
  );
}

function SourceAuthorizationForm({
  sourceId,
  sourceName,
  existing,
  onCancel,
  onSaved,
  onError,
}: {
  sourceId: string;
  sourceName: string;
  existing: DsrSourceAuthorization | null;
  onCancel: () => void;
  onSaved: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [searchableTables, setSearchableTables] = useState(existing?.searchable_tables.join(", ") ?? "");
  const [identityTables, setIdentityTables] = useState(existing?.identity_tables.join(", ") ?? "");
  const [identifierColumns, setIdentifierColumns] = useState(
    existing ? JSON.stringify(existing.identifier_columns, null, 2) : "",
  );
  const [returnableColumns, setReturnableColumns] = useState(
    existing ? JSON.stringify(existing.returnable_columns, null, 2) : "",
  );
  const [recordKeyColumns, setRecordKeyColumns] = useState(
    existing ? JSON.stringify(existing.record_key_columns, null, 2) : "",
  );
  const [erasableColumns, setErasableColumns] = useState(
    existing ? JSON.stringify(existing.erasable_columns, null, 2) : "",
  );
  const [allowExecution, setAllowExecution] = useState(existing?.allow_execution ?? false);
  const [writeCredentialRef, setWriteCredentialRef] = useState("");
  const [saving, setSaving] = useState(false);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    const identifiers = parseJsonObject(identifierColumns, "Identifier columns");
    const returnable = parseJsonObject(returnableColumns, "Returnable columns");
    const recordKeys = parseJsonObject(recordKeyColumns, "Record key columns");
    const erasable = parseJsonObject(erasableColumns, "Erasable columns");
    const firstError = identifiers.error ?? returnable.error ?? recordKeys.error ?? erasable.error;
    if (firstError) {
      onError(firstError);
      return;
    }

    setSaving(true);
    onError("");
    try {
      const payload: SourceAuthorizationInput = {
        data_source_id: sourceId,
        searchable_tables: parseList(searchableTables),
        identity_tables: parseList(identityTables),
        identifier_columns: identifiers.value as Record<string, Record<string, string>>,
        returnable_columns: returnable.value as Record<string, string[]>,
        record_key_columns: recordKeys.value as Record<string, string[]>,
        erasable_columns: erasable.value as Record<string, string[]>,
        allow_execution: allowExecution,
        write_credential_ref: writeCredentialRef.trim() || null,
      };
      await dsrApi.putSourceAuthorization(payload);
      onSaved(`Authorization saved for ${sourceName}.`);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Could not save this authorization.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="source-form" onSubmit={(e) => void save(e)}>
      <label>
        Searchable tables
        <input
          value={searchableTables}
          onChange={(e) => setSearchableTables(e.target.value)}
          placeholder="users, orders, support_tickets"
        />
        <span className="muted small">Comma-separated. A table left out here is never searched.</span>
      </label>

      <label>
        Identity tables
        <input
          value={identityTables}
          onChange={(e) => setIdentityTables(e.target.value)}
          placeholder="users"
        />
        <span className="muted small">Which of the above actually identify a person (vs. just mentioning one).</span>
      </label>

      <label>
        Identifier columns (JSON)
        <textarea
          rows={3}
          className="mono"
          value={identifierColumns}
          onChange={(e) => setIdentifierColumns(e.target.value)}
          placeholder={'{"users": {"email": "email", "phone": "phone"}}'}
        />
        <span className="muted small">Table → identifier type → column name.</span>
      </label>

      <label>
        Returnable columns (JSON)
        <textarea
          rows={3}
          className="mono"
          value={returnableColumns}
          onChange={(e) => setReturnableColumns(e.target.value)}
          placeholder={'{"users": ["id", "email", "created_at"]}'}
        />
        <span className="muted small">Table → columns allowed in a data-export response.</span>
      </label>

      <label>
        Record key columns (JSON)
        <textarea
          rows={2}
          className="mono"
          value={recordKeyColumns}
          onChange={(e) => setRecordKeyColumns(e.target.value)}
          placeholder={'{"users": ["id"]}'}
        />
        <span className="muted small">Table → the column(s) that uniquely address one row for an update or delete.</span>
      </label>

      <label>
        Erasable columns (JSON)
        <textarea
          rows={2}
          className="mono"
          value={erasableColumns}
          onChange={(e) => setErasableColumns(e.target.value)}
          placeholder={'{"users": ["email", "phone"]}'}
        />
        <span className="muted small">Table → columns an erasure action is permitted to blank or anonymise.</span>
      </label>

      <label className="checkbox-row">
        <input type="checkbox" checked={allowExecution} onChange={(e) => setAllowExecution(e.target.checked)} />
        Allow DSR to actually write to this source (correct / anonymise / delete)
      </label>
      <span className="muted small">
        Off means DSR can search and report on this source, but every execution step is blocked —
        the case stays read-only here regardless of what's approved.
      </span>

      <label>
        Write credential name
        <input
          value={writeCredentialRef}
          onChange={(e) => setWriteCredentialRef(e.target.value)}
          placeholder={existing?.write_credential_configured ? "configured — leave blank to keep it" : "e.g. PREPMYEVENT_WRITE_DB_URL"}
        />
        <span className="muted small">
          The NAME of an environment variable holding the write credential — never the credential itself.
        </span>
      </label>

      <div className="source-form-actions">
        <button type="button" className="secondary" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
        <button type="submit" disabled={saving}>
          {saving ? "Saving…" : "Save authorization"}
        </button>
      </div>
    </form>
  );
}

function RetentionSection({
  sources,
  rules,
  isAdmin,
  onSaved,
  onError,
}: {
  sources: RopaDataSource[];
  rules: DsrRetentionRule[];
  isAdmin: boolean;
  onSaved: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [showForm, setShowForm] = useState(false);
  const sourceName = (id: string | null) =>
    id ? sources.find((s) => s.id === id)?.name ?? id : "organisation-wide";

  return (
    <section className="card">
      <div className="sources-header">
        <div>
          <h2>Retention rules</h2>
          <p className="muted small">
            How long a row must be kept before an erasure is allowed to touch it. A
            blocked erasure always cites the rule and the authority behind it —
            never a bare refusal.
          </p>
        </div>
        {isAdmin && (
          <button className="secondary small" onClick={() => setShowForm((v) => !v)}>
            {showForm ? "Cancel" : "+ Add rule"}
          </button>
        )}
      </div>

      {showForm && (
        <RetentionForm
          sources={sources}
          onCancel={() => setShowForm(false)}
          onSaved={(msg) => {
            setShowForm(false);
            onSaved(msg);
          }}
          onError={onError}
        />
      )}

      {rules.length === 0 ? (
        <div className="info-box">No retention rules configured yet — erasures proceed unblocked by one.</div>
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>Table</th>
              <th>Date column</th>
              <th>Retention</th>
              <th>Scope</th>
              <th>Authority</th>
              <th>Applies to</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rules.map((r) => (
              <tr key={r.id}>
                <td>
                  <strong>{r.table_name}</strong>
                </td>
                <td className="mono small">{r.date_column}</td>
                <td>{r.retention_days} days</td>
                <td className="small">{sourceName(r.data_source_id)}</td>
                <td className="small">{r.authority}</td>
                <td className="small">
                  {r.applies_to_operations.map((op) => OPERATION_LABELS[op] ?? op).join(", ")}
                </td>
                <td>
                  <span className={`pill pill-${r.enabled ? "ok" : "muted"}`}>
                    {r.enabled ? "enabled" : "disabled"}
                  </span>
                </td>
                <td>
                  {isAdmin && r.enabled && (
                    <button
                      className="secondary small"
                      onClick={() =>
                        void dsrApi
                          .disableRetentionRule(r.id)
                          .then(() => onSaved(`Rule for ${r.table_name} disabled.`))
                          .catch((err) => onError(err instanceof Error ? err.message : "Could not disable this rule."))
                      }
                    >
                      Disable
                    </button>
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

function RetentionForm({
  sources,
  onCancel,
  onSaved,
  onError,
}: {
  sources: RopaDataSource[];
  onCancel: () => void;
  onSaved: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [tableName, setTableName] = useState("");
  const [dateColumn, setDateColumn] = useState("");
  const [retentionDays, setRetentionDays] = useState("365");
  const [authority, setAuthority] = useState("");
  const [scopeSourceId, setScopeSourceId] = useState("");
  const [operations, setOperations] = useState<Set<string>>(new Set(["delete_record"]));
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);

  function toggleOperation(op: string) {
    setOperations((prev) => {
      const next = new Set(prev);
      if (next.has(op)) next.delete(op);
      else next.add(op);
      return next;
    });
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!tableName.trim() || !dateColumn.trim() || !authority.trim() || operations.size === 0) return;
    setSaving(true);
    onError("");
    try {
      const payload: RetentionRuleInput = {
        table_name: tableName.trim(),
        date_column: dateColumn.trim(),
        retention_days: Number(retentionDays) || 365,
        authority: authority.trim(),
        data_source_id: scopeSourceId || null,
        applies_to_operations: [...operations],
        notes: notes.trim() || null,
      };
      await dsrApi.putRetentionRule(payload);
      onSaved(`Retention rule for ${payload.table_name} saved.`);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Could not save this rule.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="source-form" onSubmit={(e) => void save(e)}>
      <label>
        Table
        <input required value={tableName} onChange={(e) => setTableName(e.target.value)} placeholder="invoices" />
      </label>
      <label>
        Date column it's measured from
        <input required value={dateColumn} onChange={(e) => setDateColumn(e.target.value)} placeholder="created_at" />
      </label>
      <label>
        Retention period, in days
        <input
          required
          type="number"
          min={1}
          value={retentionDays}
          onChange={(e) => setRetentionDays(e.target.value)}
        />
      </label>
      <label>
        Applies to
        <select value={scopeSourceId} onChange={(e) => setScopeSourceId(e.target.value)}>
          <option value="">Every source (organisation-wide)</option>
          {sources.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name} only
            </option>
          ))}
        </select>
      </label>
      <label>
        Legal authority for this period
        <input
          required
          value={authority}
          onChange={(e) => setAuthority(e.target.value)}
          placeholder="e.g. Income Tax Act, 1961 s.44AA — 6 years"
        />
        <span className="muted small">
          Shown to the requester when it's the reason an erasure was refused — be specific.
        </span>
      </label>
      <fieldset className="endpoints-fieldset">
        <legend>Blocks which actions</legend>
        {MUTATING_OPERATIONS.map((op) => (
          <label key={op} className="checkbox-row">
            <input type="checkbox" checked={operations.has(op)} onChange={() => toggleOperation(op)} />
            {OPERATION_LABELS[op]}
          </label>
        ))}
      </fieldset>
      <label>
        Notes
        <textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Optional context for whoever reviews a block later." />
      </label>

      <div className="source-form-actions">
        <button type="button" className="secondary" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
        <button type="submit" disabled={saving || operations.size === 0}>
          {saving ? "Saving…" : "Save rule"}
        </button>
      </div>
    </form>
  );
}
