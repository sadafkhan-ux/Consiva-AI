import { useCallback, useEffect, useState } from "react";
import {
  regwatchApi,
  type Confidence,
  type WatchAuditEntry,
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
  // Disabled is not neutral. Nobody is watching this source, and the row should not
  // read as an inert setting.
  not_monitored: "warn",
};

const ACTION_TONE: Record<string, string> = {
  completed: "ok",
  cancelled: "neutral",
  blocked: "bad",
  in_progress: "info",
  open: "warn",
};

const DUE_TONE: Record<string, string> = {
  overdue: "bad",
  due_soon: "warn",
  on_track: "ok",
};

/** How a link was arrived at. A rule's guess and a person's assertion are not the
 *  same claim, so the list says which on every row. */
const DERIVED_LABEL: Record<string, string> = {
  rule: "topic rule",
  ropa_metadata: "from your ROPA",
  model: "model",
  manual: "you added this",
};

const DERIVED_TONE: Record<string, string> = {
  rule: "neutral",
  ropa_metadata: "info",
  model: "warn",
  manual: "ok",
};

/** Mirrors the backend's IMPACT_TARGET_KINDS. `control` is here even though nothing
 *  maps to it automatically -- there is no control register for a rule to read, but a
 *  reviewer can still file one by hand. */
const IMPACT_KINDS = [
  "ropa_record",
  "ropa_data_source",
  "consent_website",
  "consent_finding",
  "dsr_configuration",
  "incident_case",
  "policy",
  "control",
  "other",
];

/** The audit log stores machine actions; the timeline is read by compliance people. */
const AUDIT_LABEL: Record<string, string> = {
  "regwatch.finding_created": "Change detected and finding raised",
  "regwatch.relevance_assessed": "Relevance and priority assessed",
  "regwatch.impact_mapped": "Impact mapped to this organisation",
  "regwatch.interpreted": "Plain-English interpretation drafted",
  "regwatch.review_requested": "Sent for human review",
  "regwatch.approved": "Approved by a reviewer",
  "regwatch.dismissed": "Dismissed by a reviewer",
  "regwatch.action_created": "Follow-up action raised",
  "regwatch.action_completed": "Action attested as done",
  "regwatch.closed": "Finding closed",
  "regwatch.status_changed": "Status changed",
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
  // Spec section 8 lists an audit timeline on the compliance view, and section 10
  // asks that a reviewer can see when a change was first detected, when it was
  // reviewed and what happened afterwards. The endpoint existed; nothing called it.
  const [timeline, setTimeline] = useState<WatchAuditEntry[]>([]);
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
      const [detail, audit] = await Promise.all([
        regwatchApi.getFinding(id),
        regwatchApi.audit(id),
      ]);
      setSelected(detail);
      setTimeline(audit.entries);
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
                {/* Which regulator said it. A reviewer triaging a queue needs the
                    source and jurisdiction before the prose. */}
                <div className="small muted">
                  {f.source ? `${f.source.name} · ${f.source.jurisdiction}` : "source removed"}
                </div>
                <div className="small muted">{(f.summary ?? "").slice(0, 160)}</div>
              </button>
            ))}
          </section>

          {selected && (
            <FindingDetail
              finding={selected}
              timeline={timeline}
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
              onAcceptBaseline={(note) =>
                act(
                  () => regwatchApi.acceptBaselineFromFinding(selected.id, note),
                  "Baseline accepted. Future checks are measured against this snapshot, "
                    + "and this finding is closed.",
                )
              }
              onAddImpact={(body) =>
                act(
                  () => regwatchApi.addImpact(selected.id, body),
                  "Link recorded as your assertion. Re-assessment will not erase it.",
                )
              }
              onRemoveImpact={(impactId) =>
                act(() => regwatchApi.removeImpact(impactId), "Link withdrawn.")
              }
              onSetActionStatus={(actionId, status, reason) =>
                act(
                  () => regwatchApi.setActionStatus(actionId, status, reason),
                  `Action marked ${status}. Nothing was carried out by Consiva.`,
                )
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
  const overdue = summary.overdue_actions > 0 && (
    <div className="banner banner-warn small">
      {summary.overdue_actions} action(s) are past the date set for them.{" "}
      {/* The qualifier is not optional. Read without it, "overdue" in a compliance
          tool implies a missed legal obligation, which is not what this measures. */}
      {summary.overdue_note}
    </div>
  );

  if (summary.sources === 0) {
    return (
      <>
        <div className="banner banner-warn">
          No regulatory sources are registered, so nothing is being monitored. Add a source
          on the Sources tab.
        </div>
        {overdue}
      </>
    );
  }
  if (gap === 0) {
    return (
      <>
        <div className="banner banner-ok small">
          All {summary.sources} registered source(s) were collected successfully within
          their check interval. {summary.coverage_note}
        </div>
        {overdue}
      </>
    );
  }
  return (
    <>
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
    {overdue}
    </>
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
    connector: "http",
    topic: "",
    authority: "",
    check_interval_minutes: 1440,
    credential_ref: "",
  });
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState({ id: "", name: "", content: "" });
  const isManual = form.connector === "manual_upload";

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      await regwatchApi.registerSource({
        name: form.name,
        url: form.url,
        jurisdiction: form.jurisdiction,
        connector: form.connector,
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
            How it is read
            <select
              value={form.connector}
              onChange={(e) => setForm({ ...form, connector: e.target.value })}
            >
              <option value="http">Web page (fetched on a schedule)</option>
              <option value="rss">Feed (parsed as items, so a new entry is one line)</option>
              <option value="manual_upload">Uploaded by hand (never fetched)</option>
            </select>
          </label>
          <label>
            URL
            <input
              required={!isManual}
              disabled={isManual}
              value={form.url}
              onChange={(e) => setForm({ ...form, url: e.target.value })}
              placeholder={isManual ? "not used — this source is never fetched" : "https://..."}
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
                  {s.connector === "manual_upload" ? (
                    // A manual source is never fetched, so offering "Collect now"
                    // would be a button that cannot do what it says.
                    <button
                      className="secondary small"
                      disabled={busy || !s.health.enabled}
                      onClick={() => setUploading({ id: s.id, name: s.name, content: "" })}
                    >
                      Upload content
                    </button>
                  ) : (
                    <button
                      className="secondary small"
                      disabled={busy || !s.health.enabled}
                      onClick={() => onCollect(s.id)}
                    >
                      Collect now
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {sources.length === 0 && <p className="empty-note">No sources registered yet.</p>}
      </section>

      {uploading.id && (
        <section className="section dsr-actions">
          <h3>Upload content for {uploading.name}</h3>
          <p className="hint">
            This source is never fetched, so its content is whatever was last pasted here.
            It is compared against the baseline exactly like a fetched page, and the record
            notes that it arrived by hand rather than from the source itself.
          </p>
          <label>
            Content
            <textarea
              rows={10}
              value={uploading.content}
              onChange={(e) => setUploading({ ...uploading, content: e.target.value })}
            />
          </label>
          <button
            disabled={busy || !uploading.content.trim()}
            onClick={() => {
              void (async () => {
                try {
                  await regwatchApi.uploadManualContent(uploading.id, uploading.content);
                  setUploading({ id: "", name: "", content: "" });
                  onRefresh();
                } catch (e) {
                  onError(e instanceof Error ? e.message : String(e));
                }
              })();
            }}
          >
            Upload
          </button>
          <button
            className="secondary"
            onClick={() => setUploading({ id: "", name: "", content: "" })}
          >
            Cancel
          </button>
        </section>
      )}
    </>
  );
}

function FindingDetail({
  finding,
  timeline,
  busy,
  onDecide,
  onAcceptBaseline,
  onAddImpact,
  onRemoveImpact,
  onSetActionStatus,
  onOpenActions,
  onCompleteAction,
  onClose,
}: {
  finding: WatchFindingDetail;
  timeline: WatchAuditEntry[];
  busy: boolean;
  onDecide: (decision: string, reason?: string) => void;
  onAcceptBaseline: (note?: string) => void;
  onAddImpact: (body: { target_kind: string; target_label: string; rationale: string }) => void;
  onRemoveImpact: (impactId: string) => void;
  onSetActionStatus: (actionId: string, status: string, reason?: string) => void;
  onOpenActions: (
    actions: { title: string; rationale: string; expected_result: string }[],
  ) => void;
  onCompleteAction: (actionId: string, who: string, note: string) => void;
  onClose: (reason?: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [action, setAction] = useState({ title: "", rationale: "", expected_result: "" });
  const [attest, setAttest] = useState({ id: "", who: "", note: "" });
  const [cancelling, setCancelling] = useState({ id: "", reason: "" });
  const [manual, setManual] = useState({
    target_kind: "other",
    target_label: "",
    rationale: "",
  });

  const decidable = finding.status === "review_required";
  // A first capture is waiting on one thing: somebody adopting it as the reference
  // point. Until they do, every re-check reports the same capture again.
  const awaitingBaseline =
    decidable && finding.change?.change_kind === "first_capture";
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

      {finding.source && (
        <div className="key-value">
          <span>Source</span>
          <span>
            {finding.source.name}
            {finding.source.authority && (
              <span className="muted"> — {finding.source.authority}</span>
            )}{" "}
            <span className="badge info">{finding.source.jurisdiction}</span>
          </span>
        </div>
      )}

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
                <td className="small">
                  {i.target_label}
                  {/* The rationale carries the evidence for an evidenced link, and
                      the reviewer's words for a manual one. Both are worth reading;
                      a rule's boilerplate is not, so only the first two are shown. */}
                  {i.derived_from !== "rule" && i.rationale && (
                    <div className="small muted">{i.rationale}</div>
                  )}
                </td>
                {/* Never dropped: each link is a place to look, not a finding that it
                    is affected. */}
                <td className="nowrap">
                  <ConfidenceTag level={i.confidence} />
                  <span className={`badge ${DERIVED_TONE[i.derived_from] ?? "neutral"}`}>
                    {DERIVED_LABEL[i.derived_from] ?? i.derived_from}
                  </span>
                </td>
                <td>
                  {i.derived_from === "manual" && (
                    <button
                      className="secondary small"
                      disabled={busy}
                      onClick={() => onRemoveImpact(i.id)}
                    >
                      Remove
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="dsr-actions">
        <h4>Add something the rules could not see</h4>
        <p className="hint">
          The impact list above only covers what Consiva holds. A contract with an
          offshore vendor that never became a ROPA entry, or a control that lives in a
          runbook, is invisible to it &mdash; and the list would otherwise present that
          limit as the whole picture.
        </p>
        <label>
          What it touches
          <select
            value={manual.target_kind}
            onChange={(e) => setManual({ ...manual, target_kind: e.target.value })}
          >
            {IMPACT_KINDS.map((k) => (
              <option key={k} value={k}>
                {k.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <label>
          Name it
          <input
            value={manual.target_label}
            onChange={(e) => setManual({ ...manual, target_label: e.target.value })}
            placeholder="e.g. Vendor agreement with Acme Ltd (US)"
          />
        </label>
        <label>
          Why this is affected
          <textarea
            rows={2}
            value={manual.rationale}
            onChange={(e) => setManual({ ...manual, rationale: e.target.value })}
          />
        </label>
        <button
          disabled={
            busy || !manual.target_label.trim() || manual.rationale.trim().length < 20
          }
          onClick={() => {
            onAddImpact(manual);
            setManual({ ...manual, target_label: "", rationale: "" });
          }}
        >
          Add link
        </button>
        <p className="hint">
          Recorded as your assertion, at <strong>confirmed</strong> &mdash; the one
          confidence no rule in this agent may reach, because here a person is the one
          saying it. Re-running the assessment will not erase it.
        </p>
      </div>

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

      {awaitingBaseline && (
        <div className="dsr-actions">
          <h4>Adopt this snapshot as the baseline</h4>
          <p className="small">
            There was no baseline for this source, so nothing could be compared. Accepting
            this capture makes it the reference point &mdash; from then on you are told what
            changed against it. Until somebody accepts one, every check reports this same
            first capture again.
          </p>
          <label>
            Note (optional)
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <button disabled={busy} onClick={() => onAcceptBaseline(reason || undefined)}>
            Accept as baseline
          </button>
          <p className="hint">
            Recorded against your name, and this finding closes &mdash; adopting the
            reference point is the work it was asking for. It is not a judgement that the
            source matters or does not.
          </p>
        </div>
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
                    <span className={`badge ${ACTION_TONE[a.status] ?? "warn"}`}>
                      {a.status}
                    </span>
                    {/* Never a bare "overdue": the qualifier goes with it, because
                        this is the organisation's own date, not a legal one. */}
                    {a.due.state !== "no_date" && a.due.state !== "settled" && (
                      <div className="small">
                        <span className={`badge ${DUE_TONE[a.due.state] ?? "neutral"}`}>
                          {a.due.state.replace("_", " ")}
                        </span>
                        <span className="muted"> own target, not statutory</span>
                      </div>
                    )}
                  </td>
                  <td>
                    {a.status !== "completed" && a.status !== "cancelled" && (
                      <>
                        <button
                          className="secondary small"
                          onClick={() => setAttest({ id: a.id, who: "", note: "" })}
                        >
                          Record completion
                        </button>
                        {a.status !== "in_progress" && (
                          <button
                            className="secondary small"
                            disabled={busy}
                            onClick={() => onSetActionStatus(a.id, "in_progress")}
                          >
                            Start
                          </button>
                        )}
                        <button
                          className="secondary small"
                          onClick={() => setCancelling({ id: a.id, reason: "" })}
                        >
                          Cancel
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {cancelling.id && (
        <div className="dsr-actions">
          <h4>Cancel this action</h4>
          <label>
            Why it is not being done
            <textarea
              rows={2}
              value={cancelling.reason}
              onChange={(e) => setCancelling({ ...cancelling, reason: e.target.value })}
            />
          </label>
          <button
            disabled={busy || cancelling.reason.trim().length < 10}
            onClick={() => {
              onSetActionStatus(cancelling.id, "cancelled", cancelling.reason);
              setCancelling({ id: "", reason: "" });
            }}
          >
            Cancel action
          </button>
          <p className="hint">
            A compliance action that was dropped with no record of why is a gap nobody
            can explain later, so the reason is required. Cancelling does not mean the
            work was done.
          </p>
        </div>
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

      <h4>Audit timeline</h4>
      {timeline.length === 0 ? (
        <p className="empty-note">No audit entries recorded for this finding.</p>
      ) : (
        <table className="ropa-table">
          <tbody>
            {timeline.map((e) => (
              <tr key={e.id}>
                <td className="small mono nowrap">
                  {e.created_at?.slice(0, 16).replace("T", " ") ?? "—"}
                </td>
                <td className="small">
                  {AUDIT_LABEL[e.action] ?? e.action.replace("regwatch.", "").replace(/_/g, " ")}
                </td>
                <td className="small muted">
                  {/* Whether a person did it, or the agent did. The audit log records
                      an actor only for human acts; a sweep has none. */}
                  {e.actor_user_id ? "a person" : "the agent"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
