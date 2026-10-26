import type { EvidenceStatus } from "../hooks/useConsentScan";
import { EvidenceNotice } from "./EvidenceNotice";
import type { EvidenceFormItem, EvidencePolicyItem } from "../api/types";

export function FormsSection({ forms, status = "loaded" }: { forms: EvidenceFormItem[]; status?: EvidenceStatus }) {
  if (status !== "loaded") return <EvidenceNotice status={status} what="forms" />;
  if (forms.length === 0) return <p className="empty-note">No forms were detected on this scan.</p>;
  return (
    <div className="grid cols-3">
      {forms.map((f) => (
        <div className="stat" key={f.id} style={{ textAlign: "left" }}>
          <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>{f.selector ?? "Form"}</div>
          <div className="muted" style={{ fontSize: 11.5, marginBottom: 6 }}>Purpose: {f.purpose_guess ?? "unknown"}{f.page_url ? ` · on ${f.page_url.replace(/^https?:\/\//, "")}` : ""}</div>
          {f.fields.map((field, i) => (
            <div key={i} style={{ fontSize: 12, display: "flex", justifyContent: "space-between" }}>
              <span>{field.name}</span>
              <span className="muted">{field.field_type}{field.required ? " *" : ""}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

const POLICY_LABELS: Record<string, string> = {
  privacy_policy: "Privacy Policy",
  cookie_policy: "Cookie Policy",
  terms: "Terms",
  other: "Other",
};

export function PoliciesSection({ policies, status = "loaded" }: { policies: EvidencePolicyItem[]; status?: EvidenceStatus }) {
  if (status !== "loaded") return <EvidenceNotice status={status} what="policy pages" />;
  if (policies.length === 0) return <p className="empty-note">No policy pages were discovered during the crawl.</p>;
  return (
    <ul className="timeline">
      {policies.map((p) => (
        <li key={p.id}>
          <span className="badge info" style={{ marginRight: 8 }}>{POLICY_LABELS[p.policy_type] ?? p.policy_type}</span>
          <a href={p.url} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>{p.url}</a>
        </li>
      ))}
    </ul>
  );
}
