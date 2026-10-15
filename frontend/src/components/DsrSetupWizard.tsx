import { useState } from "react";
import {
  dsrApi,
  type DsrConnectionTest,
  type DsrDiscovery,
  type DsrDomainCheck,
  type DsrSchemaListing,
  type DsrSetupSource,
  type SourceAuthorizationInput,
} from "../api/dsr";

/**
 * Domain-first DSR setup: enter a domain, prove you control it, see what can be connected,
 * authorise ONE source explicitly, test it.
 *
 * Two things this screen is careful never to imply:
 *   - A verified domain does not give Consiva access to anything. Step 3 onward is a separate,
 *     explicit authorisation of specific tables, columns and operations.
 *   - A passing connection test proves the configuration is usable, not that a search finds
 *     the right person or that an erasure would verify. The test result states this itself.
 *
 * Execution (write access) is OFF unless the administrator turns it on, and the write password
 * is a write-only field: it is sent once, stored encrypted by the backend, never shown again,
 * and cleared from this component's state as soon as the save returns.
 */
const IDENTIFIER_KINDS = ["email", "phone", "reference"] as const;
type Kind = (typeof IDENTIFIER_KINDS)[number];

const READINESS_LABEL: Record<string, string> = {
  authorized: "Authorised",
  ready_to_authorize: "Ready to authorise",
  needs_credential: "Needs a credential",
  connector_not_supported: "Not supported by DSR",
  disabled: "Disabled",
  revoked: "Revoked",
};

interface TableChoice {
  identifiers: Partial<Record<Kind, string>>;
  returnable: string[];
  erasable: string[];
  keyColumn: string;
  isIdentity: boolean;
}

export function DsrSetupWizard({ isAdmin }: { isAdmin: boolean }) {
  const [domainInput, setDomainInput] = useState("");
  const [check, setCheck] = useState<DsrDomainCheck | null>(null);
  const [discovery, setDiscovery] = useState<DsrDiscovery | null>(null);
  const [source, setSource] = useState<DsrSetupSource | null>(null);
  const [schema, setSchema] = useState<DsrSchemaListing | null>(null);
  const [choices, setChoices] = useState<Record<string, TableChoice>>({});
  const [allowExecution, setAllowExecution] = useState(false);
  const [writeSecret, setWriteSecret] = useState("");
  const [saved, setSaved] = useState(false);
  const [test, setTest] = useState<DsrConnectionTest | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run<T>(fn: () => Promise<T>): Promise<T | null> {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
      return null;
    } finally {
      setBusy(false);
    }
  }

  if (!isAdmin) {
    return (
      <section className="dsr-setup">
        <h2>Set up DSR</h2>
        <p className="muted">
          Only an administrator can connect a data source. Ask an administrator in your
          organisation to run this setup.
        </p>
      </section>
    );
  }

  async function checkDomain(verify: boolean) {
    const result = await run(() => dsrApi.setupCheckDomain(domainInput, verify));
    if (!result) return;
    setCheck(result);
    setDiscovery(null);
    setSource(null);
    setSchema(null);
    setSaved(false);
    setTest(null);
    if (result.ownership.proven) {
      const found = await run(() => dsrApi.setupDiscover(result.domain));
      if (found) setDiscovery(found);
    }
  }

  async function chooseSource(s: DsrSetupSource) {
    setSource(s);
    setSchema(null);
    setChoices({});
    setSaved(false);
    setTest(null);
    setAllowExecution(false);
    setWriteSecret("");
    const listing = await run(() => dsrApi.getSourceSchema(s.data_source_id));
    if (listing) setSchema(listing);
  }

  function toggleTable(table: string, columns: string[], on: boolean) {
    setChoices((prev) => {
      const next = { ...prev };
      if (!on) {
        delete next[table];
        return next;
      }
      next[table] = {
        identifiers: {},
        returnable: [],
        erasable: [],
        keyColumn: columns.includes("id") ? "id" : (columns[0] ?? "id"),
        isIdentity: false,
      };
      return next;
    });
  }

  function patchChoice(table: string, patch: Partial<TableChoice>) {
    setChoices((prev) => ({ ...prev, [table]: { ...prev[table], ...patch } }));
  }

  function toggleIn(list: string[], value: string): string[] {
    return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
  }

  const chosenTables = Object.keys(choices);
  // A table with no identifier column can never match a person, so it cannot be searched.
  const everyTableHasIdentifier = chosenTables.every(
    (t) => Object.values(choices[t].identifiers).some(Boolean),
  );
  const canSave =
    !!source && chosenTables.length > 0 && everyTableHasIdentifier && !busy &&
    (!allowExecution || chosenTables.some((t) => choices[t].erasable.length > 0));

  async function save() {
    if (!source) return;
    const payload: SourceAuthorizationInput = {
      data_source_id: source.data_source_id,
      searchable_tables: chosenTables,
      identity_tables: chosenTables.filter((t) => choices[t].isIdentity),
      identifier_columns: Object.fromEntries(
        chosenTables.map((t) => [
          t,
          Object.fromEntries(
            Object.entries(choices[t].identifiers).filter(([, column]) => !!column),
          ) as Record<string, string>,
        ]),
      ),
      returnable_columns: Object.fromEntries(chosenTables.map((t) => [t, choices[t].returnable])),
      record_key_columns: Object.fromEntries(chosenTables.map((t) => [t, [choices[t].keyColumn]])),
      erasable_columns: Object.fromEntries(
        chosenTables.map((t) => [t, allowExecution ? choices[t].erasable : []]),
      ),
      allow_execution: allowExecution,
      ...(allowExecution && writeSecret ? { write_secret: writeSecret } : {}),
    };
    const result = await run(() => dsrApi.putSourceAuthorization(payload));
    // The secret has been sent (or was never entered); either way it must not linger here.
    setWriteSecret("");
    if (result) setSaved(true);
  }

  async function runTest() {
    if (!source) return;
    const result = await run(() => dsrApi.testSourceConnection(source.data_source_id));
    if (result) setTest(result);
  }

  const usable = (s: DsrSetupSource) =>
    s.readiness === "ready_to_authorize" || s.readiness === "authorized" || s.readiness === "revoked";

  return (
    <section className="dsr-setup">
      <h2>Set up DSR</h2>
      <p className="muted">
        Connect a system once, and every future data-subject request in your organisation reuses
        it. Nothing is connected until you authorise a specific source below.
      </p>

      {error && <div className="banner banner-warn" role="alert">{error}</div>}

      <h3>1. Your website domain</h3>
      <div className="dsr-actions">
        <input
          aria-label="Website domain"
          placeholder="example.com"
          value={domainInput}
          onChange={(e) => setDomainInput(e.target.value)}
        />
        <button disabled={busy || !domainInput.trim()} onClick={() => void checkDomain(false)}>
          Check domain
        </button>
      </div>

      {check && !check.ownership.proven && (
        <div className="banner banner-info">
          <p>
            <strong>{check.domain}</strong> is not verified yet. {check.ownership.detail}.
          </p>
          {check.txt_record && (
            <>
              <p>Add this DNS record to {check.domain}, then check again:</p>
              <pre aria-label="DNS record to publish">
                {check.txt_record.type} {check.txt_record.host} {check.txt_record.value}
              </pre>
            </>
          )}
          <button disabled={busy} onClick={() => void checkDomain(true)}>
            I added it — verify now
          </button>
        </div>
      )}

      {check?.ownership.proven && (
        <p className="pill pill-ok">
          {check.domain} verified
          {check.ownership.method === "existing_verification"
            ? " (already verified for your organisation)"
            : " just now"}
        </p>
      )}
      {check && <p className="muted small">{check.note}</p>}

      {discovery && (
        <>
          <h3>2. What can be connected</h3>
          {discovery.manifest.found ? (
            <p className="muted">
              Your site publishes an integration manifest. These are your own claims; Consiva
              does not contact them.
            </p>
          ) : (
            <p className="muted">
              No integration manifest is published at <code>/.well-known/consiva-dsr.json</code>
              {" "}({discovery.manifest.reason}). That is fine — the options below do not need one.
            </p>
          )}
          <ul className="dsr-paths">
            {discovery.paths.map((p) => (
              <li key={p.id}>
                <strong>{p.title}</strong>{" "}
                <span className={`pill pill-${p.status === "supported" ? "ok" : "muted"}`}>
                  {p.status === "supported" ? "Available" : "Not available"}
                </span>
                <div className="muted">{p.summary}</div>
                {p.requires && (
                  <ul>
                    {p.requires.map((r) => (
                      <li key={r} className="small">{r}</li>
                    ))}
                  </ul>
                )}
                {p.alternative && (
                  <div className="small">Use “{p.alternative.replace("_", " ")}” instead.</div>
                )}
              </li>
            ))}
          </ul>

          <h3>3. Choose a source to authorise</h3>
          {discovery.sources.length === 0 ? (
            <p className="muted">
              No data sources are registered yet. Register one in ROPA Sources first, or use the
              push-evidence option above.
            </p>
          ) : (
            <ul className="dsr-sources">
              {discovery.sources.map((s) => (
                <li key={s.data_source_id}>
                  <strong>{s.name}</strong> <span className="small">({s.connector})</span>{" "}
                  <span className={`pill pill-${s.readiness === "authorized" ? "ok" : usable(s) ? "warn" : "muted"}`}>
                    {READINESS_LABEL[s.readiness] ?? s.readiness}
                  </span>
                  <div className="muted small">{s.next_step}</div>
                  <button disabled={busy || !usable(s)} onClick={() => void chooseSource(s)}>
                    {s.readiness === "authorized" ? "Review authorisation" : "Authorise tables"}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      {source && schema && !schema.available && (
        <div className="banner banner-warn" role="alert">
          Could not read {schema.source_name}'s tables: {schema.message}. Check the source's
          connection in ROPA Sources, then choose it again.
        </div>
      )}

      {source && schema?.available && (
        <>
          <h3>4. Choose what DSR may use in {source.name}</h3>
          <p className="muted">
            Tick only the tables a data-subject request needs. For each, say which column holds
            the person's email, phone or reference, and which columns may be shown to them.
          </p>
          {schema.truncated && (
            <p className="muted small">Only part of a large schema is shown.</p>
          )}
          {schema.tables.map((t) => {
            const names = t.columns.map((c) => c.name);
            const choice = choices[t.name];
            return (
              <fieldset key={t.name}>
                <legend>
                  <label>
                    <input
                      type="checkbox"
                      checked={!!choice}
                      onChange={(e) => toggleTable(t.name, names, e.target.checked)}
                    />{" "}
                    {t.name}
                  </label>
                </legend>
                {choice && (
                  <div>
                    {IDENTIFIER_KINDS.map((kind) => (
                      <label key={kind}>
                        {kind} column{" "}
                        <select
                          aria-label={`${t.name} ${kind} column`}
                          value={choice.identifiers[kind] ?? ""}
                          onChange={(e) =>
                            patchChoice(t.name, {
                              identifiers: { ...choice.identifiers, [kind]: e.target.value },
                            })
                          }
                        >
                          <option value="">— none —</option>
                          {names.map((n) => <option key={n} value={n}>{n}</option>)}
                        </select>
                      </label>
                    ))}
                    <label>
                      Record key{" "}
                      <select
                        aria-label={`${t.name} record key`}
                        value={choice.keyColumn}
                        onChange={(e) => patchChoice(t.name, { keyColumn: e.target.value })}
                      >
                        {names.map((n) => <option key={n} value={n}>{n}</option>)}
                      </select>
                    </label>
                    <label>
                      <input
                        type="checkbox"
                        checked={choice.isIdentity}
                        onChange={(e) => patchChoice(t.name, { isIdentity: e.target.checked })}
                      />{" "}
                      One row = one person (identity table)
                    </label>
                    <div>
                      <div className="small">Columns that may be shown to the requester:</div>
                      {names.map((n) => (
                        <label key={n} className="small">
                          <input
                            type="checkbox"
                            aria-label={`${t.name} show ${n}`}
                            checked={choice.returnable.includes(n)}
                            onChange={() => patchChoice(t.name, { returnable: toggleIn(choice.returnable, n) })}
                          />{" "}
                          {n}{" "}
                        </label>
                      ))}
                    </div>
                    {allowExecution && (
                      <div>
                        <div className="small">Columns that may be changed or erased:</div>
                        {names.map((n) => (
                          <label key={n} className="small">
                            <input
                              type="checkbox"
                              aria-label={`${t.name} change ${n}`}
                              checked={choice.erasable.includes(n)}
                              onChange={() => patchChoice(t.name, { erasable: toggleIn(choice.erasable, n) })}
                            />{" "}
                            {n}{" "}
                          </label>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </fieldset>
            );
          })}

          <h3>5. Operations</h3>
          <label>
            <input
              type="checkbox"
              checked={allowExecution}
              onChange={(e) => setAllowExecution(e.target.checked)}
            />{" "}
            Allow DSR to change or erase data in this source (off by default)
          </label>
          <p className="muted small">
            Search and disclosure never need this. Every change still needs a reviewer's approval
            before it runs, and is checked by reading the record back afterwards.
          </p>
          {allowExecution && (
            <label>
              Write password (stored encrypted, never shown again){" "}
              <input
                type="password"
                autoComplete="new-password"
                aria-label="Write password"
                value={writeSecret}
                onChange={(e) => setWriteSecret(e.target.value)}
              />
            </label>
          )}

          <div className="dsr-actions">
            <button disabled={!canSave} onClick={() => void save()}>Save authorisation</button>
            {!everyTableHasIdentifier && chosenTables.length > 0 && (
              <span className="small muted">Every chosen table needs at least one identifier column.</span>
            )}
          </div>
        </>
      )}

      {saved && source && (
        <>
          <p className="pill pill-ok">Saved. {source.name} will be reused for every request.</p>
          <h3>6. Test the connection</h3>
          <button disabled={busy} onClick={() => void runTest()}>Test connection</button>
        </>
      )}

      {test && (
        <div className={`banner banner-${test.healthy ? "info" : "warn"}`} role="status">
          <p>
            <strong>{test.healthy ? "Healthy" : "Not healthy"}</strong>
            {test.error_code && <> — {test.error_code}</>}
          </p>
          <ul>
            {test.tables.map((t) => (
              <li key={t.table}>
                {t.table}: {t.ok ? "readable" : `problem (${t.error_code})`}
              </li>
            ))}
          </ul>
          <p className="small">Proves: {test.proves}.</p>
          <p className="small">Does not prove: {test.does_not_prove}.</p>
        </div>
      )}
    </section>
  );
}
