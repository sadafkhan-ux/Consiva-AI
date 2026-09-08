import type { WebsiteScanMetadata } from "../api/types";
import { InteractionPill } from "./shared";

export function ConsentSummarySection({ meta }: { meta: WebsiteScanMetadata | null }) {
  if (!meta) return null;
  const bannerPresent = meta.consent_mechanism && meta.consent_mechanism !== "none";
  const bannerLabel = meta.consent_mechanism == null ? "UNKNOWN" : bannerPresent ? "DETECTED" : "NOT DETECTED";
  const cmpLabel = meta.cmp_vendor ?? (meta.consent_mechanism === "banner" ? "Generic banner (vendor unknown)" : "UNKNOWN");
  const confidencePct = meta.cmp_confidence != null ? `${Math.round(meta.cmp_confidence * 100)}%` : "—";

  return (
    <section className="section">
      <h2>2. Consent Summary</h2>
      <div className="grid cols-4">
        <div className="stat">
          <div className="n" style={{ fontSize: 15 }}>{bannerLabel}</div>
          <div className="l">Consent Banner</div>
        </div>
        <div className="stat">
          <div className="n" style={{ fontSize: 15 }}>{cmpLabel}</div>
          <div className="l">CMP / Provider</div>
        </div>
        <div className="stat">
          <div className="n">{confidencePct}</div>
          <div className="l">Confidence ({meta.cmp_detection_source ?? "n/a"})</div>
        </div>
        <div className="stat">
          <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "center" }}>
            <div><span className="muted" style={{ fontSize: 10.5 }}>ACCEPT </span><InteractionPill status={meta.accept_interaction} /></div>
            <div><span className="muted" style={{ fontSize: 10.5 }}>REJECT </span><InteractionPill status={meta.reject_interaction} /></div>
          </div>
          <div className="l">Accept / Reject</div>
        </div>
      </div>
    </section>
  );
}
