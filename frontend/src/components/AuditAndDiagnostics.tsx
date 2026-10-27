import type { AuditLogResponse, StageResponse, WebsiteScanMetadata } from "../api/types";
import { friendlyError } from "../errors/friendlyError";

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
  // Plain words only: the technical error is in the server's logs (and the browser console for developers), not on this page.
  if (scanError) {
    const f = friendlyError(scanError);
    lines.push({ text: `${f.message} ${f.next}`, tone: "err" });
  }
  for (const s of stages) {
    if (s.status === "failed" && s.error && !scanError) {
      const f = friendlyError(s.error);
      lines.push({ text: `${f.message} ${f.next}`, tone: "err" });
    }
  }
  if (meta) {
    if (meta.pages_failed) lines.push({ text: `${meta.pages_failed} page(s) couldn't be loaded`, tone: "err" });
    if (meta.pages_timeout) lines.push({ text: `${meta.pages_timeout} page(s) took too long to load`, tone: "err" });
    if (meta.pages_blocked_robots) lines.push({ text: `${meta.pages_blocked_robots} page(s) skipped because the website doesn't allow scanning them`, tone: "warn" });
    if (meta.total_scroll_steps != null) lines.push({ text: `The pages were scrolled ${meta.total_scroll_steps} time(s) to load their content`, tone: "" });
    if (meta.accept_interaction === "cmp_not_automatable" || meta.reject_interaction === "cmp_not_automatable") {
      lines.push({ text: "A consent banner was found, but the scanner couldn't operate it, so Accept and Reject were not tested.", tone: "warn" });
    }
    if (meta.accept_interaction === "page_unreachable" || meta.reject_interaction === "page_unreachable") {
      lines.push({ text: "The page couldn't be reached while testing the consent choices.", tone: "err" });
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
