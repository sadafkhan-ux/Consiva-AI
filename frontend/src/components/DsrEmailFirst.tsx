import { useCallback, useEffect, useRef, useState } from "react";
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
  type DsrSelection,
} from "../api/dsr";

/**
 * Agent 3 — email-first data subject request.
 *
 * One email in, then a decision per record found. The screens map one-to-one onto
 * the backend's own case lifecycle rather than onto a wizard invented here: each
 * step advances only because the server said the case moved, and `allowed_transitions`
 * (computed from the same state machine the server enforces) decides what is
 * offered. Nothing is optimistically advanced, and no progress is shown that the
 * backend did not report.
 *
 * The underlying DSR case is unchanged — it is created behind the email lookup, and
 * every artefact (evidence, plan, approvals, executions, audit) still hangs off it.
 */

type Step = "email" | "verify" | "searching" | "data" | "review" | "approval" | "result";

const SELECTIONS: { value: DsrSelection; label: string; tone: string; hint: string }[] = [
  { value: "keep", label: "Keep", tone: "ok", hint: "Leave this record exactly as it is" },
  { value: "delete", label: "Delete", tone: "bad", hint: "Erase this record — needs approval" },
  { value: "correct", label: "Correct", tone: "warn", hint: "Change a value — needs approval" },
  { value: "export", label: "Export", tone: "info", hint: "Include this record in the response" },
  { value: "review", label: "Review", tone: "muted", hint: "Ask a person to decide" },
];

/** Values are masked by default. A reviewer can reveal one, but the page does not
 *  open showing somebody's address and phone number to whoever walks past. */
function mask(value: unknown): string {
  const text = value === null || value === undefined ? "" : String(value);
  if (!text) return "—";
  if (text.includes("@")) {
    const [local, domain] = text.split("@");
    return `${local.slice(0, 2)}${"•".repeat(Math.max(local.length - 2, 3))}@${domain}`;
  }
  if (text.length <= 4) return "•".repeat(text.length);
  return `${"•".repeat(Math.max(text.length - 4, 4))}${text.slice(-4)}`;
}

export function DsrEmailFirst() {
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [kase, setKase] = useState<DsrCase | null>(null);
  const [runs, setRuns] = useState<DsrSearchRun[]>([]);
  const [evidence, setEvidence] = useState<DsrEvidence[]>([]);
  const [plan, setPlan] = useState<DsrPlan | null>(null);
  const [executions, setExecutions] = useState<DsrExecution[]>([]);
  const [response, setResponse] = useState<DsrResponseDoc | null>(null);
  const [audit, setAudit] = useState<DsrAuditEntry[]>([]);
  const [choices, setChoices] = useState<Record<string, DsrSelection>>({});
  const [corrections, setCorrections] = useState<Record<string, Record<string, string>>>({});
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [showAudit, setShowAudit] = useState(false);
  const poll = useRef<number | null>(null);

  useEffect(() => () => { if (poll.current) window.clearInterval(poll.current); }, []);

  const refresh = useCallback(async (id: string) => {
    const [c, results, p, ex, resp] = await Promise.all([
      dsrApi.getCase(id),
      dsrApi.getResults(id).catch(() => ({ search_runs: [], evidence: [] })),
      dsrApi.getPlan(id).catch(() => null),
      dsrApi.getExecutions(id).catch(() => []),
      dsrApi.getResponse(id).catch(() => null),
    ]);
    setKase(c); setRuns(results.search_runs); setEvidence(results.evidence);
    setPlan(p); setExecutions(ex); setResponse(resp);
    return c;
  }, []);

  async function run<T>(label: string, fn: () => Promise<T>): Promise<T | null> {
    setBusy(true); setError(null); setNote(null);
    try { const out = await fn(); setNote(label); return out; }
    catch (err) { setError(err instanceof Error ? err.message : `${label} failed.`); return null; }
    finally { setBusy(false); }
  }

  // ── 1. Email ───────────────────────────────────────────────────────────────
  async function findMyData(e: React.FormEvent) {
    e.preventDefault();
    const address = email.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(address)) {
      setError("Enter a valid email address."); return;
    }
    const created = await run("Case opened.", () =>
      dsrApi.createCase({
        // The case still records what was asked for, in words -- the email is the
        // lookup key, not a replacement for the request itself.
        raw_request:
          "Self-service data request: the requester asked to see what personal data is " +
          "held about them and will choose an action for each record found.",
        requester_email: address,
      }),
    );
    if (created) { setKase(created); setStep("verify"); }
  }

  // ── 2. Verify ──────────────────────────────────────────────────────────────
  async function verifyManually() {
    if (!kase) return;
    const evidenceNote = window.prompt(
      "What did you check to confirm this person's identity?\n(Recorded on the case — required.)",
    );
    if (!evidenceNote) return;
    const out = await run("Identity verified.", () =>
      dsrApi.verifyIdentity(kase.id, { manual: true, evidence_note: evidenceNote }),
    );
    if (out?.satisfied) { setKase(out.case); startSearch(out.case.id); }
    else if (out) setKase(out.case);
  }

  async function sendChallenge() {
    if (!kase) return;
    const out = await run("Challenge issued.", () => dsrApi.startChallenge(kase.id));
    if (out) window.prompt(out.delivery_note, out.challenge);
    await refresh(kase.id);
  }

  async function submitChallenge() {
    if (!kase) return;
    const code = window.prompt("Enter the code the requester received:");
    if (!code) return;
    const out = await run("Code checked.", () =>
      dsrApi.verifyIdentity(kase.id, { challenge: code }),
    );
    if (out?.satisfied) { setKase(out.case); startSearch(out.case.id); }
    else if (out) { setKase(out.case); setError("That code did not match."); }
  }

  // ── 3. Search ──────────────────────────────────────────────────────────────
  async function startSearch(id: string) {
    setStep("searching");
    const ok = await run("Searching connected systems…", () => dsrApi.startSearch(id));
    if (!ok) { setStep("verify"); return; }
    if (poll.current) window.clearInterval(poll.current);
    poll.current = window.setInterval(async () => {
      const c = await refresh(id).catch(() => null);
      if (!c) return;
      // The backend decides when the search is done; this only watches for it.
      if (c.status !== "searching") {
        if (poll.current) window.clearInterval(poll.current);
        poll.current = null;
        setStep("data");
      }
    }, 2000);
  }

  // ── 4/5. Data + per-record action ──────────────────────────────────────────
  function choose(id: string, action: DsrSelection) {
    setChoices((prev) => ({ ...prev, [id]: action }));
  }

  const counts = SELECTIONS.reduce<Record<string, number>>((acc, s) => {
    acc[s.value] = Object.values(choices).filter((c) => c === s.value).length;
    return acc;
  }, {});
  const decided = Object.keys(choices).length;

  // ── 6. Review → build the plan on the server ───────────────────────────────
  async function submitForApproval() {
    if (!kase) return;
    const payload = evidence
      .filter((e) => choices[e.id])
      .map((e) => ({
        evidence_id: e.id,
        action: choices[e.id],
        corrections: choices[e.id] === "correct" ? corrections[e.id] : undefined,
      }));
    const built = await run("Plan submitted for approval.", () =>
      dsrApi.buildPlan(kase.id, payload),
    );
    if (built) { setPlan(built); await refresh(kase.id); setStep("approval"); }
  }

  // ── 7/8. Approval + execution ──────────────────────────────────────────────
  async function decide(action: DsrAction, decision: "approved" | "rejected") {
    if (!kase) return;
    const needsReason = decision === "rejected" || action.risk === "high";
    const reason = needsReason
      ? window.prompt(`Reason for ${decision} (required for ${action.risk}-risk actions):`)
      : null;
    if (needsReason && !reason) return;
    const out = await run(`Action ${decision}.`, () =>
      dsrApi.decideAction(kase.id, action.id, decision, reason),
    );
    if (out) { setKase(out.case); setPlan(await dsrApi.getPlan(kase.id)); }
  }

  async function execute() {
    if (!kase) return;
    const ok = await run("Execution queued.", () => dsrApi.execute(kase.id));
    if (!ok) return;
    if (poll.current) window.clearInterval(poll.current);
    poll.current = window.setInterval(async () => {
      const c = await refresh(kase.id).catch(() => null);
      if (c && c.status !== "executing" && c.status !== "approved") {
        if (poll.current) window.clearInterval(poll.current);
        poll.current = null;
        setStep("result");
      }
    }, 2000);
  }

  async function generateResponse() {
    if (!kase) return;
    const doc = await run("Response generated.", () => dsrApi.generateResponse(kase.id));
    if (doc) setResponse(doc);
  }

  async function loadAudit() {
    if (!kase) return;
    setAudit(await dsrApi.getAudit(kase.id));
    setShowAudit(true);
  }

  function startOver() {
    if (poll.current) window.clearInterval(poll.current);
    setStep("email"); setEmail(""); setKase(null); setEvidence([]); setRuns([]);
    setPlan(null); setExecutions([]); setResponse(null); setChoices({});
    setCorrections({}); setRevealed({}); setShowAudit(false); setError(null); setNote(null);
  }

  const identityOk =
    kase?.identity_status === "verified" || kase?.identity_status === "manually_verified";

  // Grouped source → table → record, which is how a reviewer reads it.
  const grouped = evidence.reduce<Record<string, Record<string, DsrEvidence[]>>>((acc, e) => {
    (acc[e.source] ??= {});
    (acc[e.source][e.table] ??= []).push(e);
    return acc;
  }, {});

  return (
    <div className="dsr ef">
      <ol className="stepper" aria-label="Progress">
        {(["email", "verify", "searching", "data", "review", "approval", "result"] as Step[])
          .map((s, i) => (
            <li key={s} className={step === s ? "now" : ""}>
              <span className="n">{i + 1}</span>
              <span>{s === "searching" ? "search" : s}</span>
            </li>
          ))}
      </ol>

      {error && <div className="banner banner-error">{error}</div>}
      {note && !error && <div className="banner banner-ok">{note}</div>}

      {kase && (
        <div className="case-strip">
          <span className="mono"><strong>{kase.reference}</strong></span>
          <span className={`pill pill-${kase.is_terminal ? "ok" : "muted"}`}>{kase.status}</span>
          <span className={`pill pill-${identityOk ? "ok" : "warn"}`}>
            identity: {kase.identity_status ?? "pending"}
          </span>
          {kase.error_code && <span className="pill pill-warn">{kase.error_code}</span>}
          <button className="linkish" onClick={startOver}>Start a new request</button>
        </div>
      )}

      {/* ── 1. Email ─────────────────────────────────────────────────────── */}
      {step === "email" && (
        <section className="card hero">
          <h2>Find my data</h2>
          <p className="muted">
            Enter the email address the person gave you. Nothing is searched until their
            identity has been verified.
          </p>
          <form className="email-form" onSubmit={findMyData}>
            <label htmlFor="dsr-email">Email address</label>
            <input
              id="dsr-email" type="email" value={email} autoComplete="off"
              placeholder="person@example.com"
              onChange={(e) => setEmail(e.target.value)}
            />
            <button type="submit" disabled={busy || !email.trim()}>Find my data</button>
          </form>
        </section>
      )}

      {/* ── 2. Verify ────────────────────────────────────────────────────── */}
      {step === "verify" && kase && (
        <section className="card hero">
          <h2>Verify the requester</h2>
          <p className="muted">
            This is a security boundary, not a formality. No record is read until it passes.
          </p>
          <div className="dsr-actions">
            <button disabled={busy} onClick={sendChallenge}>Email a code</button>
            <button disabled={busy} onClick={submitChallenge}>Enter their code</button>
            <button disabled={busy} onClick={verifyManually}>Verify manually</button>
          </div>
          <p className="small muted">
            No outbound email provider is configured, so a code is shown to you to deliver.
            Manual verification records who checked what.
          </p>
        </section>
      )}

      {/* ── 3. Searching ─────────────────────────────────────────────────── */}
      {step === "searching" && (
        <section className="card hero">
          <h2>Searching connected systems…</h2>
          <p className="muted">
            Only sources an administrator has authorized for DSR, and only the columns
            they allow-listed.
          </p>
          <div className="bar"><span /></div>
        </section>
      )}

      {/* ── 4/5. Data found + action per record ──────────────────────────── */}
      {step === "data" && (
        <>
          <section className="card">
            <h2>Data found</h2>
            {runs.map((r) => (
              <p key={r.id} className="small muted">
                {r.source}: <strong>{r.status}</strong> · {r.matches} record(s) ·
                {" "}{r.tables_searched.join(", ") || "no tables"}
                {r.error_code && <> · <span className="pill pill-warn">{r.error_code}</span> {r.error_detail}</>}
              </p>
            ))}
            {evidence.length === 0 && (
              <p className="muted">
                No record matched that email in the systems we searched. Nothing will be changed.
              </p>
            )}
          </section>

          {Object.entries(grouped).map(([source, tables]) => (
            <section className="card" key={source}>
              <h2>{source}</h2>
              {Object.entries(tables).map(([table, rows]) => (
                <div key={table} className="table-group">
                  <h3>{table} <span className="count">{rows.length} record(s)</span></h3>
                  {rows.map((e) => (
                    <div className="record" key={e.id}>
                      <div className="fields">
                        {Object.entries(e.record_snapshot ?? {}).map(([k, v]) => (
                          <div className="field" key={k}>
                            <span className="k">{k}</span>
                            <span className="v mono">
                              {revealed[e.id] ? String(v ?? "—") : mask(v)}
                            </span>
                          </div>
                        ))}
                        {!e.record_snapshot && (
                          <div className="field"><span className="k">record</span>
                            <span className="v mono">{JSON.stringify(e.record_reference)}</span></div>
                        )}
                        <button
                          className="linkish"
                          onClick={() => setRevealed((p) => ({ ...p, [e.id]: !p[e.id] }))}
                        >
                          {revealed[e.id] ? "Hide values" : "Reveal values"}
                        </button>
                      </div>

                      <div className="choices" role="group" aria-label={`Action for ${table} record`}>
                        {SELECTIONS.map((s) => (
                          <button
                            key={s.value}
                            title={s.hint}
                            className={`choice ${choices[e.id] === s.value ? `on on-${s.tone}` : ""}`}
                            disabled={busy}
                            onClick={() => choose(e.id, s.value)}
                          >
                            {s.label}
                          </button>
                        ))}
                      </div>

                      {choices[e.id] === "correct" && (
                        <div className="correct-box">
                          {Object.keys(e.record_snapshot ?? {}).map((col) => (
                            <label key={col}>
                              <span className="k">{col}</span>
                              <input
                                id={`fix-${e.id}-${col}`}
                                placeholder="new value (leave blank to keep)"
                                value={corrections[e.id]?.[col] ?? ""}
                                onChange={(ev) =>
                                  setCorrections((p) => ({
                                    ...p,
                                    [e.id]: { ...(p[e.id] ?? {}), [col]: ev.target.value },
                                  }))
                                }
                              />
                            </label>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              ))}
            </section>
          ))}

          {evidence.length > 0 && (
            <section className="card summary-bar">
              <div className="tallies">
                {SELECTIONS.map((s) => (
                  <span key={s.value} className={`tally ${counts[s.value] ? `on-${s.tone}` : ""}`}>
                    <b>{counts[s.value]}</b> {s.label.toLowerCase()}
                  </span>
                ))}
              </div>
              <div className="grow" />
              <span className="small muted">{decided} of {evidence.length} decided</span>
              <button disabled={busy || decided === 0} onClick={() => setStep("review")}>
                Review actions
              </button>
            </section>
          )}
        </>
      )}

      {/* ── 6. Review ────────────────────────────────────────────────────── */}
      {step === "review" && (
        <section className="card">
          <h2>Review before anything happens</h2>
          <p className="muted">Nothing has been executed. This is what will be submitted.</p>
          <table className="data">
            <thead><tr><th>Where</th><th>Record</th><th>Action</th></tr></thead>
            <tbody>
              {evidence.filter((e) => choices[e.id]).map((e) => (
                <tr key={e.id}>
                  <td>{e.source}/{e.table}</td>
                  <td className="mono small">{JSON.stringify(e.record_reference)}</td>
                  <td>
                    <span className={`pill pill-${SELECTIONS.find((s) => s.value === choices[e.id])?.tone}`}>
                      {choices[e.id]}
                    </span>
                    {choices[e.id] === "correct" && (
                      <span className="small muted"> {JSON.stringify(corrections[e.id] ?? {})}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="dsr-actions">
            <button disabled={busy} onClick={() => setStep("data")}>Back</button>
            <button disabled={busy} onClick={submitForApproval}>Submit for approval</button>
          </div>
        </section>
      )}

      {/* ── 7. Approval + execution ──────────────────────────────────────── */}
      {step === "approval" && plan?.plan && (
        <section className="card">
          <h2>Approval</h2>
          <p className="muted">{plan.plan.summary}</p>
          {plan.decisions && (
            <p className="small muted">
              {plan.decisions.approved} approved · {plan.decisions.rejected} rejected ·
              {" "}{plan.decisions.blocked} blocked · {plan.decisions.awaiting_decision} awaiting
              {" "}· {plan.decisions.ready_to_execute ? "ready to execute" : "not ready"}
            </p>
          )}
          <table className="data">
            <thead><tr><th>Where</th><th>Operation</th><th>Risk</th><th>Status</th><th /></tr></thead>
            <tbody>
              {plan.actions.map((a) => (
                <tr key={a.id}>
                  <td>{a.source}/{a.table}</td>
                  <td><span className="pill">{a.operation}</span></td>
                  <td><span className={`pill pill-${a.risk === "high" ? "bad" : a.risk === "medium" ? "warn" : "ok"}`}>{a.risk}</span></td>
                  <td>
                    <span className={`pill pill-${a.status === "executed" ? "ok" : a.status === "blocked" || a.status === "failed" ? "bad" : "muted"}`}>
                      {a.status}
                    </span>
                    {a.blocked_reason && <div className="small muted"><strong>Internal:</strong> {a.blocked_reason}</div>}
                    {a.requester_explanation && <div className="small requester-text"><strong>Requester sees:</strong> {a.requester_explanation}</div>}
                  </td>
                  <td>
                    {a.status === "proposed" && a.requires_approval && (
                      <>
                        <button disabled={busy} onClick={() => decide(a, "approved")}>Approve</button>
                        <button disabled={busy} onClick={() => decide(a, "rejected")}>Reject</button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="dsr-actions">
            <button
              disabled={busy || !kase?.allowed_transitions.includes("executing")}
              title={!kase?.allowed_transitions.includes("executing") ? "Every action must be decided first" : undefined}
              onClick={execute}
            >
              Execute approved actions
            </button>
            <button disabled={busy} onClick={() => kase && refresh(kase.id)}>Refresh</button>
          </div>
        </section>
      )}

      {/* ── 8/9. Execution + verification + result ───────────────────────── */}
      {step === "result" && (
        <>
          <section className="card">
            <h2>Execution and verification</h2>
            <p className="small muted">
              "The write ran" and "a read-back confirmed it" are separate claims, shown
              separately.
            </p>
            <table className="data">
              <thead><tr><th>Status</th><th>Rows</th><th>Verified</th><th>Detail</th></tr></thead>
              <tbody>
                {executions.map((x) => (
                  <tr key={x.id}>
                    <td><span className={`pill pill-${x.status === "verified" ? "ok" : x.status === "failed" ? "bad" : "warn"}`}>{x.status}</span></td>
                    <td>{x.rows_affected ?? "—"}</td>
                    <td><span className={`pill pill-${x.verification_status === "passed" ? "ok" : "bad"}`}>{x.verification_status ?? "—"}</span></td>
                    <td className="mono small">
                      {x.error_code ? `${x.error_code}: ${x.error_detail ?? ""}` : JSON.stringify(x.verification_detail ?? {})}
                    </td>
                  </tr>
                ))}
                {executions.length === 0 && (
                  <tr><td colSpan={4} className="muted">Nothing was executed — every action was kept, blocked, or left for review.</td></tr>
                )}
              </tbody>
            </table>
          </section>

          <section className="card">
            <h2>Response</h2>
            {response
              ? <pre className="response-body">{response.body_text}</pre>
              : <p className="muted">No response drafted yet.</p>}
            <div className="dsr-actions">
              <button disabled={busy} onClick={generateResponse}>Generate response</button>
              <button disabled={busy} onClick={loadAudit}>View audit</button>
            </div>
            {showAudit && (
              <ol className="timeline">
                {audit.map((a) => (
                  <li key={a.id}>
                    <span className="mono small">{a.created_at?.slice(0, 19).replace("T", " ")}</span>{" "}
                    <strong>{a.action}</strong>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </>
      )}
    </div>
  );
}
