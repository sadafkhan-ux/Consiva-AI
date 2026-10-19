import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  dsrApi,
  type ActiveCaseSummary,
  type DsrAction,
  type DsrAuditEntry,
  type DsrCase,
  type DsrEvidence,
  type DsrExecution,
  type DsrPlan,
  type DsrResponseDoc,
  type DsrSearchRun,
  type IdentityChallenge,
  type ManualVerificationMethod,
} from "../api/dsr";
import {
  CaseId,
  CodeEntry,
  CodePanel,
  DuplicateNotice,
  ManualVerifyForm,
  Notice,
  SourceWarning,
  TypeSelector,
  WhyDisabled,
} from "./dsrShared";
import {
  REQUEST_TYPE_OPTIONS,
  canDraftResponse,
  caseStatusLabel,
  formatDate,
  identityLabel,
  identityVerified,
  maskEmail,
  needsTypeSelection,
  plainStatus,
  requestTypeLabel,
  searchState,
  statusTone,
  useSourceStatus,
  visibleEvidence,
  wrongCodeMessage,
} from "./dsrLogic";

/**
 * Agent 3 (DSR Fulfillment) operator console.
 *
 * Reuses the existing shell, styles and status-pill classes rather than
 * introducing a design of its own -- consistency across the six agents
 * matters more here than a bespoke look for this one.
 *
 * The one rule it holds to strictly (prompt §33/§34): it never shows progress the
 * backend did not report. Buttons are enabled from `allowed_transitions`, which the
 * backend computes from the same state machine it enforces, so the UI cannot offer
 * an action the server would refuse. Nothing here optimistically advances a status.
 *
 * The list defaults to sorting by SLA urgency, not creation time: a DSR case is a
 * legal deadline before it's anything else, so the case closest to breach is what an
 * operator should see first, every time they open this screen.
 */

type Tab = "evidence" | "plan" | "executions" | "response" | "audit";

// How often to re-poll the case list while one is still open/in-flight. Same
// cadence as ROPA's run poll (RopaDashboard.tsx) -- fast enough to feel live,
// slow enough not to hammer the API from an idle tab.
const ACTIVE_CASE_POLL_MS = 4000;

/** `sla.remaining_seconds` is the backend's own clock, never recomputed here
 *  (see DsrSla's own comment in api/dsr.ts) -- this only formats it. */
function formatSla(sla: DsrCase["sla"]): string {
  if (sla.closed) return "closed";
  if (sla.breached) return "breached";
  const s = sla.remaining_seconds;
  if (s <= 0) return "overdue";
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h left`;
  if (hours > 0) return `${hours}h ${minutes}m left`;
  return `${minutes}m left`;
}

function slaTone(sla: DsrCase["sla"]): string {
  if (sla.closed) return "muted";
  if (sla.breached || sla.overdue) return "bad";
  if (sla.remaining_seconds < 24 * 3600) return "warn";
  return "ok";
}

interface Props {
  /** A case to open on arrival (from the address after a reload). Read once, on mount. */
  initialCaseId?: string | null;
  /** Reports the open case so the address can follow it. */
  onCaseChange?: (id: string | null) => void;
  onOpenConfig?: () => void;
}

export function DsrDashboard({ initialCaseId = null, onCaseChange, onOpenConfig }: Props = {}) {
  const [cases, setCases] = useState<DsrCase[]>([]);
  const [selected, setSelected] = useState<DsrCase | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  // Mirrors ROPA's selectedRef pattern: read inside the poll interval without
  // putting `selected` in that effect's dependency list, so polling doesn't
  // restart every time an operator clicks a different case.
  const selectedRef = useRef<DsrCase | null>(null);
  selectedRef.current = selected;
  const [searchRuns, setSearchRuns] = useState<DsrSearchRun[]>([]);
  const [evidence, setEvidence] = useState<DsrEvidence[]>([]);
  const [plan, setPlan] = useState<DsrPlan | null>(null);
  const [executions, setExecutions] = useState<DsrExecution[]>([]);
  const [response, setResponse] = useState<DsrResponseDoc | null>(null);
  const [audit, setAudit] = useState<DsrAuditEntry[]>([]);
  const [tab, setTab] = useState<Tab>("evidence");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Distinct from `cases.length === 0`: without this, the very first render --
  // before loadCases() has even resolved once -- is indistinguishable from a
  // genuinely empty list and shows "No DSR cases yet." while still loading.
  const [casesLoading, setCasesLoading] = useState(true);

  // Intake form
  const [rawRequest, setRawRequest] = useState("");
  const [email, setEmail] = useState("");
  // "" means let the system classify the words; a chosen type is recorded as a person's decision.
  const [requestType, setRequestType] = useState("");
  const [duplicates, setDuplicates] = useState<ActiveCaseSummary[] | null>(null);
  const [issued, setIssued] = useState<IdentityChallenge | null>(null);
  const [showManual, setShowManual] = useState(false);
  const sources = useSourceStatus();

  const loadCases = useCallback(async () => {
    try {
      const rows = await dsrApi.listCases();
      setCases(rows);
      // If the case currently open just advanced (e.g. searching -> search_completed),
      // refresh it so the detail pane reflects that without a manual click.
      const current = selectedRef.current;
      if (current) {
        const updated = rows.find((c) => c.id === current.id);
        if (updated && updated.status !== current.status) setSelected(updated);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load cases.");
    } finally {
      setCasesLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCases();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reopen the case named in the address (a reload), then keep the address in step with the
  // case that is open. Both read from the backend: the address holds only an id.
  useEffect(() => {
    if (initialCaseId) void loadCase(initialCaseId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    onCaseChange?.(selected?.id ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id]);

  // Live status: while any case is still open, keep polling so SLA countdowns
  // and in-flight status (searching, executing, ...) advance without the
  // operator hitting Refresh themselves. Mirrors RopaDashboard's active-run poll.
  useEffect(() => {
    const hasOpenCase = cases.some((c) => !c.is_terminal);
    if (!hasOpenCase) return;
    const id = setInterval(() => void loadCases(), ACTIVE_CASE_POLL_MS);
    return () => clearInterval(id);
  }, [cases, loadCases]);

  const filteredCases = useMemo(() => {
    const q = query.trim().toLowerCase();
    return cases
      .filter((c) => statusFilter === "all" || c.status === statusFilter)
      .filter(
        (c) =>
          !q ||
          c.reference.toLowerCase().includes(q) ||
          c.requester_email?.toLowerCase().includes(q) ||
          c.raw_request.toLowerCase().includes(q) ||
          c.request_type.toLowerCase().includes(q),
      )
      .sort((a, b) => {
        // Open cases first, soonest-due first -- the case nearest breach is
        // what an operator needs to see, not whichever was created last.
        if (a.is_terminal !== b.is_terminal) return a.is_terminal ? 1 : -1;
        if (!a.is_terminal && !b.is_terminal) {
          return a.sla.remaining_seconds - b.sla.remaining_seconds;
        }
        return (b.closed_at ?? "").localeCompare(a.closed_at ?? "");
      });
  }, [cases, query, statusFilter]);

  const statusOptions = useMemo(
    () => [...new Set(cases.map((c) => c.status))].sort(),
    [cases],
  );

  const loadCase = useCallback(async (id: string) => {
    setBusy(true);
    setError(null);
    try {
      const [fresh, results, planData, execs, resp, auditRows] = await Promise.all([
        dsrApi.getCase(id),
        dsrApi.getResults(id).catch(() => ({ search_runs: [], evidence: [] })),
        dsrApi.getPlan(id).catch(() => null),
        dsrApi.getExecutions(id).catch(() => []),
        dsrApi.getResponse(id).catch(() => null),
        dsrApi.getAudit(id).catch(() => []),
      ]);
      // A code or form belongs to the case it was made for.
      if (selectedRef.current?.id !== fresh.id) { setIssued(null); setShowManual(false); }
      setSelected(fresh);
      setSearchRuns(results.search_runs);
      setEvidence(results.evidence);
      setPlan(planData);
      setExecutions(execs);
      setResponse(resp);
      setAudit(auditRows);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the case.");
    } finally {
      setBusy(false);
    }
  }, []);

  /** Every mutating action funnels through here so a failure always surfaces as a
   *  message and the case is always re-read from the backend afterwards. */
  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(label);
      if (selected) await loadCase(selected.id);
      await loadCases();
    } catch (err) {
      setError(err instanceof Error ? err.message : `${label} failed.`);
    } finally {
      setBusy(false);
    }
  }

  async function createCase(e: React.FormEvent, overrideReason?: string) {
    e.preventDefault();
    if (!rawRequest.trim() || !email.trim()) return;
    setBusy(true);
    setError(null);
    try {
      // Same rule as the New request tab: one open case per requester unless a person says why.
      if (!overrideReason) {
        const open = await dsrApi.activeCases(email.trim().toLowerCase()).catch(() => [] as ActiveCaseSummary[]);
        if (open.length > 0) { setDuplicates(open); return; }
      }
      setDuplicates(null);
      let created = await dsrApi.createCase({
        raw_request: rawRequest.trim(),
        requester_email: email.trim(),
        ...(overrideReason ? { duplicate_override_reason: overrideReason } : {}),
      });
      let typeError: string | null = null;
      if (requestType) {
        // A type chosen here is a person's decision, recorded as such by the backend.
        try { created = await dsrApi.classify(created.id, requestType); }
        catch (err) { typeError = err instanceof Error ? err.message : "could not set the type"; }
      }
      setRawRequest("");
      setEmail("");
      setRequestType("");
      await loadCases();
      await loadCase(created.id);
      if (typeError) setError(`The case was opened, but its type could not be set: ${typeError}`);
      else setNotice(`Case ${created.reference} created and classified as "${created.request_type}".`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the case.");
    } finally {
      setBusy(false);
    }
  }

  async function applyType(type: string) {
    if (!selected) return;
    await act("Request type set.", () => dsrApi.classify(selected.id, type));
  }

  async function submitCode(code: string) {
    if (!selected) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const out = await dsrApi.verifyIdentity(selected.id, { challenge: code });
      await loadCase(selected.id);
      await loadCases();
      if (out.satisfied) { setIssued(null); setNotice("Identity verified."); }
      else setError(wrongCodeMessage(out));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not check the code.");
    } finally {
      setBusy(false);
    }
  }

  async function verifyManually(v: { method: ManualVerificationMethod; reference: string; note: string }) {
    if (!selected) return;
    await act("Identity verified manually.", () =>
      dsrApi.verifyIdentity(selected.id, {
        manual: true, method: v.method, reference: v.reference, evidence_note: v.note,
      }),
    );
    setShowManual(false);
  }

  const can = (status: string) => selected?.allowed_transitions.includes(status) ?? false;
  const identityOk = identityVerified(selected);
  const codeReady = issued !== null || selected?.identity_status === "in_progress";
  const drafting = selected ? canDraftResponse(selected, searchRuns) : false;
  // Why each unavailable action is unavailable, written out on the page.
  const reasons: string[] = [];
  if (selected && !selected.is_terminal) {
    if (!identityOk) reasons.push("Start search is off until the requester's identity is verified.");
    else if (!can("searching")) reasons.push(`Start search is not available while the case is "${caseStatusLabel(selected)}".`);
    if (!can("executing")) reasons.push("Execute approved actions needs an approved plan.");
    if (!drafting) reasons.push("Generate response is off until the requester is verified and a search has finished.");
    if (!response) reasons.push("Mark sent & close needs a drafted response first.");
    else if (!can("completed")) reasons.push("Mark sent & close is off until every approved action has an outcome.");
  }

  return (
    <div className="dsr">
      <section className="card">
        <h2>New data subject request</h2>
        <SourceWarning status={sources} onOpenConfig={onOpenConfig} />
        <form className="dsr-intake" onSubmit={(e) => void createCase(e)}>
          <label>
            The requester's own words
            <textarea
              rows={2}
              value={rawRequest}
              placeholder="e.g. Please delete all my personal information."
              onChange={(e) => setRawRequest(e.target.value)}
            />
          </label>
          <label>
            Requester email
            <input
              type="email"
              value={email}
              placeholder="person@example.com"
              onChange={(e) => { setEmail(e.target.value); setDuplicates(null); }}
            />
          </label>
          <label>
            Request type
            <select value={requestType} onChange={(e) => setRequestType(e.target.value)}>
              <option value="">Detect from the words</option>
              {REQUEST_TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={busy || !rawRequest.trim() || !email.trim()}>
            Create case
          </button>
        </form>
        {duplicates && (
          <DuplicateNotice
            existing={duplicates}
            busy={busy}
            onOpen={(id) => { setDuplicates(null); void loadCase(id); }}
            onProceed={(reason) => void createCase({ preventDefault() {} } as React.FormEvent, reason)}
            onCancel={() => setDuplicates(null)}
          />
        )}
      </section>

      {error && <Notice tone="error">{error}</Notice>}
      {notice && <Notice tone="ok">{notice}</Notice>}

      <div className="dsr-layout">
        <aside className="card dsr-list">
          <h3>
            Cases ({filteredCases.length}
            {filteredCases.length !== cases.length ? ` of ${cases.length}` : ""})
          </h3>
          <div className="dsr-list-filters">
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search reference, email, request…"
            />
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="all">All statuses</option>
              {statusOptions.map((s) => (
                <option key={s} value={s}>
                  {plainStatus(s)}
                </option>
              ))}
            </select>
          </div>
          {casesLoading && <p className="muted">Loading cases…</p>}
          {!casesLoading && cases.length === 0 && <p className="muted">No DSR cases yet.</p>}
          {!casesLoading && cases.length > 0 && filteredCases.length === 0 && (
            <p className="muted">No case matches this search.</p>
          )}
          <ul>
            {filteredCases.map((c) => (
              <li key={c.id}>
                <button
                  className={selected?.id === c.id ? "active" : ""}
                  onClick={() => void loadCase(c.id)}
                >
                  <div className="dsr-list-row">
                    <strong>{c.reference}</strong>
                    <span className={`pill pill-${slaTone(c.sla)}`}>{formatSla(c.sla)}</span>
                  </div>
                  <div className="dsr-list-row">
                    <span className={`pill pill-${statusTone(c.status, c.error_code)}`} title={c.status}>
                      {caseStatusLabel(c)}
                    </span>
                    <span className="muted small">{requestTypeLabel(c.request_type)}</span>
                  </div>
                  <div className="dsr-list-meta muted small">
                    {maskEmail(c.requester_email)} · Created {formatDate(c.created_at)} · Due {formatDate(c.sla.due_at)}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        {selected && (
          <section className="card dsr-detail">
            <header className="dsr-head">
              <div>
                <h2>{selected.reference}</h2>
                <CaseId id={selected.id} />
                <p className="muted">{selected.raw_request}</p>
              </div>
              <div className="dsr-badges">
                {/* One plain label. The stored status is in the tooltip; the portal's own name for
                    it is in the API, not a second pill here that reads differently. */}
                <span
                  className={`pill pill-${statusTone(selected.status, selected.error_code)}`}
                  title={`${selected.status} / ${selected.public_status ?? ""}`}
                >
                  {caseStatusLabel(selected)}
                </span>
                <span className="pill">{requestTypeLabel(selected.request_type)}</span>
                <span className={`pill pill-${identityOk ? "ok" : "warn"}`}>
                  Identity: {identityLabel(selected.identity_status)}
                </span>
                <span className={`pill pill-${slaTone(selected.sla)}`}>{formatSla(selected.sla)}</span>
                <span className="pill pill-muted">Due {formatDate(selected.sla.due_at)}</span>
              </div>
            </header>

            {/* A stopped case always says why. The backend guarantees an error code
                accompanies every bad ending, so this is never an empty box. */}
            {selected.status_message && !selected.is_terminal && (
              <Notice tone="info">
                <strong>{selected.status_message}</strong>
                {selected.next_step && <> {selected.next_step}</>}
              </Notice>
            )}
            {selected.error_code && (
              <Notice tone="warn">
                <strong>{selected.error_code}</strong>
                {selected.error_detail && <> — {selected.error_detail}</>}
              </Notice>
            )}

            {needsTypeSelection(selected) && (
              <TypeSelector busy={busy} onApply={(t) => void applyType(t)} />
            )}

            <div className="dsr-actions">
              <button
                disabled={busy || identityOk}
                onClick={() =>
                  void act("Code generated. No email was sent -- give it to the requester.", async () => {
                    // No email provider is configured, so the operator passes the code on. It is
                    // shown inline (below), once -- never in a browser prompt.
                    setIssued(await dsrApi.startChallenge(selected.id));
                  })
                }
              >
                Generate code
              </button>
              <button
                disabled={busy || identityOk}
                aria-expanded={showManual}
                onClick={() => setShowManual((v) => !v)}
              >
                Verify manually
              </button>
              <button
                disabled={busy || !identityOk || !can("searching")}
                onClick={() => void act("Search queued.", () => dsrApi.startSearch(selected.id))}
              >
                Start search
              </button>
              <button
                disabled={busy || !can("executing")}
                onClick={() => void act("Execution queued.", () => dsrApi.execute(selected.id))}
              >
                Execute approved actions
              </button>
              {selected.retryable && selected.error_code === "ACTION_FAILED" && (
                <button
                  disabled={busy}
                  title="Reopen the failed actions for another attempt; then press Execute"
                  onClick={() =>
                    void act("Failed actions reopened -- review, then execute.", () =>
                      dsrApi.retryExecution(selected.id),
                    )
                  }
                >
                  Retry failed actions
                </button>
              )}
              <button
                disabled={busy || !drafting}
                onClick={() =>
                  void act("Response generated.", () => dsrApi.generateResponse(selected.id))
                }
              >
                Generate response
              </button>
              <button
                disabled={busy || !can("completed") || !response}
                onClick={() => void act("Case completed.", () => dsrApi.complete(selected.id))}
              >
                Mark sent &amp; close
              </button>
              <button disabled={busy} onClick={() => void loadCase(selected.id)}>
                Refresh
              </button>
            </div>
            <WhyDisabled reasons={reasons} />

            {!identityOk && !selected.is_terminal && (
              <>
                {issued && (
                  <CodePanel
                    challenge={issued.challenge} expiresAt={issued.expires_at} emailed={issued.emailed}
                    onDismiss={() => setIssued(null)}
                  />
                )}
                <CodeEntry enabled={codeReady} busy={busy} onSubmit={(c) => void submitCode(c)} />
                {showManual && (
                  <ManualVerifyForm
                    busy={busy}
                    onSubmit={(v) => void verifyManually(v)}
                    onCancel={() => setShowManual(false)}
                  />
                )}
              </>
            )}

            <div role="tablist" aria-label="Case details" className="tabs">
              {(["evidence", "plan", "executions", "response", "audit"] as Tab[]).map((t) => (
                <button
                  key={t}
                  id={`case-tab-${t}`}
                  role="tab"
                  type="button"
                  aria-selected={tab === t}
                  aria-controls="case-panel"
                  tabIndex={tab === t ? 0 : -1}
                  className={tab === t ? "active" : ""}
                  onClick={() => setTab(t)}
                >
                  {t}
                </button>
              ))}
            </div>

            <div role="tabpanel" id="case-panel" aria-labelledby={`case-tab-${tab}`}>
            {tab === "evidence" && (
              <EvidenceTab runs={searchRuns} evidence={evidence} kase={selected} />
            )}
            {tab === "plan" && (
              <PlanTab
                plan={plan}
                busy={busy}
                onDecide={(action, decision) => {
                  const reason =
                    decision === "approved" && action.risk !== "high"
                      ? null
                      : window.prompt(`Reason for ${decision}:`);
                  if (decision !== "approved" && !reason) return;
                  if (action.risk === "high" && !reason) return;
                  void act(`Action ${decision}.`, () =>
                    dsrApi.decideAction(selected.id, action.id, decision, reason),
                  );
                }}
              />
            )}
            {tab === "executions" && <ExecutionsTab executions={executions} />}
            {tab === "response" && <ResponseTab response={response} />}
            {tab === "audit" && <AuditTab entries={audit} />}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}

/** What the Evidence tab says, decided by the search runs the backend recorded. An empty
 *  evidence list is "no match" only when a search actually finished -- with no run, or only failed
 *  runs, it is "nothing was searched", which is a different (and opposite) claim for an operator to
 *  pass on to a requester. */
function EvidenceTab({
  runs,
  evidence,
  kase,
}: {
  runs: DsrSearchRun[];
  evidence: DsrEvidence[];
  kase: DsrCase;
}) {
  const state = searchState(runs, evidence, kase.status);
  const shown = visibleEvidence(runs, evidence);
  return (
    <>
      <h3>Searches</h3>
      {state === "not_run" && (
        <Notice tone="warn">
          <strong>No search has been run for this case.</strong> {kase.status_message}
          {kase.next_step && <> <strong>Next step:</strong> {kase.next_step}</>}
        </Notice>
      )}
      {state === "running" && <Notice tone="info">A search is in progress. Results appear here when it finishes.</Notice>}
      {state === "failed" && (
        <Notice tone="warn">
          The search did not complete -- it failed with {kase.error_code ?? "an unrecorded error"}. This is
          not the same as finding no data: see the error above, resolve it, and retry the search.
        </Notice>
      )}
      {state === "partial" && (
        <Notice tone="warn">
          Some sources could not be searched, so the results below are incomplete. This is not the same as
          finding no data in those sources: retry the search.
        </Notice>
      )}
      {runs.length > 0 && (
        <table className="data">
          <thead>
            <tr>
              <th>Source</th><th>Status</th><th>Matches</th><th>Subjects</th><th>Tables</th><th>Problem</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td>{r.source}</td>
                <td><span className={`pill pill-${r.status === "completed" ? "ok" : r.status === "failed" ? "bad" : "warn"}`}>{r.status}</span></td>
                <td>{r.matches}</td>
                <td>{r.distinct_subjects}</td>
                <td className="muted small">{r.tables_searched.join(", ") || "—"}</td>
                <td className="small">{r.error_code ? `${r.error_code}: ${r.error_detail ?? ""}` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Evidence ({shown.length})</h3>
      {state === "no_match" && (
        <p className="muted">
          No matching records found. The search completed and found no data for this person in the
          systems searched.
        </p>
      )}
      {shown.length > 0 && (
        <table className="data">
          <thead>
            <tr><th>Where</th><th>Matched on</th><th>Type</th><th>Record</th><th>Data held</th></tr>
          </thead>
          <tbody>
            {shown.map((e) => (
              <tr key={e.id}>
                <td>{e.source}/{e.table}</td>
                <td>{e.matched_column} <span className="muted small">({e.identifier_kind})</span></td>
                <td><span className="pill">{e.match_type}</span></td>
                <td className="mono small">{JSON.stringify(e.record_reference)}</td>
                <td className="mono small">{e.record_snapshot ? JSON.stringify(e.record_snapshot) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function PlanTab({
  plan,
  busy,
  onDecide,
}: {
  plan: DsrPlan | null;
  busy: boolean;
  onDecide: (action: DsrAction, decision: string) => void;
}) {
  if (!plan?.plan) return <p className="muted">No action plan yet. Run a search first.</p>;
  const d = plan.decisions;
  return (
    <>
      <p><strong>v{plan.plan.version}</strong> — {plan.plan.summary}</p>
      {d && (
        <p className="muted small">
          {d.approved} approved · {d.rejected} rejected · {d.blocked} blocked ·{" "}
          {d.awaiting_decision} awaiting decision ·{" "}
          {d.ready_to_execute ? "ready to execute" : "not ready to execute"}
        </p>
      )}
      <table className="data">
        <thead>
          <tr><th>Operation</th><th>Target</th><th>Reason</th><th>Expected</th><th>Risk</th><th>Status</th><th /></tr>
        </thead>
        <tbody>
          {plan.actions.map((a) => (
            <tr key={a.id}>
              <td><span className="pill">{a.operation}</span></td>
              <td>{a.source}/{a.table} <span className="mono small">{JSON.stringify(a.record_reference)}</span></td>
              <td className="small">{a.reason}</td>
              <td className="small">{a.expected_result}</td>
              <td><span className={`pill pill-${a.risk === "high" ? "bad" : a.risk === "medium" ? "warn" : "ok"}`}>{a.risk}</span></td>
              <td>
                <span className={`pill pill-${a.status === "executed" ? "ok" : a.status === "blocked" || a.status === "failed" ? "bad" : "muted"}`}>
                  {a.status}
                </span>
                {/* Both texts, labelled. The reviewer needs the configuration
                    detail to act on; they also need to see exactly what the data
                    subject will be told, because that is what leaves the building. */}
                {a.blocked_reason && (
                  <div className="small muted">
                    <strong>Internal:</strong> {a.blocked_reason}
                  </div>
                )}
                {a.requester_explanation && (
                  <div className="small requester-text">
                    <strong>Requester sees:</strong> {a.requester_explanation}
                  </div>
                )}
              </td>
              <td>
                {a.status === "proposed" && a.requires_approval && (
                  <>
                    <button disabled={busy} onClick={() => onDecide(a, "approved")}>Approve</button>
                    <button disabled={busy} onClick={() => onDecide(a, "rejected")}>Reject</button>
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function ExecutionsTab({ executions }: { executions: DsrExecution[] }) {
  if (executions.length === 0) return <p className="muted">Nothing has been executed.</p>;
  return (
    <table className="data">
      <thead>
        <tr><th>Status</th><th>Rows affected</th><th>Verified</th><th>Detail</th><th>Attempts</th></tr>
      </thead>
      <tbody>
        {executions.map((e) => (
          <tr key={e.id}>
            <td><span className={`pill pill-${e.status === "verified" ? "ok" : e.status === "failed" ? "bad" : "warn"}`}>{e.status}</span></td>
            <td>{e.rows_affected ?? "—"}</td>
            {/* Shown separately from status on purpose: a write that ran and a
                read-back that confirmed it are different claims (§23). */}
            <td>
              <span className={`pill pill-${e.verification_status === "passed" ? "ok" : "bad"}`}>
                {e.verification_status ?? "—"}
              </span>
            </td>
            <td className="mono small">
              {e.error_code ? `${e.error_code}: ${e.error_detail ?? ""}` : JSON.stringify(e.verification_detail ?? {})}
            </td>
            <td>{e.attempts}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ResponseTab({ response }: { response: DsrResponseDoc | null }) {
  if (!response) return <p className="muted">No response drafted yet.</p>;
  return (
    <>
      <p className="muted small">
        v{response.version} · {response.status} ·{" "}
        {response.drafted_by_model
          ? `drafted by ${response.drafted_by_model}`
          : "assembled deterministically from evidence — no model in this path"}
      </p>
      <pre className="response-body">{response.body_text}</pre>
      <h3>Grounded facts ({response.grounded_facts.length})</h3>
      <p className="muted small">
        Every claim above traces to one of these rows. A sentence that does not appear
        here is not a DSR fact.
      </p>
      <pre className="mono small">{JSON.stringify(response.grounded_facts, null, 2)}</pre>
    </>
  );
}

function AuditTab({ entries }: { entries: DsrAuditEntry[] }) {
  if (entries.length === 0) return <p className="muted">No audit entries.</p>;
  return (
    <ol className="timeline">
      {entries.map((e) => (
        <li key={e.id}>
          <span className="mono small">{e.created_at?.slice(0, 19).replace("T", " ")}</span>{" "}
          <strong>{e.action}</strong>
          {e.after && <pre className="mono small">{JSON.stringify(e.after)}</pre>}
        </li>
      ))}
    </ol>
  );
}
