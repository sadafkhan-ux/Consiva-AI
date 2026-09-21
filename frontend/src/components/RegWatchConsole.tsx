import { useCallback, useEffect, useState } from "react";
import {
  regwatchApi,
  type Confidence,
  type WatchFinding,
  type WatchFindingDetail,
  type WatchSource,
  type WatchSummary,
} from "../api/regwatch";

/**
 * Agent 5 (Regulatory Watch) console.
 *
 * Reuses the existing shell, styles and pill classes rather than introducing a
 * design of its own, exactly as Agents 3 and 4 do.
 *
 * THE ONE THING THIS SCREEN MUST NEVER DO
 * ---------------------------------------
 * Look calm when it is not watching. A compliance team that sees a quiet dashboard
 * concludes it is being told about regulatory change; if two regulators have been
 * unreachable all week, that conclusion is false and expensive. So the count of
 * sources NOT currently watched is rendered FIRST, above the findings, and a source
 * row cannot be drawn without its health block -- the API type makes `health`
 * required for precisely this reason.
 *
 * CONFIDENCE IS NEVER DROPPED. Relevance, priority and every impact link are shown
 * next to the confidence the backend attached. "This affects you" where the backend
 * said "possible" is the failure this agent exists to prevent.
 *
 * NOTHING PRETENDS TO ACT. Consiva does not amend a policy or update a notice. Every
 * action control records what a person did, and says so.
 */

type Tab = "findings" | "sources";

const STATUS_TONE: Record<string, string> = {
  closed: "ok",
  approved: "ok",
  dismissed: "neutral",
  superseded: "neutral",
  failed: "bad",
  review_required: "warn",
  action_open: "warn",
  detected: "info",
  assessing: "info",
};

const HEALTH_TONE: Record<string, string> = {
  current: "ok",
  stale: "warn",
  failing: "bad",
  never_collected: "warn",
};

const RELEVANCE_TONE: Record<string, string> = {
  relevant: "warn",
  not_relevant: "neutral",
  undetermined: "info",
};

const PRIORITY_TONE: Record<string, string> = {
  critical: "bad",
  high: "bad",
  medium: "warn",
  low: "neutral",
};

/** Confidence is rendered as words, never as a number. A percentage invites a
 *  precision none of these judgements have. */
function ConfidenceTag({ level }: { level: Confidence }) {
  const tone = level === "confirmed" ? "ok" : level === "probable" ? "warn" : "info";
  return <span className={`badge ${tone}`}>{level}</span>;
}

export function RegWatchConsole() {
  const [tab, setTab] = useState<Tab>("findings");
  const [summary, setSummary] = useState<WatchSummary | null>(null);
  const [sources, setSources] = useState<WatchSource[]>([]);
  const [findings, setFindings] = useState<WatchFinding[]>([]);
  const [selected, setSelected] = useState<WatchFindingDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, srcs, f] = await Promise.all([
        regwatchApi.summary(),
        regwatchApi.listSources(),
        regwatchApi.listFindings(),
      ]);
      setSummary(s);
      setSources(srcs.sources);
      setFindings(f.findings);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function openFinding(id: string) {
    try {
      setSelected(await regwatchApi.getFinding(id));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function act(fn: () => Promise<unknown>, done: string) {
    setBusy(true);
    setNotice(null);
    try {
      await fn();
      setNotice(done);
      await refresh();
      if (selected) await openFinding(selected.id);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dsr">
      {error && <div className="banner banner-error">{error}</div>}
      {notice && <div className="banner banner-ok">{notice}</div>}

      {/* Coverage first, deliberately. See the header comment. */}
      {summary && <CoverageBanner summary={summary} />}

      <nav className="agent-switch">
        <button className={tab === "findings" ? "active" : ""} onClick={() => setTab("findings")}>
          Findings {summary ? `(${summary.awaiting_review} awaiting review)` : ""}
        </button>
        <button className={tab === "sources" ? "active" : ""} onClick={() => setTab("sources")}>
          Sources {summary ? `(${summary.sources_enabled}/${summary.sources})` : ""}
        </button>
      </nav>

      {tab === "sources" && (
        <SourcesTab
          sources={sources}
          busy={busy}
          onCollect={(id) =>
            act(
              () => regwatchApi.collectNow(id),
              "Collection queued. The source will be fetched by the worker; this screen will "
                + "show the result once it lands.",
            )
          }
          onRefresh={refresh}
          onRegistered={refresh}
          onError={setError}
        />
      )}

      {tab === "findings" && (
        <div className="dsr-layout">
          <section className="section dsr-list">
            <h3>Regulatory findings</h3>
            {findings.length === 0 && (
              <p className="empty-note">
                No findings recorded. That means nothing has been detected as changed on the
                sources listed above &mdash; it does not mean the sources listed as not
                currently watched are unchanged.
              </p>
            )}
            {findings.map((f) => (
              <button
                key={f.id}
                className={`finding-card${selected?.id === f.id ? " active" : ""}`}
                onClick={() => void openFinding(f.id)}
              >
                <div className="finding-head">
                  <span className="mono">{f.reference}</span>
                  <span className={`badge ${STATUS_TONE[f.status] ?? "neutral"}`}>{f.status}</span>
                </div>
                <div className="dsr-badges">
                  <span className={`badge ${RELEVANCE_TONE[f.relevance] ?? "info"}`}>
                    {f.relevance}
                  </span>
                  <ConfidenceTag level={f.relevance_confidence} />
                  {f.priority && (
                    <span className={`badge ${PRIORITY_TONE[f.priority] ?? "neutral"}`}>
                      {f.priority}
                    </span>
                  )}
                </div>
                <div className="small muted">{(f.summary ?? "").slice(0, 160)}</div>
              </button>
            ))}
          </section>

          {selected && (
            <FindingDetail
              finding={selected}
              busy={busy}
              onDecide={(decision, reason) =>
                act(
                  () => regwatchApi.decide(selected.id, decision, reason),
                  `Decision recorded: ${decision}.`,
                )
              }
              onOpenActions={(actions) =>
                act(() => regwatchApi.openActions(selected.id, actions), "Actions raised.")
              }
              onCompleteAction={(actionId, who, note) =>
                act(
                  () => regwatchApi.completeAction(actionId, who, note),
                  "Attestation recorded. Consiva did not perform this work and has not verified it.",
                )
              }
              onClose={(reason) =>
                act(() => regwatchApi.closeFinding(selected.id, reason), "Finding closed.")
              }
            />
          )}
        </div>
      )}
    </div>
  );
}

/**
 * The first thing on the screen. A dashboard that led with "3 findings awaiting
 * review" would invite the reading that everything else is fine.
 */
function CoverageBanner({ summary }: { summary: WatchSummary }) {
  const gap = summary.sources_not_currently_watched;
  if (summary.sources === 0) {
    return (
      <div className="banner banner-warn">
        No regulatory sources are registered, so nothing is being monitored. Add a source on
        the Sources tab.
      </div>
    );
  }
  if (gap === 0) {
    return (
      <div className="banner banner-ok small">
        All {summary.sources} registered source(s) were collected successfully within their
        check interval. {summary.coverage_note}
      </div>
    );
  }
  return (
    <div className="banner banner-error">
      <strong>
        {gap} of {summary.sources} source(s) are not currently being watched.
      </strong>
      <ul>
        {summary.unwatched.map((s) => (
          <li key={s.id} className="small">
            <span className="mono">{s.name}</span> &mdash; {s.state}. {s.note}
          </li>
        ))}
      </ul>
      <p className="small">
        Their current content is unknown. This is <strong>not</strong> a report that they have
        not changed.
      </p>
    </div>
  );
}

function SourcesTab({
  sources,
  busy,
  onCollect,
  onRefresh,
  onRegistered,
  onError,
}: {
  sources: WatchSource[];
  busy: boolean;
  onCollect: (id: string) => void;
  onRefresh: () => void;
  onRegistered: () => void;
  onError: (message: string) => void;
}) {
  const [form, setForm] = useState({
    name: "",
    url: "",
    jurisdiction: "India",
    topic: "",
    authority: "",
    check_interval_minutes: 1440,
    credential_ref: "",
  });
  const [saving, setSaving] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      await regwatchApi.registerSource({
        name: form.name,
        url: form.url,
        jurisdiction: form.jurisdiction,
        topic: form.topic || null,
        authority: form.authority || null,
        check_interval_minutes: Number(form.check_interval_minutes),
        credential_ref: form.credential_ref || null,
      });
      setForm({ ...form, name: "", url: "", topic: "", authority: "", credential_ref: "" });
      onRegistered();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <section className="section dsr-intake">
        <h3>Approve a source for monitoring</h3>
        <p className="hint">
          Only sources registered here are ever fetched. Nothing else in Agent&nbsp;5 takes a
          URL.
        </p>
        <form onSubmit={submit}>
          <label>
            Name
            <input
              required
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="MeitY &mdash; DPDP notifications"
            />
          </label>
          <label>
            URL
            <input
              required
              value={form.url}
              onChange={(e) => setForm({ ...form, url: e.target.value })}
              placeholder="https://www.meity.gov.in/..."
            />
          </label>
          <label>
            Jurisdiction
            <input
              required
              value={form.jurisdiction}
              onChange={(e) => setForm({ ...form, jurisdiction: e.target.value })}
            />
          </label>
          <label>
            Topic (optional)
            <input
              value={form.topic}
              onChange={(e) => setForm({ ...form, topic: e.target.value })}
              placeholder="consent, rights, breach&hellip;"
            />
          </label>
          <label>
            Authority (optional)
            <input
              value={form.authority}
              onChange={(e) => setForm({ ...form, authority: e.target.value })}
            />
          </label>
          <label>
            Check every (minutes)
            <input
              type="number"
              min={5}
              value={form.check_interval_minutes}
              onChange={(e) =>
                setForm({ ...form, check_interval_minutes: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Credential reference (optional)
            <input
              value={form.credential_ref}
              onChange={(e) => setForm({ ...form, credential_ref: e.target.value })}
              placeholder="REGWATCH_SOURCE_TOKEN"
            />
            <span className="hint">
              The <strong>name</strong> of an environment variable, never the secret itself.
              Consiva stores the name; the value stays in the server environment.
            </span>
          </label>
          <button type="submit" disabled={saving}>
            {saving ? "Registering…" : "Register source"}
          </button>
        </form>
      </section>

      <section className="section">
        <div className="dsr-head">
          <h3>Registered sources</h3>
          <button className="secondary small" onClick={onRefresh}>
            Refresh
          </button>
        </div>
        <table className="ropa-table">
          <thead>
            <tr>
              <th>Source</th>
              <th>Jurisdiction</th>
              <th>Watch state</th>
              <th>Last success</th>
              <th>Auth</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sources.map((s) => (
              <tr key={s.id}>
                <td>
                  <div>{s.name}</div>
                  <div className="small mono muted">{s.url}</div>
                </td>
                <td className="nowrap">{s.jurisdiction}</td>
                <td>
                  <span className={`badge ${HEALTH_TONE[s.health.state] ?? "neutral"}`}>
                    {s.health.state}
                  </span>
                  <div className="small muted">{s.health.note}</div>
                </td>
                <td className="nowrap small mono">
                  {s.health.last_success_at?.slice(0, 16).replace("T", " ") ?? "never"}
                </td>
                <td className="small">
                  {/* The ref name only. The value is never sent to a browser. */}
                  {s.requires_credential ? (
                    <span className="mono">{s.credential_ref}</span>
                  ) : (
                    <span className="muted">none</span>
                  )}
                </td>
                <td>
                  <button
                    className="secondary small"
                    disabled={busy || !s.health.enabled}
                    onClick={() => onCollect(s.id)}
                  >
                    Collect now
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {sources.length === 0 && <p className="empty-note">No sources registered yet.</p>}
      </section>
    </>
  );
}

function FindingDetail({
  finding,
  busy,
  onDecide,
  onOpenActions,
  onCompleteAction,
  onClose,
}: {
  finding: WatchFindingDetail;
  busy: boolean;
  onDecide: (decision: string, reason?: string) => void;
  onOpenActions: (
    actions: { title: string; rationale: string; expected_result: string }[],
  ) => void;
  onCompleteAction: (actionId: string, who: string, note: string) => void;
  onClose: (reason?: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [action, setAction] = useState({ title: "", rationale: "", expected_result: "" });
  const [attest, setAttest] = useState({ id: "", who: "", note: "" });

  const decidable = finding.status === "review_required";
  const canRaiseActions = finding.status === "approved";
  const canClose = finding.status === "approved" || finding.status === "action_open";

  return (
    <section className="section response-body">
      <div className="dsr-head">
        <h3>
          <span className="mono">{finding.reference}</span>
        </h3>
        <span className={`badge ${STATUS_TONE[finding.status] ?? "neutral"}`}>
          {finding.status}
        </span>
      </div>

      {finding.error_code && (
        <div className="banner banner-error small">
          <strong>{finding.error_code}</strong> &mdash; {finding.error_detail}
        </div>
      )}

      <div className="key-value">
        <span>Relevance</span>
        <span>
          <span className={`badge ${RELEVANCE_TONE[finding.relevance] ?? "info"}`}>
            {finding.relevance}
          </span>{" "}
          <ConfidenceTag level={finding.relevance_confidence} />
        </span>
      </div>
      {finding.relevance_reason && <p className="small muted">{finding.relevance_reason}</p>}

      <div className="key-value">
        <span>Priority</span>
        <span>
          {finding.priority ? (
            <>
              <span className={`badge ${PRIORITY_TONE[finding.priority] ?? "neutral"}`}>
                {finding.priority}
              </span>{" "}
              <ConfidenceTag level={finding.priority_confidence} />
            </>
          ) : (
            <span className="muted">not set</span>
          )}
        </span>
      </div>

      <h4>What changed</h4>
      <p>{finding.summary}</p>
      {finding.change && (
        <>
          <p className="small muted">
            {finding.change.change_kind} &mdash; +{finding.change.added_lines} / -
            {finding.change.removed_lines} lines
          </p>
          {finding.change.diff_excerpt && (
            <pre className="mono small">{finding.change.diff_excerpt.slice(0, 2000)}</pre>
          )}
        </>
      )}

      <h4>What it may touch</h4>
      <p className="small">{finding.impact_summary}</p>
      {(finding.impacts ?? []).length > 0 && (
        <table className="ropa-table">
          <tbody>
            {(finding.impacts ?? []).map((i) => (
              <tr key={i.id}>
                <td className="small mono nowrap">{i.target_kind}</td>
                <td className="small">{i.target_label}</td>
                {/* Never dropped: each link is a place to look, not a finding that it
                    is affected. */}
                <td>
                  <ConfidenceTag level={i.confidence} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {finding.citations.length > 0 ? (
        <>
          <h4>Grounded in</h4>
          <ul className="small">
            {finding.citations.map((c) => (
              <li key={c.chunk_id}>
                {c.document_title}
                {c.section ? ` §${c.section}` : ""}
                {c.document_version ? ` (v${c.document_version})` : ""}
              </li>
            ))}
          </ul>
          {finding.drafted_by_model && (
            <p className="hint">
              The plain-English part of the summary was drafted by {finding.drafted_by_model}
              {" "}from the passages above. It describes what the text says; it does not
              establish that anything applies to this organisation.
            </p>
          )}
        </>
      ) : (
        <p className="hint">
          No interpretation was drafted &mdash; nothing in the approved knowledge base was
          close enough to ground one. This change is described from the source text only.
        </p>
      )}

      {finding.open_questions.length > 0 && (
        <>
          <h4>Not established</h4>
          <ul className="small">
            {finding.open_questions.map((q, idx) => (
              <li key={idx}>{q}</li>
            ))}
          </ul>
        </>
      )}

      {decidable && (
        <div className="dsr-actions">
          <label>
            Reason (required to dismiss or escalate)
            <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} />
          </label>
          <button disabled={busy} onClick={() => onDecide("approved", reason || undefined)}>
            Approve &mdash; this is worth acting on
          </button>
          <button
            className="secondary"
            disabled={busy}
            onClick={() => onDecide("dismissed", reason)}
          >
            Dismiss &mdash; does not apply to us
          </button>
          <button
            className="secondary"
            disabled={busy}
            onClick={() => onDecide("escalated", reason)}
          >
            Escalate
          </button>
          <p className="hint">
            Approving records your name against this finding. Dismissing is final &mdash; a
            later change to the same source raises a new finding rather than reopening this
            one.
          </p>
        </div>
      )}

      {canRaiseActions && (
        <div className="dsr-actions">
          <h4>Raise the work this calls for</h4>
          <label>
            Title
            <input
              value={action.title}
              onChange={(e) => setAction({ ...action, title: e.target.value })}
            />
          </label>
          <label>
            Why
            <textarea
              rows={2}
              value={action.rationale}
              onChange={(e) => setAction({ ...action, rationale: e.target.value })}
            />
          </label>
          <label>
            What a completed version looks like
            <textarea
              rows={2}
              value={action.expected_result}
              onChange={(e) => setAction({ ...action, expected_result: e.target.value })}
            />
          </label>
          <button disabled={busy || !action.title} onClick={() => onOpenActions([action])}>
            Raise action
          </button>
          <p className="hint">
            Consiva does not carry any of this out. An action is a task for a person, closed
            by recording who did it and what they did.
          </p>
        </div>
      )}

      {(finding.actions ?? []).length > 0 && (
        <>
          <h4>Actions</h4>
          <table className="ropa-table">
            <tbody>
              {(finding.actions ?? []).map((a) => (
                <tr key={a.id}>
                  <td>
                    <div>{a.title}</div>
                    <div className="small muted">{a.expected_result}</div>
                    {a.completed_by && (
                      <div className="small">
                        Attested by {a.completed_by}: {a.completion_note}
                        {/* Said on the row, not in a footnote. */}
                        <span className="badge neutral">not verified by Consiva</span>
                      </div>
                    )}
                  </td>
                  <td className="nowrap">
                    <span className={`badge ${a.status === "completed" ? "ok" : "warn"}`}>
                      {a.status}
                    </span>
                  </td>
                  <td>
                    {a.status !== "completed" && (
                      <button
                        className="secondary small"
                        onClick={() => setAttest({ id: a.id, who: "", note: "" })}
                      >
                        Record completion
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {attest.id && (
        <div className="dsr-actions">
          <h4>Record what was done</h4>
          <label>
            Who did it
            <input
              value={attest.who}
              onChange={(e) => setAttest({ ...attest, who: e.target.value })}
            />
          </label>
          <label>
            What they did
            <textarea
              rows={2}
              value={attest.note}
              onChange={(e) => setAttest({ ...attest, note: e.target.value })}
            />
          </label>
          <button
            disabled={busy || !attest.who || attest.note.trim().length < 10}
            onClick={() => {
              onCompleteAction(attest.id, attest.who, attest.note);
              setAttest({ id: "", who: "", note: "" });
            }}
          >
            Record attestation
          </button>
          <p className="hint">
            This records your word that the work was done. Consiva did not perform it and
            cannot verify it.
          </p>
        </div>
      )}

      {canClose && (
        <div className="dsr-actions">
          <button
            className="secondary"
            disabled={busy}
            onClick={() => onClose(reason || undefined)}
          >
            Close finding
          </button>
          <p className="hint">
            Refused while any action is still open, so that &ldquo;closed&rdquo; means the
            same thing on every row.
          </p>
        </div>
      )}
    </section>
  );
}
