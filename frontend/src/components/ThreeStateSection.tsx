import type { EvidenceCookieItem, EvidenceTrackerItem, WebsiteScanMetadata } from "../api/types";
import { countByState, REQUEST_UNIT_NOTE } from "../report/stateCounts";
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
  cookies,
}: {
  meta: WebsiteScanMetadata | null;
  trackers: EvidenceTrackerItem[];
  /** The scan's cookie rows. When given, every figure is counted from the evidence rows exactly as the report counts them
   *  (report/stateCounts.ts). When absent (evidence not loaded) the stored summary figures are shown instead. */
  cookies?: EvidenceCookieItem[];
}) {
  if (!meta) return null;
  const counted = cookies ? countByState(cookies, trackers) : null;
  const cByState = counted ? counted.cookies : (meta.cookies_by_consent_state ?? {});
  const tByState = counted ? counted.requests : (meta.trackers_by_consent_state ?? {});

  return (
    <section className="section">
      <h2>3. Three Consent States — Before / Accept / Reject</h2>
      <div className="state-cards">
        {STATES.map(({ key, title }) => {
          const sampleTrackers = trackers.filter((t) => t.consent_states.includes(key)).slice(0, 5);
          const hosts = [...new Set(
            trackers.filter((t) => t.consent_states.includes(key) && t.page_url).map((t) => domainOf(t.page_url as string)),
          )];
          const pill = key === "pre_consent" ? null : key === "post_accept" ? meta.accept_interaction : meta.reject_interaction;
          return (
            <div className="state-card" key={key}>
              <h3>{title}</h3>
              {pill ? <InteractionPill status={pill} /> : <span className="badge neutral">Baseline (no interaction)</span>}
              <div className="counts">
                <div><b>{cByState[key as keyof typeof cByState] ?? 0}</b> cookies</div>
                <div><b>{tByState[key as keyof typeof tByState] ?? 0}</b> third-party requests</div>
                {counted && <div className="hint" style={{ margin: 0 }}><b>{counted.knownRequests[key as keyof typeof counted.knownRequests]}</b> analytics or marketing</div>}
              </div>
              {pill && pill !== "clicked" && (
                <div className="hint" style={{ margin: "8px 0 0" }} data-testid={`state-untested-${key}`}>
                  No choice was made, so these figures are the page seen again — not what happens after {key === "post_accept" ? "Accept" : "Reject"}.
                </div>
              )}
              {hosts.length > 0 && (
                <div className="hint" style={{ margin: "8px 0 0" }} data-testid={`state-pages-${key}`}>
                  Trackers recorded on: {hosts.join(", ")}
                </div>
              )}
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
      <p className="hint" style={{ marginTop: 12 }} data-testid="state-unit-note">
        {REQUEST_UNIT_NOTE} Cookies are counted the same way: once each, in every state they were seen.
      </p>
    </section>
  );
}
