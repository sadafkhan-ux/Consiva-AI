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
  // Findings exist, but the analysis step that explains them did not finish. This
  // is NOT "failed": on a real hubspot.com scan the rules produced three findings --
  // including 54 marketing scripts still firing after the visitor pressed Reject --
  // and the page reported "Failed" with an empty findings list, because a failed
  // llm_analysis stage was treated as a failed scan. Nor is it "completed", which
  // would imply the findings carry the narrative and citations they normally do.
  | "degraded"
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
  /** Which pipeline stage failed, when one did. The status panel showed "—" for
   *  "Current Stage" while the failing stage was known and displayed further down
   *  the same page. */
  failedStage: string | null;
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
  failedStage: null,
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
    // Each settles on its own. Previously `Promise.all` with only the evidence call
    // guarded meant one failing request discarded the other two results as well --
    // so a 500 on findings also blanked the evidence that had loaded fine.
    const [findings, audit, evidence] = await Promise.all([
      api.getFindings(scanId).catch(() => null),
      api.getAudit(scanId).catch(() => null),
      api.getEvidence(scanId).catch(() => null),
    ]);
    if (runIdRef.current !== myRunId) return;
    setState((s) => ({
      ...s,
      findings: findings ?? s.findings,
      audit: audit ?? s.audit,
      evidence: evidence ?? s.evidence,
    }));
  }, []);

  /** Evidence only, for the failure paths.
   *
   *  The crawl and the analysis are separate things that fail separately. When
   *  analysis dies, everything the crawl collected is still in the database, and a
   *  report that renders "no trackers were detected" over 840 stored trackers is
   *  worse than one that renders nothing at all -- it is a confident false negative
   *  in a product whose whole job is to find what a site is doing. */
  const loadEvidenceRegardless = useCallback(async (scanId: string, myRunId: number) => {
    const evidence = await api.getEvidence(scanId).catch(() => null);
    if (runIdRef.current !== myRunId || !evidence) return;
    setState((s) => ({ ...s, evidence }));
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
            // The fallback's own terminal stage. A failed llm_analysis no longer
            // ends the run -- the graph routes to create_rule_findings, which
            // writes findings and then this stage.
            byName.rule_findings_generated?.status === "completed" ||
            // Deliberately NOT "any stage failed". That condition stopped polling
            // the instant llm_analysis was marked failed, which is the moment
            // BEFORE the fallback runs -- so the UI gave up a second early and
            // never saw the findings that appeared right after. Only llm_analysis
            // has a downstream recovery path; a failure anywhere else is terminal
            // and should still stop the poll immediately.
            stages.some((s) => s.status === "failed" && s.stage !== "llm_analysis")
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
          // 600s, matching the analysis ceiling below -- 120s here was less than a
          // single observed website_scan, so this loop reported "timed out" over
          // scans that were still running and did complete. Two real measurements
          // from one session: website_scan alone took 120s and 78s, and because the
          // worker takes one job per poll (app/jobs/worker.py dequeue_one, single
          // replica) a scan started while another is mid-Chromium also waits out the
          // first -- 73s of queue time was observed, for 151s end-to-end against the
          // old 120s budget. The queue wait scales with whatever else is in flight,
          // so the ceiling has to cover queue time + scan, not just scan.
          600_000,
          1_200
        );
        if (runIdRef.current !== myRunId) return;

        if (finalScan.status === "failed") {
          // Load whatever evidence the crawl DID persist before giving up. A failed
          // scan still leaves real cookies, trackers, forms and pages behind, and
          // without this the report renders every itemized section as "none
          // detected" -- telling the customer their site is clean when the truth is
          // that the pipeline broke. Measured on a real hubspot.com scan: 840
          // trackers, 124 cookies, 50 forms and 175 policies were in the database
          // while the report said none of them existed.
          await loadEvidenceRegardless(scan.id, myRunId);
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
          // Fetch findings too, not just evidence.
          //
          // This path used to call loadEvidenceRegardless (evidence ONLY) and
          // return. On scan 63497fc6 the rules had already written three findings
          // to the database -- 297 trackers firing before any consent interaction,
          // 54 still firing after Reject -- and the report showed none of them,
          // because nothing on this path ever asked for them. The crawl evidence
          // appeared, the findings list stayed empty, and the banner said "Failed".
          //
          // A failed llm_analysis costs the narrative, not the findings; the page
          // has to reflect that distinction rather than collapsing both into
          // "Failed" and showing nothing.
          await refreshFindingsAuditEvidence(scan.id, myRunId);
          if (runIdRef.current !== myRunId) return;
          const reason = `Analysis failed at stage "${failedStage.stage}": ${failedStage.error}`;
          setState((s) => ({
            ...s,
            // Degraded only when the fallback actually produced something. If it
            // produced nothing there is genuinely nothing to show and "failed" is
            // the honest label -- this must not dress an empty result up as a
            // partial success.
            phase: s.findings.length > 0 ? "degraded" : "failed",
            error: reason,
            failedStage: failedStage.stage,
            completedAt: Date.now(),
            durationMs: s.startedAt ? Date.now() - s.startedAt : null,
          }));
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
    [pollStagesUntilAuditOrPending, refreshFindingsAuditEvidence, loadEvidenceRegardless]
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
