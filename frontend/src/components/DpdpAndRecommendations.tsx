import type { FindingResponse } from "../api/types";

// Both sections below are aggregated from the SAME /findings response -- the backend
// has no separate "DPDP references" or "recommendations" endpoint; each reference and
// recommendation already lives on its originating finding. Nothing here is generated
// on the frontend -- every source_doc/section/recommendation_text string is exactly
// what the backend returned.

export function DpdpReferencesSection({ findings }: { findings: FindingResponse[] }) {
  const all = findings.flatMap((f) => f.dpdp_reference.map((ref) => ({ ref, finding: f })));
  if (all.length === 0) return <p className="empty-note">No DPDP references have been retrieved yet for this scan.</p>;
  return (
    <div>
      {all.map(({ ref, finding }, i) => (
        <div className="dpdp-item" key={`${finding.id}-${i}`}>
          <div style={{ fontWeight: 600 }}>{ref.source_doc}{ref.version ? ` (${ref.version})` : ""}</div>
          {ref.section && <div className="muted" style={{ margin: "3px 0" }}>{ref.section}</div>}
          <div className="muted" style={{ fontSize: 11 }}>
            Chunk: <code>{ref.chunk_id.slice(0, 8)}</code> · cited by finding: {finding.finding_text.slice(0, 60)}…
          </div>
        </div>
      ))}
    </div>
  );
}

export function RecommendationsSection({ findings }: { findings: FindingResponse[] }) {
  const all = findings.flatMap((f) => f.recommendations.map((r) => ({ rec: r, finding: f })));
  if (all.length === 0) return <p className="empty-note">No recommendations have been generated yet for this scan.</p>;
  return (
    <div>
      {all.map(({ rec, finding }) => (
        <div className="recommendation-item" key={rec.id}>
          <div className="what">WHAT SHOULD BE FIXED: {rec.recommendation_text}</div>
          <div className="why">WHY IT MATTERS: {finding.finding_text}</div>
          <div className="muted" style={{ fontSize: 11 }}>Priority: {rec.priority ?? finding.priority}</div>
        </div>
      ))}
    </div>
  );
}
