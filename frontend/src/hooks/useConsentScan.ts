import { useCallback, useRef, useState } from "react";
import { api } from "../api/client";
import { ApiError } from "../api/types";
import type {
  AuditLogResponse,
  FindingResponse,
  ScanEvidenceResponse,
  ScanStatusResponse,
  StageResponse,
  WebsiteScanMetadata,
} from "../api/types";

// Phases are derived from REAL backend fields (consent_scans.status, agent_run/stage
// status), not invented client-side states -- see deriveFailureReason for how the
// specific failure labels (Page Unreachable / Consent Not Found / CMP Not
// Automatable / Accept Failed / Reject Failed) are derived from real response data.
export type ScanPhase =
  | "idle"
  | "starting"
  | "scanning"
  | "analyzing"
  | "awaiting_review"
  | "completed"
  | "failed";

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollUntil<T>(fn: () => Promise<T>, isDone: (v: T) => boolean, timeoutMs: number, intervalMs: number): Promise<T> {
  const start = Date.now();
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const result = await fn();
    if (isDone(result)) return result;
    if (Date.now() - start > timeoutMs) {
      throw new Error("Timed out waiting for the backend -- it may still finish on its own; re-check the scan by re-entering the same URL is not needed, the scan already exists server-side.");
    }
    await sleep(intervalMs);
  }
}

export function deriveFailureReason(scanError: string | null, meta: WebsiteScanMetadata | null): string | null {
  if (scanError && scanError.includes("Could not reach the site at all")) return "Page Unreachable";
  if (!meta) return null;
  if (meta.accept_interaction === "click_failed") return "Accept Failed";
  if (meta.reject_interaction === "click_failed") return "Reject Failed";
  if (meta.consent_mechanism === "none") return "Consent Not Found";
  if (meta.accept_interaction === "cmp_not_automatable" || meta.reject_interaction === "cmp_not_automatable") return "CMP detected but could not be automated";
  return null;
}

export interface ConsentScanState {
  phase: ScanPhase;
  scanId: string | null;
  scan: ScanStatusResponse | null;
  stages: StageResponse[];
  websiteScanMeta: WebsiteScanMetadata | null;
  findings: FindingResponse[];
  audit: AuditLogResponse[];
  evidence: ScanEvidenceResponse | null;
  error: string | null;
  startedAt: number | null;
  completedAt: number | null;
  durationMs: number | null;
}

const INITIAL_STATE: ConsentScanState = {
  phase: "idle",
  scanId: null,
  scan: null,
  stages: [],
  websiteScanMeta: null,
  findings: [],
  audit: [],
  evidence: null,
  error: null,
  startedAt: null,
  completedAt: null,
  durationMs: null,
};

export function useConsentScan() {
  const [state, setState] = useState<ConsentScanState>(INITIAL_STATE);
  // Guards against a stale poll loop from a PREVIOUS run() call updating state after
  // the user starts a new scan -- React state closures alone won't stop an in-flight
  // while-loop from a prior invocation.
  const runIdRef = useRef(0);

  const refreshFindingsAuditEvidence = useCallback(async (scanId: string, myRunId: number) => {
    const [findings, audit, evidence] = await Promise.all([
      api.getFindings(scanId),
      api.getAudit(scanId),
      api.getEvidence(scanId).catch(() => null),
    ]);
    if (runIdRef.current !== myRunId) return;
    setState((s) => ({ ...s, findings, audit, evidence: evidence ?? s.evidence }));
  }, []);

  const pollStagesUntilAuditOrPending = useCallback(
    async (scanId: string, myRunId: number): Promise<StageResponse[]> => {
      return pollUntil(
        async () => {
          const stages = await api.getStages(scanId);
          if (runIdRef.current !== myRunId) return stages;
          setState((s) => ({ ...s, stages, websiteScanMeta: (stages.find((x) => x.stage === "website_scan")?.metadata as WebsiteScanMetadata) ?? s.websiteScanMeta }));
          return stages;
        },
        (stages) => {
          const byName = Object.fromEntries(stages.map((s) => [s.stage, s]));
          return (
            byName.audit_saved?.status === "completed" ||
            byName.audit_saved?.status === "failed" ||
            byName.findings_generated?.status === "completed" ||
            stages.some((s) => s.status === "failed")
          );
        },
        // 600s: the NVIDIA LLM analysis stage has been directly observed taking ~550s
        // under real, live endpoint degradation (its own retry/backoff riding out
        // repeated timeouts) -- a shorter ceiling would report "timed out" over a
        // pipeline that is actually still working and later succeeds.
        600_000,
        1_500
      );
    },
    []
  );

  const run = useCallback(
    async (url: string, authorized: boolean) => {
      const myRunId = ++runIdRef.current;
      setState({ ...INITIAL_STATE, phase: "starting", startedAt: Date.now() });

      try {
        const scan = await api.createScan(url, authorized);
        if (runIdRef.current !== myRunId) return;
        setState((s) => ({ ...s, scanId: scan.id, phase: "scanning" }));

        const finalScan = await pollUntil(
          async () => {
            const [s, stages] = await Promise.all([api.getScan(scan.id), api.getStages(scan.id)]);
            if (runIdRef.current !== myRunId) return s;
            setState((prev) => ({
              ...prev,
              scan: s,
              stages,
              websiteScanMeta: (stages.find((x) => x.stage === "website_scan")?.metadata as WebsiteScanMetadata) ?? prev.websiteScanMeta,
            }));
            return s;
          },
          (s) => s.status === "completed" || s.status === "failed",
          120_000,
          1_200
        );
        if (runIdRef.current !== myRunId) return;

        if (finalScan.status === "failed") {
          setState((s) => ({ ...s, phase: "failed", error: finalScan.error, completedAt: Date.now() }));
          return;
        }

        setState((s) => ({ ...s, phase: "analyzing" }));
        await api.analyze(scan.id);
        if (runIdRef.current !== myRunId) return;

        const finalStages = await pollStagesUntilAuditOrPending(scan.id, myRunId);
        if (runIdRef.current !== myRunId) return;

        const byName = Object.fromEntries(finalStages.map((s) => [s.stage, s]));
        const failedStage = finalStages.find((s) => s.status === "failed");
        if (failedStage) {
          setState((s) => ({ ...s, phase: "failed", error: `Analysis failed at stage "${failedStage.stage}": ${failedStage.error}`, completedAt: Date.now() }));
          return;
        }

        await refreshFindingsAuditEvidence(scan.id, myRunId);
        if (runIdRef.current !== myRunId) return;

        const stillAwaiting = byName.audit_saved?.status !== "completed" && byName.audit_saved?.status !== "failed";
        setState((s) => ({
          ...s,
          phase: stillAwaiting ? "awaiting_review" : "completed",
          completedAt: Date.now(),
          durationMs: s.startedAt ? Date.now() - s.startedAt : null,
        }));
      } catch (err) {
        if (runIdRef.current !== myRunId) return;
        const message = err instanceof ApiError ? err.detail : err instanceof Error ? err.message : String(err);
        setState((s) => ({ ...s, phase: "failed", error: message, completedAt: Date.now() }));
      }
    },
    [pollStagesUntilAuditOrPending, refreshFindingsAuditEvidence]
  );

  // After a human decision (approve/reject/edit), the LangGraph checkpointer resumes
  // the paused run server-side on its own -- this just keeps polling so the UI
  // reflects audit_saved's completion without a page reload.
  const afterReviewDecision = useCallback(async () => {
    const myRunId = runIdRef.current;
    const scanId = state.scanId;
    if (!scanId) return;
    await refreshFindingsAuditEvidence(scanId, myRunId);
    const stages = await api.getStages(scanId);
    const byName = Object.fromEntries(stages.map((s) => [s.stage, s]));
    if (byName.audit_saved?.status === "completed" || byName.audit_saved?.status === "failed") {
      setState((s) => ({
        ...s,
        stages,
        phase: byName.audit_saved?.status === "failed" ? "failed" : "completed",
        durationMs: s.startedAt ? Date.now() - s.startedAt : null,
      }));
      await refreshFindingsAuditEvidence(scanId, myRunId);
    } else {
      // Still other findings pending -- keep polling in the background.
      pollStagesUntilAuditOrPending(scanId, myRunId)
        .then(async (finalStages) => {
          if (runIdRef.current !== myRunId) return;
          const done = Object.fromEntries(finalStages.map((s) => [s.stage, s]));
          setState((s) => ({
            ...s,
            phase: done.audit_saved?.status === "failed" ? "failed" : "completed",
            durationMs: s.startedAt ? Date.now() - s.startedAt : null,
          }));
          await refreshFindingsAuditEvidence(scanId, myRunId);
        })
        .catch(() => {
          /* still awaiting further review decisions -- not an error */
        });
    }
  }, [state.scanId, refreshFindingsAuditEvidence, pollStagesUntilAuditOrPending]);

  return { state, run, afterReviewDecision };
}
