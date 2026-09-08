import type { AuditLogResponse, StageResponse, WebsiteScanMetadata } from "../api/types";

export function AuditSection({ audit }: { audit: AuditLogResponse[] }) {
  if (audit.length === 0) return <p className="empty-note">No audit events recorded yet for this scan.</p>;
  return (
    <ul className="timeline">
      {audit.map((a) => (
        <li key={a.id}>
          <b>{a.action}</b>
          <div className="muted" style={{ fontSize: 12 }}>
            entity: {a.entity_type}:{a.entity_id.slice(0, 8)} · actor: {a.actor_user_id ? a.actor_user_id.slice(0, 8) : "system"}
            {a.provider ? ` · provider: ${a.provider}` : ""}
            {a.model_name ? ` · model: ${a.model_name}` : ""}
          </div>
        </li>
      ))}
    </ul>
  );
}

export function DiagnosticsSection({
  scanError,
  stages,
  meta,
}: {
  scanError: string | null;
  stages: StageResponse[];
  meta: WebsiteScanMetadata | null;
}) {
  const lines: { text: string; tone: "err" | "warn" | "" }[] = [];
  if (scanError) lines.push({ text: `Scan error: ${scanError}`, tone: "err" });
  for (const s of stages) {
    if (s.status === "failed" && s.error) lines.push({ text: `Stage "${s.stage}" failed: ${s.error}`, tone: "err" });
  }
  if (meta) {
    if (meta.pages_failed) lines.push({ text: `${meta.pages_failed} page(s) failed to load`, tone: "err" });
    if (meta.pages_timeout) lines.push({ text: `${meta.pages_timeout} page(s) timed out`, tone: "err" });
    if (meta.pages_blocked_robots) lines.push({ text: `${meta.pages_blocked_robots} page(s) blocked by robots.txt`, tone: "warn" });
    if (meta.total_scroll_steps != null) lines.push({ text: `${meta.total_scroll_steps} scroll step(s) across the pre-consent crawl`, tone: "" });
    if (meta.accept_interaction === "cmp_not_automatable" || meta.reject_interaction === "cmp_not_automatable") {
      lines.push({ text: "CMP detected but could not be automated (cmp_not_automatable)", tone: "warn" });
    }
    if (meta.accept_interaction === "page_unreachable" || meta.reject_interaction === "page_unreachable") {
      lines.push({ text: "Page Unreachable during a consent-state pass", tone: "err" });
    }
  }

  if (lines.length === 0) return <p className="empty-note">No errors or warnings for this scan.</p>;

  return (
    <ul className="diag-list">
      {lines.map((l, i) => (
        <li key={i} className={l.tone}>{l.text}</li>
      ))}
    </ul>
  );
}
