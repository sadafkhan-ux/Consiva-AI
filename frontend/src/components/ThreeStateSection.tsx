import type { EvidenceTrackerItem, WebsiteScanMetadata } from "../api/types";
import { InteractionPill } from "./shared";

function domainOf(scriptSrc: string): string {
  try {
    return new URL(scriptSrc).hostname;
  } catch {
    return scriptSrc;
  }
}

const STATES: { key: string; title: string }[] = [
  { key: "pre_consent", title: "Before Consent" },
  { key: "post_accept", title: "After Accept" },
  { key: "post_reject", title: "After Reject" },
];

export function ThreeStateSection({
  meta,
  trackers,
}: {
  meta: WebsiteScanMetadata | null;
  trackers: EvidenceTrackerItem[];
}) {
  if (!meta) return null;
  const cByState = meta.cookies_by_consent_state ?? {};
  const tByState = meta.trackers_by_consent_state ?? {};

  return (
    <section className="section">
      <h2>3. Three Consent States — Before / Accept / Reject</h2>
      <div className="state-cards">
        {STATES.map(({ key, title }) => {
          const sampleTrackers = trackers.filter((t) => t.consent_states.includes(key)).slice(0, 5);
          const pill = key === "pre_consent" ? null : key === "post_accept" ? meta.accept_interaction : meta.reject_interaction;
          return (
            <div className="state-card" key={key}>
              <h3>{title}</h3>
              {pill ? <InteractionPill status={pill} /> : <span className="badge neutral">Baseline (no interaction)</span>}
              <div className="counts">
                <div><b>{cByState[key] ?? 0}</b> cookies</div>
                <div><b>{tByState[key] ?? 0}</b> trackers / third-party requests</div>
              </div>
              {sampleTrackers.length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <div className="hint" style={{ margin: "0 0 4px" }}>Sample domains observed in this state:</div>
                  {sampleTrackers.map((t) => (
                    <div key={t.id} className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>{domainOf(t.script_src)}</div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <p className="hint" style={{ marginTop: 12 }}>
        Trackers here represent every observed third-party script/network request (the backend does not separately
        persist raw "script" vs "third-party resource" categories per consent state — a tracker record already
        represents a detected third-party script or beacon).
      </p>
    </section>
  );
}
