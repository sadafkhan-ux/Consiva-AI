import { useState } from "react";
import { api } from "../api/client";
import { ApiError } from "../api/types";
import type { FindingResponse } from "../api/types";
import { RiskBadge, StatusBadge } from "./shared";

type ReviewAction = "approve" | "reject" | "edit";

export function FindingsSection({
  findings,
  onDecision,
}: {
  findings: FindingResponse[];
  onDecision: () => Promise<void>;
}) {
  const [openReview, setOpenReview] = useState<{ id: string; action: ReviewAction } | null>(null);
  const [reason, setReason] = useState("");
  const [editedRisk, setEditedRisk] = useState("low");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  if (findings.length === 0) {
    return <p className="empty-note">No findings were generated for this scan — either nothing triggered a rule, or the analysis is still running.</p>;
  }

  function openForm(finding: FindingResponse, action: ReviewAction) {
    setOpenReview({ id: finding.id, action });
    setReason("");
    setEditedRisk(finding.risk_level);
    setFormError(null);
  }

  async function submit(finding: FindingResponse) {
    if (!openReview) return;
    // Mirrors the backend's own validation exactly (never bypassed, only
    // pre-checked): reject/edit always require a reason; approve requires one only
    // for a high-risk finding (see backend/app/services/review_service.py).
    const reasonRequired = openReview.action !== "approve" || finding.risk_level === "high";
    const trimmed = reason.trim();
    if (reasonRequired && !trimmed) {
      setFormError("A reason is required for this decision.");
      return;
    }
    setSubmitting(true);
    setFormError(null);
    try {
      if (openReview.action === "approve") {
        await api.approveFinding(finding.id, trimmed || null);
      } else if (openReview.action === "reject") {
        await api.rejectFinding(finding.id, trimmed);
      } else {
        await api.editFinding(finding.id, { risk_level: editedRisk }, trimmed);
      }
      setOpenReview(null);
      await onDecision();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.detail : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      {findings.map((f) => (
        <div className="finding-card" key={f.id}>
          <div className="finding-head">
            <div className="badges">
              <RiskBadge level={f.risk_level} />
              <span className="badge neutral">{f.priority} priority</span>
              <StatusBadge status={f.status} />
            </div>
            <div className="muted" style={{ fontSize: 12 }}>Rule evidence category: {f.category}</div>
          </div>

          <div className="finding-text">{f.finding_text}</div>

          <div className="finding-block">
            <div className="label">Evidence</div>
            <div className="finding-meta">
              {f.evidence.length ? f.evidence.map((e) => <code key={e}>{e.slice(0, 8)}</code>) : "none referenced"}
            </div>
          </div>

          <div className="finding-block">
            <div className="label">DPDP Reference</div>
            <div className="finding-meta">
              {f.dpdp_reference.length
                ? f.dpdp_reference.map((r, i) => (
                    <div key={i}><code>{r.source_doc}</code>{r.section ? ` — ${r.section}` : ""}</div>
                  ))
                : "none supplied"}
            </div>
          </div>

          <div className="finding-block">
            <div className="label">Recommendation</div>
            <div className="finding-meta">{f.recommendations.map((r) => r.recommendation_text).join("; ") || "—"}</div>
          </div>

          {f.status === "pending" && (
            <>
              {openReview?.id !== f.id ? (
                <div className="finding-actions">
                  <button className="approve small" onClick={() => openForm(f, "approve")}>Approve</button>
                  <button className="reject small" onClick={() => openForm(f, "reject")}>Reject</button>
                  <button className="secondary small" onClick={() => openForm(f, "edit")}>Edit</button>
                </div>
              ) : (
                <div className="review-form">
                  {openReview.action === "edit" && (
                    <>
                      <label htmlFor={`risk-${f.id}`}>New risk level</label>
                      <select id={`risk-${f.id}`} value={editedRisk} onChange={(e) => setEditedRisk(e.target.value)}>
                        <option value="low">low</option>
                        <option value="medium">medium</option>
                        <option value="high">high</option>
                      </select>
                    </>
                  )}
                  <label htmlFor={`reason-${f.id}`}>
                    Reason {(openReview.action !== "approve" || f.risk_level === "high") ? "(required)" : "(optional)"}
                  </label>
                  <input id={`reason-${f.id}`} type="text" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why?" />
                  {formError && <div className="error-box">{formError}</div>}
                  <div className="row-actions">
                    <button
                      className={openReview.action === "approve" ? "approve" : openReview.action === "reject" ? "reject" : "secondary"}
                      disabled={submitting}
                      onClick={() => submit(f)}
                    >
                      {submitting ? "Submitting…" : openReview.action === "approve" ? "Confirm approve" : openReview.action === "reject" ? "Confirm reject" : "Save edit"}
                    </button>
                    <button className="secondary" disabled={submitting} onClick={() => setOpenReview(null)}>Cancel</button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      ))}
    </div>
  );
}
