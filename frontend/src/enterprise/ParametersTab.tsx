import { useMemo, useState } from "react";
import { enterpriseScans, enterpriseErrorMessage, type ParameterHistoryItem, type ParameterState, type ParameterView } from "../api/enterprise";
import { asList, dateOnly, dateTime, points, paramState, text, words } from "./format";
import { useLoaded } from "./hooks";
import { stripRef } from "./ListTabs";
import { Empty, ErrorNote, Pill, Skeleton } from "./ui";

const REVIEW_STATES: ParameterState[] = ["met", "partly_met", "not_met", "not_assessed", "not_applicable"];

function History({ scanId, pid, onClose }: { scanId: string; pid: string; onClose: () => void }) {
  const h = useLoaded<ParameterHistoryItem[]>(async () => asList(await enterpriseScans.parameterHistory(scanId, pid)), `${scanId}|${pid}`);
  return (
    <aside className="finding-card" aria-label={`History of ${pid}`} data-testid="history-drawer">
      <div className="finding-head"><strong>History of {pid}</strong> <button type="button" className="secondary small" onClick={onClose}>Close</button></div>
      {h.error ? <ErrorNote message={h.error} onRetry={h.reload} /> : !h.data ? <Skeleton rows={2} /> : h.data.length === 0 ? <Empty>No assessment has been recorded yet.</Empty> : (
        <ol style={{ paddingLeft: 18 }}>
          {h.data.map((it, i) => {
            const st = paramState(String(it.state));
            return (
              <li key={i} style={{ marginBottom: 8 }}>
                <Pill tone={st.tone} dashed={!st.assessed}>{st.label}</Pill> <span className="muted small">{dateTime(it.assessed_at)}</span>
                <div className="small">By {text(it.reviewed_by ?? it.assessor)}{it.model ? `, model ${it.model}` : ""}</div>
                {it.review_reason && <div className="small">Reason: {it.review_reason}</div>}
                {it.note && <div className="muted small">{it.note}</div>}
              </li>
            );
          })}
        </ol>
      )}
    </aside>
  );
}

function ReviewForm({ scanId, p, onDone }: { scanId: string; p: ParameterView; onDone: () => void }) {
  const [state, setState] = useState<ParameterState>(REVIEW_STATES.includes(p.state) ? p.state : "not_assessed");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await enterpriseScans.review(scanId, p.id, state, reason.trim());
      onDone();
    } catch (x) {
      setErr(enterpriseErrorMessage(x));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} data-testid={`review-form-${p.id}`} style={{ marginTop: 8 }}>
      <div className="inline-row">
        <label htmlFor={`rs-${p.id}`}>New state</label>
        <select id={`rs-${p.id}`} value={state} onChange={(e) => setState(e.target.value as ParameterState)}>
          {REVIEW_STATES.map((s) => <option key={s} value={s}>{paramState(s).label}</option>)}
        </select>
      </div>
      <div className="field">
        <label htmlFor={`rr-${p.id}`}>Reason (kept in the audit trail)</label>
        <textarea id={`rr-${p.id}`} rows={2} value={reason} onChange={(e) => setReason(e.target.value)} />
      </div>
      {err && <ErrorNote message={err} />}
      <button type="submit" className="small" disabled={busy || reason.trim().length === 0}>{busy ? "Saving…" : "Save review"}</button>
    </form>
  );
}

function ParameterCard({ scanId, p, canReview, onChanged, onEvidence }: { scanId: string; p: ParameterView; canReview: boolean; onChanged: () => void; onEvidence: (id: string) => void }) {
  const st = paramState(p.state);
  const [reviewing, setReviewing] = useState(false);
  const [history, setHistory] = useState(false);
  return (
    <article className="finding-card" data-testid={`param-${p.id}`} data-state={p.state}>
      <div className="finding-head">
        <span className="mono">{p.id}</span> <strong>{p.name}</strong>{" "}
        <Pill tone={st.tone} dashed={!st.assessed} testId={`param-state-${p.id}`}>{st.label}</Pill>
        {!p.in_force_on_as_of && p.in_force && <Pill tone="info">Not yet in force</Pill>}
      </div>
      {!st.assessed && p.state !== "not_applicable" && <p className="muted small">No result: this parameter has not been judged met or not met. It is not counted as a pass or a fail.</p>}
      <p className="small">
        Points earned {points(p.earned)} of {points(p.in_play)} in play ({points(p.points)} available).
        {p.legal_refs.length > 0 && <> Legal references: {p.legal_refs.join(", ")}.</>}
        {p.in_force && <> In force from {dateOnly(p.in_force)}.</>}
      </p>
      {p.note && <p className="small">{p.note}</p>}
      <p className="muted small">
        Assessed by {text(p.reviewed_by ?? p.assessor, "no one yet")}{p.model ? ` (model ${p.model})` : ""}
        {p.confidence !== null && p.confidence !== undefined ? `, confidence ${String(p.confidence)}` : ""}
        {p.assessed_at ? `, ${dateTime(p.assessed_at)}` : ""}.
        {p.review_reason ? ` Review reason: ${p.review_reason}` : ""}
      </p>
      {p.evidence_refs.length > 0 && (
        <p className="small">Evidence:{" "}
          {p.evidence_refs.map((r) => <button key={r} type="button" className="secondary small" style={{ marginRight: 4 }} onClick={() => onEvidence(stripRef(r))}>{r.length > 14 ? `${r.slice(0, 14)}…` : r}</button>)}
        </p>
      )}
      <div className="inline-row">
        {canReview && <button type="button" className="secondary small" aria-expanded={reviewing} onClick={() => setReviewing((v) => !v)}>Review</button>}
        <button type="button" className="secondary small" aria-expanded={history} onClick={() => setHistory((v) => !v)}>History</button>
      </div>
      {reviewing && canReview && <ReviewForm scanId={scanId} p={p} onDone={() => { setReviewing(false); onChanged(); }} />}
      {history && <History scanId={scanId} pid={p.id} onClose={() => setHistory(false)} />}
    </article>
  );
}

export function ParametersTab({ scanId, refresh, canReview, onEvidence, onChanged, onlyGroup, heading }: {
  scanId: string; refresh: string; canReview: boolean; onEvidence: (id: string) => void; onChanged: () => void; onlyGroup?: string; heading?: string;
}) {
  const r = useLoaded<ParameterView[]>(async () => asList(await enterpriseScans.parameters(scanId)), scanId, refresh);
  const groups = useMemo(() => {
    const m = new Map<string, { label: string; items: ParameterView[] }>();
    for (const p of r.data ?? []) {
      if (onlyGroup && p.group !== onlyGroup) continue;
      const g = m.get(p.group) ?? { label: text(p.group_label, words(p.group)), items: [] };
      g.items.push(p);
      m.set(p.group, g);
    }
    return [...m.entries()];
  }, [r.data, onlyGroup]);
  if (r.error && !r.data) return <ErrorNote message={r.error} onRetry={r.reload} />;
  if (!r.data) return <Skeleton rows={6} />;
  if (groups.length === 0) return <Empty>No parameters have been recorded for this scan yet.</Empty>;
  return (
    <div data-testid="parameters-tab">
      {heading && <p className="muted">{heading}</p>}
      {groups.map(([k, g]) => (
        <section key={k} aria-label={g.label} style={{ marginBottom: 14 }}>
          <h4 style={{ margin: "8px 0" }}>{g.label} <span className="muted small">({g.items.length})</span></h4>
          {g.items.map((p) => <ParameterCard key={p.id} scanId={scanId} p={p} canReview={canReview} onChanged={() => { r.reload(); onChanged(); }} onEvidence={onEvidence} />)}
        </section>
      ))}
    </div>
  );
}
