import { useCallback, useEffect, useState } from "react";
import {
  COMMUNICATION_AUDIENCES,
  EVIDENCE_KINDS,
  INCIDENT_SOURCES,
  SYSTEM_KINDS,
  incidentsApi,
  type AffectedSubjects,
  type Communication,
  type Confidence,
  type Impact,
  type Incident,
  type IncidentAction,
  type IncidentAuditEntry,
  type IncidentEvidence,
  type IncidentReport,
  type PlanSummary,
  type RiskView,
  type TimelineEntry,
} from "../api/incidents";

/**
 * Agent 4 (Breach Response) console.
 *
 * Reuses the existing shell, styles and status-pill classes rather than
 * introducing a design of its own, and follows the same rule Agent 3's UI does:
 * it never shows progress the backend did not report. Buttons come from
 * `allowed_transitions`, which the backend computes from the state machine it
 * enforces, so the UI cannot offer a move the server would refuse.
 *
 * Two things are specific to this agent and are held to strictly.
 *
 * CONFIDENCE IS NEVER DROPPED. Every substantive claim is rendered next to the
 * confidence the backend attached to it. An incident console that shows "personal
 * data involved: yes" where the backend said "probable" is the precise failure this
 * agent exists to prevent, so there is no code path here that renders one of those
 * fields without its level.
 *
 * NOTHING PRETENDS TO ACT. Consiva cannot disable an account, revoke a key or send
 * an email. Every containment control is labelled as recording what a person did,
 * and the attestation it produces is shown as somebody's word rather than as a
 * verified outcome.
 */

type Tab = "evidence" | "timeline" | "impact" | "response" | "comms" | "report" | "audit";

const STATUS_TONE: Record<string, string> = {
  closed: "ok",
  approved: "ok",
  rejected: "bad",
  failed: "bad",
  escalated: "warn",
  review_required: "warn",
  approval_required: "warn",
  communication_pending: "warn",
  cancelled: "neutral",
  partially_completed: "warn",
};

const CONFIDENCE_TONE: Record<Confidence, string> = {
  confirmed: "ok",
  probable: "info",
  possible: "warn",
  unknown: "neutral",
};

/** A claim and the confidence behind it, always together. */
function ConfidenceChip({ level, label }: { level: Confidence | null; label?: string }) {
  const value = level ?? "unknown";
  return (
    <span className={`badge ${CONFIDENCE_TONE[value] ?? "neutral"}`}>
      {label ? `${label}: ` : ""}
      {value}
    </span>
  );
}

function Tone({ status }: { status: string }) {
  return <span className={`badge ${STATUS_TONE[status] ?? "neutral"}`}>{status.replace(/_/g, " ")}</span>;
}

function when(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "—";
}

export function BreachConsole() {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [selected, setSelected] = useState<Incident | null>(null);
  const [evidence, setEvidence] = useState<IncidentEvidence[]>([]);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [impact, setImpact] = useState<Impact | null>(null);
  const [risk, setRisk] = useState<RiskView | null>(null);
  const [actions, setActions] = useState<IncidentAction[]>([]);
  const [planSummary, setPlanSummary] = useState<PlanSummary | null>(null);
  const [comms, setComms] = useState<Communication[]>([]);
  const [report, setReport] = useState<IncidentReport | null>(null);
  const [audit, setAudit] = useState<IncidentAuditEntry[]>([]);
  const [tab, setTab] = useState<Tab>("evidence");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Intake
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [source, setSource] = useState<string>("manual");

  const loadList = useCallback(async () => {
    try {
      setIncidents(await incidentsApi.list());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load incidents.");
    }
  }, []);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  const loadIncident = useCallback(async (id: string) => {
    setBusy(true);
    setError(null);
    try {
      const [fresh, ev, tl, imp, rk, plan, cm, rp, ad] = await Promise.all([
        incidentsApi.get(id),
        incidentsApi.listEvidence(id).catch(() => []),
        incidentsApi.getTimeline(id).catch(() => []),
        incidentsApi.getImpact(id).catch(() => null),
        incidentsApi.getRisk(id).catch(() => null),
        incidentsApi.getPlan(id).catch(() => null),
        incidentsApi.listCommunications(id).catch(() => []),
        incidentsApi.getReport(id).catch(() => null),
        incidentsApi.getAudit(id).catch(() => []),
      ]);
      setSelected(fresh);
      setEvidence(ev);
      setTimeline(tl);
      setImpact(imp);
      setRisk(rk);
      setActions(plan?.actions ?? []);
      setPlanSummary(plan?.summary ?? null);
      setComms(cm);
      setReport(rp);
      setAudit(ad);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the incident.");
    } finally {
      setBusy(false);
    }
  }, []);

  /** Every mutating control funnels through here, so a refusal always surfaces as
   *  the backend's own message and the incident is always re-read afterwards. */
  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(label);
      if (selected) await loadIncident(selected.id);
      await loadList();
    } catch (err) {
      setError(err instanceof Error ? err.message : `${label} failed.`);
    } finally {
      setBusy(false);
    }
  }

  async function report_incident(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim() || description.trim().length < 10) return;
    setBusy(true);
    setError(null);
    try {
      const created = await incidentsApi.create({
        title: title.trim(),
        description: description.trim(),
        source,
        detected_at: new Date().toISOString(),
      });
      setTitle("");
      setDescription("");
      await loadList();
      await loadIncident(created.id);
      setNotice(`Incident ${created.reference} opened.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open the incident.");
    } finally {
      setBusy(false);
    }
  }

  const can = (status: string) => selected?.allowed_transitions.includes(status) ?? false;

  return (
    <div className="dsr">
      <section className="section">
        <h2>Report an incident</h2>
        <p className="hint">
          Consiva assesses what happened from the evidence you provide. It does not
          monitor your systems for you, and it does not carry out containment — it
          records what your team does.
        </p>
        <form className="dsr-intake" onSubmit={report_incident}>
          <label>
            What happened
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Unauthorized access to the customer database"
            />
          </label>
          <label style={{ flex: "2 1 420px" }}>
            Describe it
            <textarea
              rows={2}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="An account with no business reason queried the customer table 400 times overnight."
            />
          </label>
          <label style={{ flex: "0 1 200px" }}>
            How was it detected
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              {INCIDENT_SOURCES.map((s) => (
                <option key={s} value={s}>
                  {s.replace(/_/g, " ")}
                </option>
              ))}
            </select>
          </label>
          <button disabled={busy || !title.trim() || description.trim().length < 10}>Open incident</button>
        </form>
      </section>

      {error && <div className="banner banner-error">{error}</div>}
      {notice && <div className="banner banner-ok">{notice}</div>}

      <div className="dsr-layout">
        <section className="section dsr-list">
          <h2>Incidents ({incidents.length})</h2>
          {incidents.length === 0 && <p className="empty-note">No incidents yet.</p>}
          <ul>
            {incidents.map((inc) => (
              <li key={inc.id}>
                <button
                  className={selected?.id === inc.id ? "active" : ""}
                  onClick={() => void loadIncident(inc.id)}
                >
                  <span className="mono">{inc.reference}</span>
                  <Tone status={inc.status} />
                  {inc.sla.breached && <span className="badge bad">overdue</span>}
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section className="section">
          {!selected && <p className="empty-note">Select an incident, or open one above.</p>}
          {selected && (
            <>
              <div className="dsr-head">
                <div>
                  <h2 style={{ marginBottom: 6 }}>
                    <span className="mono">{selected.reference}</span>
                  </h2>
                  <div style={{ fontSize: 15, marginBottom: 6 }}>{selected.title}</div>
                  <p className="muted small" style={{ maxWidth: 640 }}>{selected.description}</p>
                </div>
                <div className="dsr-badges">
                  <Tone status={selected.status} />
                  {selected.incident_type && <span className="badge info">{selected.incident_type}</span>}
                  {selected.severity && <span className={`badge ${selected.severity}`}>{selected.severity}</span>}
                </div>
              </div>

              {/* The two substantive questions, never shown without their level. */}
              <div className="stat-row" style={{ marginTop: 12, gap: 20 }}>
                <div className="stat">
                  <div className="stat-label">Personal data involved</div>
                  <div className="stat-value">
                    <ConfidenceChip level={selected.personal_data_involved} />
                  </div>
                </div>
                <div className="stat">
                  <div className="stat-label">Is this a breach</div>
                  <div className="stat-value">
                    <ConfidenceChip level={selected.breach_confirmed} />
                  </div>
                </div>
                <div className="stat">
                  <div className="stat-label">Classified</div>
                  <div className="stat-value small muted">
                    {selected.classification_method ?? "not yet"}
                    {selected.classification_confidence != null &&
                      ` · ${(selected.classification_confidence * 100).toFixed(0)}%`}
                  </div>
                </div>
              </div>

              <FindingControls
                incident={selected}
                busy={busy}
                onSet={(field, confidence, reason) =>
                  act("Finding recorded.", () =>
                    incidentsApi.setFinding(selected.id, { field, confidence, reason }),
                  )
                }
              />

              <div className="info-box small" style={{ marginTop: 12 }}>
                <strong>Response target:</strong> {when(selected.sla.due_at)}
                {selected.sla.breached && <span className="badge bad" style={{ marginLeft: 8 }}>passed</span>}
                <div className="muted" style={{ marginTop: 4 }}>{selected.sla.note}</div>
              </div>

              {selected.error_code && (
                <div className="banner banner-warn" style={{ marginTop: 12 }}>
                  <strong>{selected.error_code}</strong>
                  {selected.error_detail ? ` — ${selected.error_detail}` : ""}
                </div>
              )}

              <div className="dsr-actions" style={{ marginTop: 14 }}>
                <button
                  disabled={busy || selected.is_terminal}
                  onClick={() => void act("Analysis queued.", () => incidentsApi.analyse(selected.id))}
                >
                  Run analysis
                </button>
                <button
                  className="secondary"
                  disabled={busy || !can("response_pending")}
                  onClick={() =>
                    void act("Moved to response planning.", () =>
                      incidentsApi.transition(selected.id, "response_pending"),
                    )
                  }
                >
                  Plan response
                </button>
                <button
                  className="secondary"
                  disabled={busy || !can("review_required")}
                  onClick={() =>
                    void act("Sent for review.", () =>
                      incidentsApi.transition(selected.id, "review_required"),
                    )
                  }
                >
                  Send for review
                </button>
                <RejectControl
                  disabled={busy || !can("rejected")}
                  onReject={(reason) =>
                    act("Recorded as not an incident.", () =>
                      incidentsApi.transition(selected.id, "rejected", reason, "invalid_incident"),
                    )
                  }
                />
              </div>

              <nav className="tabs ropa-tabs" style={{ marginTop: 16 }}>
                {(["evidence", "timeline", "impact", "response", "comms", "report", "audit"] as Tab[]).map((t) => (
                  <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
                    {t === "comms" ? "communications" : t}
                  </button>
                ))}
              </nav>

              {tab === "evidence" && (
                <EvidenceTab
                  incident={selected}
                  evidence={evidence}
                  busy={busy}
                  onAdd={(payload) =>
                    act("Evidence filed.", () => incidentsApi.addEvidence(selected.id, payload))
                  }
                  onReveal={async (evidenceId) => {
                    const full = await incidentsApi.getEvidenceDetail(selected.id, evidenceId);
                    setEvidence((rows) => rows.map((r) => (r.id === evidenceId ? { ...r, ...full } : r)));
                  }}
                />
              )}

              {tab === "timeline" && <TimelineTab entries={timeline} />}

              {tab === "impact" && (
                <ImpactTab
                  impact={impact}
                  risk={risk}
                  busy={busy}
                  onAddSystem={(payload) =>
                    act("System recorded.", () => incidentsApi.addSystem(selected.id, payload))
                  }
                  onAddSubjects={(payload) =>
                    act("Affected group recorded.", () => incidentsApi.addSubjects(selected.id, payload))
                  }
                />
              )}

              {tab === "response" && (
                <ResponseTab
                  actions={actions}
                  summary={planSummary}
                  busy={busy}
                  onBuild={() => act("Plan built.", () => incidentsApi.buildPlan(selected.id))}
                  onDecide={(actionId, decision, reason) =>
                    act("Decision recorded.", () =>
                      incidentsApi.decideAction(selected.id, actionId, decision, reason),
                    )
                  }
                  onAttest={(actionId, performed_by, attestation) =>
                    act("Attestation recorded.", () =>
                      incidentsApi.attest(selected.id, actionId, { performed_by, attestation }),
                    )
                  }
                />
              )}

              {tab === "comms" && (
                <CommunicationsTab
                  comms={comms}
                  busy={busy}
                  onDraft={(audience, subject) =>
                    act("Draft created.", () =>
                      incidentsApi.draftCommunication(selected.id, { audience, subject }),
                    )
                  }
                  onDecide={(id, decision, reason) =>
                    act("Draft decided.", () =>
                      incidentsApi.decideCommunication(selected.id, id, decision, reason),
                    )
                  }
                  onSent={(id) => act("Recorded as sent.", () => incidentsApi.markSent(selected.id, id))}
                />
              )}

              {tab === "report" && (
                <ReportTab
                  report={report}
                  incident={selected}
                  busy={busy}
                  onGenerate={() => act("Report generated.", () => incidentsApi.generateReport(selected.id))}
                  onClose={(summary) => act("Incident closed.", () => incidentsApi.close(selected.id, summary))}
                />
              )}

              {tab === "audit" && <AuditTab entries={audit} />}
            </>
          )}
        </section>
      </div>
    </div>
  );
}

// ── Findings ──────────────────────────────────────────────────────────────────────

/**
 * The only route to "confirmed", and it is deliberately not one click.
 *
 * Saying personal data was definitely involved, or that this definitely is a breach,
 * is a statement an organisation makes with a name against it. The backend refuses a
 * `confirmed` finding without a reason; this form refuses to submit one too, so the
 * refusal is explained here rather than arriving as a 409.
 */
function FindingControls({
  incident,
  busy,
  onSet,
}: {
  incident: Incident;
  busy: boolean;
  onSet: (field: "personal_data_involved" | "breach_confirmed", confidence: Confidence, reason: string) => void;
}) {
  const [field, setField] = useState<"personal_data_involved" | "breach_confirmed">("breach_confirmed");
  const [confidence, setConfidence] = useState<Confidence>("probable");
  const [reason, setReason] = useState("");
  const needsReason = confidence === "confirmed";

  if (incident.is_terminal) return null;

  return (
    <div className="info-box" style={{ marginTop: 12 }}>
      <div className="small muted" style={{ marginBottom: 6 }}>
        Record a finding. <strong>Confirmed</strong> is a position your organisation
        takes — it needs a reason, and it is recorded against your name.
      </div>
      <div className="dsr-intake">
        <label style={{ flex: "0 1 220px" }}>
          Question
          <select value={field} onChange={(e) => setField(e.target.value as typeof field)}>
            <option value="breach_confirmed">Is this a breach</option>
            <option value="personal_data_involved">Was personal data involved</option>
          </select>
        </label>
        <label style={{ flex: "0 1 160px" }}>
          How sure
          <select value={confidence} onChange={(e) => setConfidence(e.target.value as Confidence)}>
            <option value="confirmed">confirmed</option>
            <option value="probable">probable</option>
            <option value="possible">possible</option>
            <option value="unknown">unknown</option>
          </select>
        </label>
        <label style={{ flex: "2 1 320px" }}>
          {needsReason ? "Reason (required)" : "Reason"}
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Egress logs show 12,000 rows leaving to an external host at 02:14."
          />
        </label>
        <button
          className="secondary"
          disabled={busy || (needsReason && !reason.trim())}
          onClick={() => {
            onSet(field, confidence, reason.trim());
            setReason("");
          }}
        >
          Record finding
        </button>
      </div>
    </div>
  );
}

function RejectControl({ disabled, onReject }: { disabled: boolean; onReject: (reason: string) => void }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");

  if (!open) {
    return (
      <button className="secondary" disabled={disabled} onClick={() => setOpen(true)}>
        Not an incident
      </button>
    );
  }
  return (
    <span className="dsr-intake" style={{ flex: "1 1 100%" }}>
      <label style={{ flex: "2 1 320px" }}>
        Why is this not an incident
        <input
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Traced to the nightly backup job; no external access."
        />
      </label>
      <button
        disabled={disabled || !reason.trim()}
        onClick={() => {
          onReject(reason.trim());
          setOpen(false);
          setReason("");
        }}
      >
        Record
      </button>
      <button className="secondary" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </span>
  );
}

// ── Evidence ──────────────────────────────────────────────────────────────────────

function EvidenceTab({
  evidence,
  busy,
  onAdd,
  onReveal,
}: {
  incident: Incident;
  evidence: IncidentEvidence[];
  busy: boolean;
  onAdd: (payload: { kind: string; source_system: string; summary: string }) => void;
  onReveal: (evidenceId: string) => Promise<void>;
}) {
  const [kind, setKind] = useState<string>("access_log");
  const [system, setSystem] = useState("");
  const [summary, setSummary] = useState("");

  return (
    <div style={{ marginTop: 14 }}>
      <div className="dsr-intake">
        <label style={{ flex: "0 1 200px" }}>
          Kind
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {EVIDENCE_KINDS.map((k) => (
              <option key={k} value={k}>
                {k.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <label style={{ flex: "0 1 200px" }}>
          Where it came from
          <input value={system} onChange={(e) => setSystem(e.target.value)} placeholder="siem" />
        </label>
        <label style={{ flex: "2 1 320px" }}>
          What it shows
          <input
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            placeholder="400 SELECTs against customers from 203.0.113.9"
          />
        </label>
        <button
          className="secondary"
          disabled={busy || !system.trim() || !summary.trim()}
          onClick={() => {
            onAdd({ kind, source_system: system.trim(), summary: summary.trim() });
            setSystem("");
            setSummary("");
          }}
        >
          File evidence
        </button>
      </div>

      <p className="hint">
        Evidence is append-only. Anything that looks like a password or token is
        stripped before it is stored, and it cannot be edited or removed afterwards —
        an investigation whose record can be rewritten is not a record.
      </p>

      {evidence.length === 0 && <p className="empty-note">No evidence filed yet.</p>}
      {evidence.length > 0 && (
        <table className="ropa-table">
          <thead>
            <tr>
              <th>Kind</th>
              <th>Source</th>
              <th>Summary</th>
              <th className="nowrap">Observed</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {evidence.map((e) => (
              <tr key={e.id}>
                <td className="nowrap">
                  {e.kind.replace(/_/g, " ")}
                  {e.is_derived && <span className="badge info" style={{ marginLeft: 6 }}>derived</span>}
                </td>
                <td className="nowrap mono">{e.source_system}</td>
                <td>
                  {e.summary}
                  {e.contains_secrets && (
                    <div className="small muted">Secret-shaped fields were removed before storage.</div>
                  )}
                </td>
                <td className="nowrap small muted">{when(e.observed_at)}</td>
                <td>
                  {/* Detail is never handed out in a listing: a list view is the one
                      most likely to be left open on a shared screen. */}
                  {e.detail ? (
                    <pre className="key-value">{JSON.stringify(e.detail, null, 2)}</pre>
                  ) : e.has_detail ? (
                    <button className="secondary small" onClick={() => void onReveal(e.id)}>
                      Show detail
                    </button>
                  ) : (
                    <span className="muted small">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function TimelineTab({ entries }: { entries: TimelineEntry[] }) {
  return (
    <div style={{ marginTop: 14 }}>
      <p className="hint">
        What happened in the world, as far as the evidence shows. Distinct from the
        audit trail, which is what happened in Consiva.
      </p>
      {entries.length === 0 && <p className="empty-note">Nothing on the timeline yet.</p>}
      {entries.length > 0 && (
        <table className="ropa-table">
          <thead>
            <tr>
              <th className="nowrap">When</th>
              <th>Event</th>
              <th>Actor</th>
              <th>Source</th>
              <th>How sure</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((t) => (
              <tr key={t.id}>
                <td className="nowrap small mono">{when(t.occurred_at)}</td>
                <td>{t.event}</td>
                <td className="small">{t.actor ?? "—"}</td>
                <td className="small mono">{t.source_system ?? "—"}</td>
                <td>
                  <ConfidenceChip level={t.confidence} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── Impact and risk ───────────────────────────────────────────────────────────────

function ImpactTab({
  impact,
  risk,
  busy,
  onAddSystem,
  onAddSubjects,
}: {
  impact: Impact | null;
  risk: RiskView | null;
  busy: boolean;
  onAddSystem: (p: { system_name: string; system_kind: string }) => void;
  onAddSubjects: (p: AffectedSubjects & { subject_group: string }) => void;
}) {
  const [systemName, setSystemName] = useState("");
  const [systemKind, setSystemKind] = useState<string>("database");
  const [group, setGroup] = useState("");
  const [count, setCount] = useState("");
  const [basis, setBasis] = useState<"counted" | "estimated" | "unknown">("estimated");

  return (
    <div style={{ marginTop: 14 }}>
      <div className="dsr-intake">
        <label style={{ flex: "1 1 220px" }}>
          Affected system
          <input value={systemName} onChange={(e) => setSystemName(e.target.value)} placeholder="crm-postgres" />
        </label>
        <label style={{ flex: "0 1 170px" }}>
          Kind
          <select value={systemKind} onChange={(e) => setSystemKind(e.target.value)}>
            {SYSTEM_KINDS.map((k) => (
              <option key={k} value={k}>
                {k.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <button
          className="secondary"
          disabled={busy || !systemName.trim()}
          onClick={() => {
            onAddSystem({ system_name: systemName.trim(), system_kind: systemKind });
            setSystemName("");
          }}
        >
          Add system
        </button>
      </div>

      {impact && impact.systems.length > 0 && (
        <table className="ropa-table" style={{ marginTop: 10 }}>
          <thead>
            <tr>
              <th>System</th>
              <th>Kind</th>
              <th>How sure</th>
              <th>Known to Consiva</th>
            </tr>
          </thead>
          <tbody>
            {impact.systems.map((s) => (
              <tr key={s.id}>
                <td className="mono">{s.system_name}</td>
                <td className="small">{s.system_kind.replace(/_/g, " ")}</td>
                <td>
                  <ConfidenceChip level={s.confidence} />
                </td>
                <td className="small">
                  {s.known_to_consiva ? (
                    <span className="badge ok">discovered</span>
                  ) : (
                    <span className="badge warn" title="Its contents are unknown to Consiva">
                      never connected
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 style={{ marginTop: 18 }}>Data categories</h2>
      <p className="hint">
        Taken from the ROPA agent's map of what each system contains. That is not the
        same as what was touched, which is why these sit at <em>possible</em> until
        someone establishes otherwise.
      </p>
      {(!impact || impact.data_categories.length === 0) && (
        <p className="empty-note">No categories established. Run analysis once a system is recorded.</p>
      )}
      {impact && impact.data_categories.length > 0 && (
        <table className="ropa-table">
          <thead>
            <tr>
              <th>Category</th>
              <th>How sure</th>
              <th>Columns</th>
            </tr>
          </thead>
          <tbody>
            {impact.data_categories.map((c) => (
              <tr key={c.category}>
                <td>
                  <span className="pill pill-pd">{c.category}</span>
                </td>
                <td>
                  <ConfidenceChip level={c.confidence} />
                </td>
                <td className="mono small">{c.columns.join(", ") || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 style={{ marginTop: 18 }}>People affected</h2>
      <div className="dsr-intake">
        <label style={{ flex: "1 1 200px" }}>
          Group
          <input value={group} onChange={(e) => setGroup(e.target.value)} placeholder="customers" />
        </label>
        <label style={{ flex: "0 1 140px" }}>
          How many
          <input value={count} onChange={(e) => setCount(e.target.value)} placeholder="400" inputMode="numeric" />
        </label>
        <label style={{ flex: "0 1 170px" }}>
          Where the number came from
          <select value={basis} onChange={(e) => setBasis(e.target.value as typeof basis)}>
            <option value="counted">counted</option>
            <option value="estimated">estimated</option>
            <option value="unknown">unknown</option>
          </select>
        </label>
        <button
          className="secondary"
          disabled={busy || !group.trim()}
          onClick={() => {
            const parsed = basis === "unknown" || !count.trim() ? null : Number(count);
            onAddSubjects({
              subject_group: group.trim(),
              record_count: Number.isFinite(parsed as number) ? (parsed as number) : null,
              count_basis: basis,
              basis_note: null,
              confidence: basis === "counted" ? "probable" : "possible",
              group: group.trim(),
            });
            setGroup("");
            setCount("");
          }}
        >
          Record group
        </button>
      </div>

      {impact && impact.subjects.length > 0 && (
        <>
          <table className="ropa-table" style={{ marginTop: 10 }}>
            <thead>
              <tr>
                <th>Group</th>
                <th>Records</th>
                <th>Basis</th>
                <th>How sure</th>
              </tr>
            </thead>
            <tbody>
              {impact.subjects.map((s) => (
                <tr key={s.group}>
                  <td>{s.group}</td>
                  <td className="mono">{s.record_count ?? "not established"}</td>
                  <td className="small">{s.count_basis}</td>
                  <td>
                    <ConfidenceChip level={s.confidence} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* The total never appears without its basis. One unknown group makes the
              whole total unknown rather than merely approximate. */}
          <div className="info-box" style={{ marginTop: 10 }}>
            <strong>Total:</strong>{" "}
            {impact.total.record_count == null
              ? "not established — at least one group's count is unknown, so no total is given"
              : `${impact.total.record_count.toLocaleString()} (${impact.total.count_basis})`}
          </div>
        </>
      )}

      <h2 style={{ marginTop: 18 }}>Risk</h2>
      {!risk?.current && <p className="empty-note">No risk assessment yet. Run analysis.</p>}
      {risk?.current && (
        <div>
          <div className="dsr-badges" style={{ justifyContent: "flex-start", marginBottom: 8 }}>
            <span className={`badge ${risk.current.risk_level}`}>{risk.current.risk_level}</span>
            <span className="badge neutral">score {risk.current.risk_score}</span>
            <ConfidenceChip level={risk.current.confidence} label="held with" />
            <span className="badge info">v{risk.current.version}</span>
          </div>
          <p className="small">{risk.current.reason}</p>
          {risk.current.factors.length > 0 && (
            <table className="ropa-table">
              <thead>
                <tr>
                  <th>Factor</th>
                  <th>Contribution</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {risk.current.factors.map((f) => (
                  <tr key={f.code}>
                    <td className="small">
                      {f.label}
                      {!f.evidenced && (
                        <span className="badge warn" style={{ marginLeft: 6 }} title="Rests on an assumption">
                          assumed
                        </span>
                      )}
                    </td>
                    <td className="mono">{f.contribution}</td>
                    <td className="small muted">{f.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {risk.current.regulatory_context.length > 0 && (
            <div className="info-box" style={{ marginTop: 12 }}>
              <div className="small muted" style={{ marginBottom: 6 }}>
                Material to read before deciding. This is <strong>not</strong> a
                determination that any obligation applies to this incident.
              </div>
              {risk.current.regulatory_context.map((c, i) => (
                <div key={i} className="small" style={{ marginBottom: 8 }}>
                  <strong>{String(c.source ?? "source")}</strong>{" "}
                  <span className="muted">v{String(c.version ?? "?")}</span>
                  <div className="muted">{String(c.excerpt ?? "")}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Response ──────────────────────────────────────────────────────────────────────

function ResponseTab({
  actions,
  summary,
  busy,
  onBuild,
  onDecide,
  onAttest,
}: {
  actions: IncidentAction[];
  summary: PlanSummary | null;
  busy: boolean;
  onBuild: () => void;
  onDecide: (actionId: string, decision: string, reason?: string) => void;
  onAttest: (actionId: string, performedBy: string, attestation: string) => void;
}) {
  return (
    <div style={{ marginTop: 14 }}>
      <div className="dsr-actions">
        <button className="secondary" disabled={busy} onClick={onBuild}>
          Build plan
        </button>
        {summary && (
          <span className="small muted">
            {summary.approved} approved · {summary.awaiting_decision} awaiting a decision ·{" "}
            {summary.completed} recorded as done
          </span>
        )}
      </div>

      <p className="hint">
        Every action here is performed by a person outside Consiva. Consiva records
        what was done and who did it; it does not disable accounts, revoke keys or
        isolate services itself.
      </p>

      {actions.length === 0 && <p className="empty-note">No plan yet.</p>}
      {actions.map((a) => (
        <ActionCard key={a.id} action={a} busy={busy} onDecide={onDecide} onAttest={onAttest} />
      ))}
    </div>
  );
}

function ActionCard({
  action,
  busy,
  onDecide,
  onAttest,
}: {
  action: IncidentAction;
  busy: boolean;
  onDecide: (actionId: string, decision: string, reason?: string) => void;
  onAttest: (actionId: string, performedBy: string, attestation: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [who, setWho] = useState("");
  const [what, setWhat] = useState("");
  const highRisk = action.risk === "high";

  return (
    <div className="finding-card" style={{ marginBottom: 12 }}>
      <div className="finding-head">
        <div>
          <strong>{action.title}</strong>
          <div className="small muted">{action.rationale}</div>
        </div>
        <div className="dsr-badges">
          <span className={`badge ${action.risk}`}>{action.risk} risk</span>
          <Tone status={action.status} />
          <span className="badge neutral" title="A person performs this, not Consiva">
            {action.execution_mode}
          </span>
        </div>
      </div>

      <div className="small muted" style={{ marginTop: 6 }}>
        Expected result: {action.expected_result}
        {action.target && (
          <>
            {" · "}Target: <span className="mono">{action.target}</span>
          </>
        )}
      </div>

      {action.blocked_reason && <div className="banner banner-warn small">{action.blocked_reason}</div>}

      {action.status === "proposed" && action.requires_approval && (
        <div className="dsr-intake" style={{ marginTop: 8 }}>
          <label style={{ flex: "2 1 320px" }}>
            {highRisk ? "Reason (required for a high-risk action)" : "Reason"}
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <button
            disabled={busy || (highRisk && !reason.trim())}
            onClick={() => onDecide(action.id, "approved", reason.trim() || undefined)}
          >
            Approve
          </button>
          <button
            className="secondary"
            disabled={busy || !reason.trim()}
            onClick={() => onDecide(action.id, "rejected", reason.trim())}
          >
            Reject
          </button>
        </div>
      )}

      {(action.status === "approved" || (action.status === "proposed" && !action.requires_approval)) && (
        <div className="dsr-intake" style={{ marginTop: 8 }}>
          <label style={{ flex: "0 1 200px" }}>
            Who performed it
            <input value={who} onChange={(e) => setWho(e.target.value)} placeholder="priya@corp" />
          </label>
          <label style={{ flex: "2 1 320px" }}>
            What they actually did
            <input
              value={what}
              onChange={(e) => setWhat(e.target.value)}
              placeholder="Revoked the account's database role at 09:40."
            />
          </label>
          <button
            disabled={busy || !who.trim() || !what.trim()}
            onClick={() => {
              onAttest(action.id, who.trim(), what.trim());
              setWho("");
              setWhat("");
            }}
          >
            Record as done
          </button>
        </div>
      )}

      {action.executions.map((e) => (
        <div key={e.id} className="info-box small" style={{ marginTop: 8 }}>
          <strong>{e.performed_by ?? "someone"}</strong>: {e.attestation}
          <div className="muted" style={{ marginTop: 4 }}>
            Recorded as <span className="badge warn">{e.verification_status}</span> — Consiva did not
            observe this action and cannot confirm its effect.
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Communications ────────────────────────────────────────────────────────────────

function CommunicationsTab({
  comms,
  busy,
  onDraft,
  onDecide,
  onSent,
}: {
  comms: Communication[];
  busy: boolean;
  onDraft: (audience: string, subject: string) => void;
  onDecide: (id: string, decision: string, reason?: string) => void;
  onSent: (id: string) => void;
}) {
  const [audience, setAudience] = useState<string>("internal");
  const [subject, setSubject] = useState("");

  return (
    <div style={{ marginTop: 14 }}>
      <div className="dsr-intake">
        <label style={{ flex: "0 1 200px" }}>
          Audience
          <select value={audience} onChange={(e) => setAudience(e.target.value)}>
            {COMMUNICATION_AUDIENCES.map((a) => (
              <option key={a} value={a}>
                {a.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <label style={{ flex: "2 1 320px" }}>
          Subject
          <input value={subject} onChange={(e) => setSubject(e.target.value)} />
        </label>
        <button
          className="secondary"
          disabled={busy || subject.trim().length < 3}
          onClick={() => {
            onDraft(audience, subject.trim());
            setSubject("");
          }}
        >
          Draft
        </button>
      </div>

      <p className="hint">
        Consiva has no outbound provider and sends nothing. A draft to anyone outside
        the organisation needs a recorded approval before it can even be marked as
        sent, and marking it sent records that a person sent it.
      </p>

      {comms.length === 0 && <p className="empty-note">No communications drafted.</p>}
      {comms.map((c) => (
        <div key={c.id} className="finding-card" style={{ marginBottom: 12 }}>
          <div className="finding-head">
            <div>
              <strong>{c.subject}</strong>
              <div className="small muted">
                {c.audience.replace(/_/g, " ")}
                {c.external && <span className="badge warn" style={{ marginLeft: 6 }}>external</span>}
              </div>
            </div>
            <Tone status={c.status} />
          </div>
          {c.body && <pre className="response-body">{c.body}</pre>}
          <div className="dsr-actions">
            {c.status === "review_required" && (
              <>
                <button disabled={busy} onClick={() => onDecide(c.id, "approved")}>
                  Approve
                </button>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => onDecide(c.id, "rejected", "Not appropriate to send.")}
                >
                  Reject
                </button>
              </>
            )}
            {(c.status === "approved" || c.status === "draft") && !c.sent_at && (
              <button className="secondary" disabled={busy} onClick={() => onSent(c.id)}>
                We sent this
              </button>
            )}
            {c.sent_at && <span className="small muted">Recorded as sent {when(c.sent_at)}.</span>}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Report and closure ────────────────────────────────────────────────────────────

function ReportTab({
  report,
  incident,
  busy,
  onGenerate,
  onClose,
}: {
  report: IncidentReport | null;
  incident: Incident;
  busy: boolean;
  onGenerate: () => void;
  onClose: (summary: string) => void;
}) {
  const [summary, setSummary] = useState("");

  return (
    <div style={{ marginTop: 14 }}>
      <div className="dsr-actions">
        <button className="secondary" disabled={busy} onClick={onGenerate}>
          {report ? "Regenerate report" : "Generate report"}
        </button>
        {report && (
          <span className="small muted">
            v{report.version} · {report.grounded_facts.length} grounded facts ·{" "}
            {report.drafted_by_model ? `drafted by ${report.drafted_by_model}` : "assembled from recorded facts, no model"}
          </span>
        )}
      </div>

      {!report && <p className="empty-note">No report yet.</p>}
      {report && <pre className="response-body">{report.body_text}</pre>}

      {!incident.is_terminal && (
        <div className="dsr-intake" style={{ marginTop: 14 }}>
          <label style={{ flex: "2 1 420px" }}>
            Closure summary
            <input
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              placeholder="Access revoked; 400 customer records read; customers told."
            />
          </label>
          <button
            disabled={busy || !summary.trim() || !report || !incident.allowed_transitions.includes("closed")}
            title={
              !report
                ? "An incident cannot be closed without a report."
                : !incident.allowed_transitions.includes("closed")
                  ? "An incident is closed from closure review, so somebody has looked at the whole thing first."
                  : undefined
            }
            onClick={() => {
              onClose(summary.trim());
              setSummary("");
            }}
          >
            Close incident
          </button>
        </div>
      )}
    </div>
  );
}

function AuditTab({ entries }: { entries: IncidentAuditEntry[] }) {
  return (
    <div style={{ marginTop: 14 }}>
      <p className="hint">
        What happened in Consiva — append-only, and the same audit table the other
        three agents write to.
      </p>
      {entries.length === 0 && <p className="empty-note">Nothing recorded yet.</p>}
      {entries.length > 0 && (
        <table className="ropa-table">
          <thead>
            <tr>
              <th className="nowrap">When</th>
              <th>Action</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.id}>
                <td className="nowrap small mono">{when(e.created_at)}</td>
                <td className="small">{e.action}</td>
                <td>
                  {e.after && <pre className="key-value">{JSON.stringify(e.after, null, 2)}</pre>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
