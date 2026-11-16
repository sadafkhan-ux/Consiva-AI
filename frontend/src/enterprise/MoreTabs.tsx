import { useEffect, useMemo, useState, type ReactNode } from "react";
import { enterpriseScans, enterpriseErrorMessage, fetchEnterpriseReportPdf, siteHost, type ChangesView, type FindingView, type ReportVersion, type ScanListItem } from "../api/enterprise";
import { saveBlob } from "../api/client";
import { asList, bytes, count, dateOnly, dateTime, text, words } from "./format";
import { useLoaded, usePaged } from "./hooks";
import { stripRef } from "./ListTabs";
import { Empty, ErrorNote, PagedView, Pill, Skeleton } from "./ui";

const SEVERITY_TONE = { high: "bad", medium: "warn", low: "info", info: "muted" } as const;

function FindingCard({ f, onEvidence }: { f: FindingView; onEvidence: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const tone = SEVERITY_TONE[f.severity] ?? "muted";
  const shown = f.affected_urls.length;
  return (
    <article className="finding-card" data-testid={`finding-${f.finding_key}`}>
      <div className="finding-head">
        <Pill tone={tone}>{text(f.severity)}</Pill> <strong>{f.title}</strong> <span className="muted small">{f.parameter_id}</span>
        {f.human_review && <Pill tone="info">Human review</Pill>}
      </div>
      <button type="button" className="secondary small" aria-expanded={open} onClick={() => setOpen((v) => !v)}>{open ? "Hide details" : "Show details"}</button>
      {open && (
        <dl style={{ margin: "8px 0 0" }} data-testid="finding-details">
          {([["Description", f.description], ["Expected", f.expected], ["Observed", f.observed], ["Reason", f.reason], ["Recommendation", f.recommendation]] as [string, string | null][]).map(([k, v]) => (
            <div key={k} style={{ marginBottom: 6 }}><dt className="muted small">{k}</dt><dd style={{ margin: 0 }}>{text(v)}</dd></div>
          ))}
          <div style={{ marginBottom: 6 }}>
            <dt className="muted small">Affected URLs ({count(f.affected_count)} in total{shown < (f.affected_count ?? 0) ? `, first ${shown} shown` : ""})</dt>
            <dd style={{ margin: 0 }}>{shown === 0 ? "None listed" : <ul style={{ margin: 0, paddingLeft: 18 }}>{f.affected_urls.map((u) => <li key={u} className="mono" style={{ wordBreak: "break-all" }}>{u}</li>)}</ul>}</dd>
          </div>
          <div style={{ marginBottom: 6 }}>
            <dt className="muted small">Evidence</dt>
            <dd style={{ margin: 0 }}>{f.evidence_refs.length === 0 ? "None linked" : f.evidence_refs.map((r) => <button key={r} type="button" className="secondary small" style={{ marginRight: 4 }} onClick={() => onEvidence(stripRef(r))}>{r.length > 14 ? `${r.slice(0, 14)}…` : r}</button>)}</dd>
          </div>
          <div style={{ marginBottom: 6 }}><dt className="muted small">Legal references</dt><dd style={{ margin: 0 }}>{f.legal_refs.length ? f.legal_refs.join(", ") : "None recorded"}</dd></div>
          <div>
            <dt className="muted small">Penalty ceiling</dt>
            <dd style={{ margin: 0 }} data-testid="penalty-ceiling">
              {typeof f.penalty_ceiling_cr === "number"
                ? `The Act's Schedule sets a ceiling of up to Rs ${f.penalty_ceiling_cr} crore${f.schedule_item ? ` (Schedule item ${f.schedule_item})` : ""} for this kind of breach. It is a limit set by law on what the Board may impose, not a prediction of any penalty.`
                : "No ceiling recorded for this finding."}
            </dd>
          </div>
        </dl>
      )}
    </article>
  );
}

export function FindingsTab({ scanId, refresh, onEvidence }: { scanId: string; refresh: string; onEvidence: (id: string) => void }) {
  const [severity, setSeverity] = useState("");
  const p = usePaged<FindingView>((offset, limit) => enterpriseScans.findings(scanId, { severity, offset, limit }), `${scanId}|${severity}`, 20, refresh);
  return (
    <div data-testid="findings-tab">
      <div className="inline-row">
        <label htmlFor="f-sev">Severity</label>
        <select id="f-sev" value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option value="">All</option>
          {["high", "medium", "low", "info"].map((s) => <option key={s} value={s}>{words(s)}</option>)}
        </select>
      </div>
      <PagedView p={p} noun="findings" empty="No findings for this scan.">
        {(items) => <div>{items.map((f) => <FindingCard key={f.id} f={f} onEvidence={onEvidence} />)}</div>}
      </PagedView>
    </div>
  );
}

function ChangeList({ title, items }: { title: string; items: ReactNode[] }) {
  return (
    <section style={{ marginBottom: 10 }} aria-label={title}>
      <h4 style={{ margin: "0 0 4px" }}>{title}</h4>
      {items.length === 0 ? <p className="muted small">No change.</p> : <ul style={{ margin: 0, paddingLeft: 18 }}>{items.map((it, i) => <li key={i} className="small" style={{ wordBreak: "break-all" }}>{it}</li>)}</ul>}
    </section>
  );
}

export function ChangesTab({ scanId, websiteUrl, createdAt }: { scanId: string; websiteUrl: string; createdAt: string }) {
  const list = useLoaded(() => enterpriseScans.list({ limit: 200 }), scanId);
  const earlier = useMemo<ScanListItem[]>(() => {
    const host = siteHost(websiteUrl);
    return (list.data?.items ?? []).filter((s) => s.scan_id !== scanId && siteHost(s.website_url) === host && s.created_at < createdAt);
  }, [list.data, scanId, websiteUrl, createdAt]);
  const [against, setAgainst] = useState("");
  const [cmp, setCmp] = useState<{ data: ChangesView | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: false });
  useEffect(() => {
    if (!against) return;
    let alive = true;
    setCmp({ data: null, error: null, loading: true });
    enterpriseScans.changes(scanId, against).then((d) => alive && setCmp({ data: d, error: null, loading: false })).catch((e) => alive && setCmp({ data: null, error: enterpriseErrorMessage(e), loading: false }));
    return () => { alive = false; };
  }, [against, scanId]);
  const c = cmp.data;
  const dl = (n: number | null | undefined) => (typeof n === "number" ? (n > 0 ? `+${n}` : String(n)) : "not recorded yet");
  return (
    <div data-testid="changes-tab">
      {list.error ? <ErrorNote message={list.error} onRetry={list.reload} /> : !list.data ? <Skeleton rows={2} /> : earlier.length === 0 ? <Empty>There is no earlier scan of this site to compare with.</Empty> : (
        <div className="inline-row">
          <label htmlFor="c-against">Compare with</label>
          <select id="c-against" value={against} onChange={(e) => setAgainst(e.target.value)}>
            <option value="">Choose an earlier scan</option>
            {earlier.map((s) => <option key={s.scan_id} value={s.scan_id}>{dateOnly(s.created_at)} ({words(s.completion ?? s.status)})</option>)}
          </select>
        </div>
      )}
      {cmp.loading && <Skeleton rows={4} />}
      {cmp.error && <ErrorNote message={cmp.error} />}
      {c && !c.comparable && <div className="banner banner-warn" role="status">The two scans cannot be compared yet.</div>}
      {c && c.comparable && (
        <div data-testid="changes-result">
          {c.score && (
            <p data-testid="score-delta"><strong>Score:</strong> {c.score.previous ?? "no score"} to {c.score.current ?? "no score"}{c.score.delta !== null && c.score.delta !== undefined ? ` (${dl(c.score.delta)})` : ""}</p>
          )}
          <ChangeList title="URLs" items={c.urls ? [`${count(c.urls.added)} added`, `${count(c.urls.removed)} removed`, ...(c.urls.added_examples ?? []).slice(0, 5).map((u) => `added: ${u}`), ...(c.urls.removed_examples ?? []).slice(0, 5).map((u) => `removed: ${u}`)] : []} />
          <ChangeList title="Policies" items={(c.policies?.changed ?? []).map((p) => `${p.url} changed`)} />
          <ChangeList title="Cookies" items={[...(c.cookies?.added ?? []).map((x) => `added: ${x}`), ...(c.cookies?.removed ?? []).map((x) => `removed: ${x}`)]} />
          <ChangeList title="Trackers" items={[...(c.trackers?.added ?? []).map((x) => `added: ${x}`), ...(c.trackers?.removed ?? []).map((x) => `removed: ${x}`)]} />
          <ChangeList title="Forms" items={c.forms && ((c.forms.added ?? 0) > 0 || (c.forms.removed ?? 0) > 0) ? [`${count(c.forms.added)} added, ${count(c.forms.removed)} removed`] : []} />
          <ChangeList title="Consent" items={(c.consent?.changes ?? []).map((x) => `${words(x.test)}: ${words(x.from)} to ${words(x.to)}`)} />
          <ChangeList title="Findings" items={[`${c.findings?.new.length ?? 0} new`, `${c.findings?.resolved.length ?? 0} resolved`, ...(c.findings?.changed ?? []).map((x) => `${x.finding_key}: ${words(x.from)} to ${words(x.to)}`)].filter((x) => !/^0 /.test(x))} />
          <ChangeList title="Coverage" items={c.coverage ? [`Previous: ${JSON.stringify(c.coverage.previous ?? {})}`, `Current: ${JSON.stringify(c.coverage.current ?? {})}`] : []} />
        </div>
      )}
    </div>
  );
}

const REPORT_BUSY = new Set(["queued", "running", "generating", "pending"]);

export function ReportTab({ scanId, refresh }: { scanId: string; refresh: string }) {
  const r = useLoaded<ReportVersion[]>(async () => asList((await enterpriseScans.reports(scanId)) as { items: ReportVersion[] }), scanId, refresh);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const reload = r.reload;
  const generating = (r.data ?? []).some((v) => REPORT_BUSY.has(v.status));
  useEffect(() => {
    if (!generating) return;
    const t = window.setTimeout(reload, 4000);
    return () => window.clearTimeout(t);
  }, [generating, r.data, reload]);

  async function generate() {
    setBusy("generate");
    setNote(null);
    try {
      const g = await enterpriseScans.generateReport(scanId);
      setNote({ ok: true, text: `Report version ${g.version} is ${words(g.status)}.` });
      reload();
    } catch (e) {
      setNote({ ok: false, text: enterpriseErrorMessage(e) });
    } finally {
      setBusy(null);
    }
  }
  async function download(version?: number) {
    setBusy(`dl-${version ?? "latest"}`);
    setNote(null);
    try {
      const f = await fetchEnterpriseReportPdf(scanId, version);
      saveBlob(f.blob, f.filename);
    } catch (e) {
      setNote({ ok: false, text: enterpriseErrorMessage(e) });
    } finally {
      setBusy(null);
    }
  }
  return (
    <div data-testid="report-tab">
      <div className="inline-row">
        <button type="button" disabled={busy !== null} onClick={generate}>{busy === "generate" ? "Generating…" : "Generate report"}</button>
        <button type="button" className="secondary" disabled={busy !== null || (r.data ?? []).every((v) => v.status !== "ready")} onClick={() => download()}>Download latest PDF</button>
      </div>
      {note && <div className={`banner ${note.ok ? "banner-ok" : "banner-error"}`} role="status" data-testid="report-note">{note.text}</div>}
      {r.error ? <ErrorNote message={r.error} onRetry={r.reload} /> : !r.data ? <Skeleton rows={3} /> : r.data.length === 0 ? <Empty>No report has been generated yet.</Empty> : (
        <div className="check-table-wrap">
          <table className="ropa-table" data-testid="report-versions">
            <thead><tr><th>Version</th><th>Status</th><th>Pages</th><th>Size</th><th>Generated</th><th><span aria-hidden>Download</span></th></tr></thead>
            <tbody>
              {r.data.map((v) => (
                <tr key={v.version}>
                  <td>{v.version}</td>
                  <td><Pill tone={v.status === "ready" ? "ok" : v.status === "failed" ? "bad" : "info"}>{words(v.status)}</Pill></td>
                  <td>{count(v.page_count)}</td>
                  <td>{bytes(v.byte_size)}</td>
                  <td>{dateTime(v.generated_at)}</td>
                  <td><button type="button" className="secondary small" disabled={v.status !== "ready" || busy !== null} onClick={() => download(v.version)} aria-label={`Download version ${v.version}`}>PDF</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
