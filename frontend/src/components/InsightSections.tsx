import type { InsightSection } from "../report/insights";

const TONE_BADGE = { good: "ok", warn: "warn", mute: "neutral" } as const;

/** What kind of knowledge the status rests on: confirmed by the scan or the policy's own words, only observed, or not verified. */
const KNOWLEDGE_NOTE = {
  Confirmed: "Confirmed: measured by the scan, or the policy's own words.",
  Observed: "Observed: the scan saw it. That is not proof of more than it shows.",
  "Not verified": "Not verified: the scan did not see it, which is not evidence that it does not exist.",
} as const;

/**
 * The dedicated sections beside the score: grievance officer, Data Protection Officer, Indian-language support, children's data and the age gate,
 * and observable security signals. Each says where things stand, what kind of knowledge that rests on, the facts, the policy's own words where
 * it has them, what the scan cannot tell, and the provisions. The same reading (report/insights.ts) is printed in the PDF.
 */
export function InsightSections({ sections }: { sections: InsightSection[] }) {
  return (
    <>
      {sections.map((s) => (
        <section className="section" key={s.key} data-testid={`insight-${s.key}`}>
          <h2>{s.title}</h2>
          <p style={{ margin: "4px 0", display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span className={`badge ${TONE_BADGE[s.tone]}`} data-testid={`insight-${s.key}-status`}>{s.status}</span>
            <span className="badge neutral" data-testid={`insight-${s.key}-knowledge`} title={KNOWLEDGE_NOTE[s.knowledge]}>{s.knowledge}</span>
          </p>
          <p className="small" style={{ margin: "6px 0" }}>{s.summary}</p>
          {s.facts.length > 0 && (
            <div style={{ overflowX: "auto" }}>
              <table className="data-table" data-testid={`insight-${s.key}-facts`}>
                <tbody>
                  {s.facts.map((f) => (
                    <tr key={f.label}>
                      <td style={{ width: "36%" }}>{f.label}</td>
                      <td><span className={`badge ${TONE_BADGE[f.tone]}`} style={{ whiteSpace: "normal" }}>{f.value}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {s.quote && (
            <blockquote className="small" style={{ margin: "8px 0", paddingLeft: 10, borderLeft: "3px solid var(--border, #ccd)" }} data-testid={`insight-${s.key}-quote`}>
              “{s.quote.text}”
              {s.quote.source && <div className="muted">— <a href={s.quote.source} target="_blank" rel="noreferrer">{s.quote.source}</a></div>}
            </blockquote>
          )}
          <p className="muted small" style={{ margin: "6px 0 0" }} data-testid={`insight-${s.key}-legal`}>{s.legal}</p>
          {s.notes.map((n) => <p key={n} className="muted small" style={{ margin: "4px 0 0" }}>{n}</p>)}
        </section>
      ))}
    </>
  );
}
