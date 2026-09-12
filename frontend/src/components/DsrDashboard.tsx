import { useCallback, useEffect, useState } from "react";
import {
  dsrApi,
  type DsrAction,
  type DsrAuditEntry,
  type DsrCase,
  type DsrEvidence,
  type DsrExecution,
  type DsrPlan,
  type DsrResponseDoc,
  type DsrSearchRun,
} from "../api/dsr";

/**
 * Agent 3 (DSR Fulfillment) testing UI.
 *
 * Deliberately small -- this exists to drive the workflow end to end, not to be the
 * final product surface. It reuses the existing shell, styles and status-pill
 * classes rather than introducing a design of its own.
 *
 * The one rule it holds to strictly (prompt §33/§34): it never shows progress the
 * backend did not report. Buttons are enabled from `allowed_transitions`, which the
 * backend computes from the same state machine it enforces, so the UI cannot offer
 * an action the server would refuse. Nothing here optimistically advances a status.
 */

type Tab = "evidence" | "plan" | "executions" | "response" | "audit";

const STATUS_TONE: Record<string, string> = {
  completed: "ok",
  execution_verified: "ok",
  identity_verified: "ok",
  failed: "bad",
  rejected: "bad",
  expired: "bad",
  partially_completed: "warn",
  escalated: "warn",
  review_required: "warn",
  approval_required: "warn",
  cancelled: "muted",
};

export function DsrDashboard() {
  const [cases, setCases] = useState<DsrCase[]>([]);
  const [selected, setSelected] = useState<DsrCase | null>(null);
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

  // Intake form
  const [rawRequest, setRawRequest] = useState("");
  const [email, setEmail] = useState("");

  const loadCases = useCallback(async () => {
    try {
      setCases(await dsrApi.listCases());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load cases.");
    }
  }, []);

  useEffect(() => {
    void loadCases();
  }, [loadCases]);

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

  async function createCase(e: React.FormEvent) {
    e.preventDefault();
    if (!rawRequest.trim() || !email.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const created = await dsrApi.createCase({
        raw_request: rawRequest.trim(),
        requester_email: email.trim(),
      });
      setRawRequest("");
      setEmail("");
      await loadCases();
      await loadCase(created.id);
      setNotice(`Case ${created.reference} created and classified as "${created.request_type}".`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the case.");
    } finally {
      setBusy(false);
    }
  }

  const can = (status: string) => selected?.allowed_transitions.includes(status) ?? false;
  const identityOk =
    selected?.identity_status === "verified" || selected?.identity_status === "manually_verified";

  return (
    <div className="dsr">
      <section className="card">
        <h2>New data subject request</h2>
        <form className="dsr-intake" onSubmit={createCase}>
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
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <button type="submit" disabled={busy || !rawRequest.trim() || !email.trim()}>
            Create case
          </button>
        </form>
      </section>

      {error && <div className="banner banner-error">{error}</div>}
      {notice && <div className="banner banner-ok">{notice}</div>}

      <div className="dsr-layout">
        <aside className="card dsr-list">
          <h3>Cases ({cases.length})</h3>
          {cases.length === 0 && <p className="muted">No DSR cases yet.</p>}
          <ul>
            {cases.map((c) => (
              <li key={c.id}>
                <button
                  className={selected?.id === c.id ? "active" : ""}
                  onClick={() => void loadCase(c.id)}
                >
                  <strong>{c.reference}</strong>
                  <span className={`pill pill-${STATUS_TONE[c.status] ?? "muted"}`}>{c.status}</span>
                  <span className="muted small">{c.request_type}</span>
                  {c.sla.breached && <span className="pill pill-bad">SLA</span>}
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
                <p className="muted">{selected.raw_request}</p>
              </div>
              <div className="dsr-badges">
                <span className={`pill pill-${STATUS_TONE[selected.status] ?? "muted"}`}>
                  {selected.status}
                </span>
                <span className="pill">{selected.request_type}</span>
                <span className={`pill pill-${identityOk ? "ok" : "warn"}`}>
                  identity: {selected.identity_status ?? "pending"}
                </span>
                {selected.sla.overdue && <span className="pill pill-bad">overdue</span>}
              </div>
            </header>

            {/* A stopped case always says why. The backend guarantees an error code
                accompanies every bad ending, so this is never an empty box. */}
            {selected.error_code && (
              <div className="banner banner-warn">
                <strong>{selected.error_code}</strong>
                {selected.error_detail && <> — {selected.error_detail}</>}
              </div>
            )}

            <div className="dsr-actions">
              <button
                disabled={busy || identityOk}
                onClick={() =>
                  void act("Identity challenge issued.", async () => {
                    const c = await dsrApi.startChallenge(selected.id);
                    // No email provider is configured, so the operator delivers it.
                    window.prompt(c.delivery_note, c.challenge);
                  })
                }
              >
                Issue identity challenge
              </button>
              <button
                disabled={busy || identityOk}
                onClick={() => {
                  const note = window.prompt("What did you check to verify this person?");
                  if (!note) return;
                  void act("Identity verified manually.", () =>
                    dsrApi.verifyIdentity(selected.id, { manual: true, evidence_note: note }),
                  );
                }}
              >
                Verify manually
              </button>
              <button
                disabled={busy || !identityOk || !can("searching")}
                title={!identityOk ? "Identity must be verified before any search" : undefined}
                onClick={() => void act("Search queued.", () => dsrApi.startSearch(selected.id))}
              >
                Start search
              </button>
              <button
                disabled={busy || !can("executing")}
                title={!can("executing") ? "Execution requires an approved plan" : undefined}
                onClick={() => void act("Execution queued.", () => dsrApi.execute(selected.id))}
              >
                Execute approved actions
              </button>
              <button
                disabled={busy}
                onClick={() =>
                  void act("Response generated.", () => dsrApi.generateResponse(selected.id))
                }
              >
                Generate response
              </button>
              <button
                disabled={busy || !can("completed") || !response}
                title={!response ? "Generate a response before completing" : undefined}
                onClick={() => void act("Case completed.", () => dsrApi.complete(selected.id))}
              >
                Mark sent &amp; close
              </button>
              <button disabled={busy} onClick={() => void loadCase(selected.id)}>
                Refresh
              </button>
            </div>

            <nav className="tabs">
              {(["evidence", "plan", "executions", "response", "audit"] as Tab[]).map((t) => (
                <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
                  {t}
                </button>
              ))}
            </nav>

            {tab === "evidence" && (
              <EvidenceTab runs={searchRuns} evidence={evidence} />
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
          </section>
        )}
      </div>
    </div>
  );
}

function EvidenceTab({ runs, evidence }: { runs: DsrSearchRun[]; evidence: DsrEvidence[] }) {
  return (
    <>
      <h3>Searches</h3>
      {runs.length === 0 && <p className="muted">No search has run yet.</p>}
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

      <h3>Evidence ({evidence.length})</h3>
      {evidence.length === 0 && <p className="muted">No records matched.</p>}
      <table className="data">
        <thead>
          <tr><th>Where</th><th>Matched on</th><th>Type</th><th>Record</th><th>Data held</th></tr>
        </thead>
        <tbody>
          {evidence.map((e) => (
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
                {/* A blocked action keeps its reason visible -- that is what makes a
                    partial fulfilment explainable to the requester. */}
                {a.blocked_reason && <div className="small muted">{a.blocked_reason}</div>}
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
