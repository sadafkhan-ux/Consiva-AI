import { useCallback, useRef, useState } from "react";
import { api } from "../api/client";
import { ApiError } from "../api/types";
import type {
  AuditLogResponse,
  FindingResponse,
  DpdpScoreResponse,
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

/** How long a poll keeps retrying after the connection to the backend drops before it gives up. */
const NETWORK_GRACE_MS = 120_000;

/** Polls `fn` until `isDone`. `timeoutMs` may be `Infinity`: the scan-status poll has no client-side
 *  ceiling, because the BACKEND bounds a scan (its own safety ceiling, and a bounded number of job
 *  retries) and reports a terminal status when it is over. A request that fails still ends the loop. */
export async function pollUntil<T>(
  fn: () => Promise<T>, isDone: (v: T) => boolean, timeoutMs: number, intervalMs: number,
  networkGraceMs: number = NETWORK_GRACE_MS,
): Promise<T> {
  // Time spent waiting is counted per round, and one round counts for at most a few intervals. A laptop that sleeps,
  // or a tab the browser freezes, therefore does not turn a few minutes of waiting into hours that "time out" the
  // scan the moment it wakes (a real scan showed an 8,413 s duration for a 21 s crawl).
  let waited = 0;
  let outageStart: number | null = null;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const roundStart = Date.now();
    try {
      const result = await fn();
      outageStart = null;
      if (isDone(result)) return result;
    } catch (err) {
      // A dropped connection (wifi blip, sleep/wake, a restarting backend) is not the scan failing. Keep trying for
      // a bounded time; anything else, or a longer outage, ends the poll as before.
      if (!(err instanceof ApiError && err.status === 0)) throw err;
      outageStart ??= roundStart;
      if (Date.now() - outageStart > networkGraceMs) throw err;
    }
    waited += Math.min(Date.now() - roundStart + intervalMs, intervalMs * 5);
    if (waited > timeoutMs) {
      throw new Error("Timed out waiting for the backend -- it may still finish on its own; re-check the scan by re-entering the same URL is not needed, the scan already exists server-side.");
    }
    await sleep(intervalMs);
  }
}

/** True when the scan failed and collected NO pages, so nothing about the site was measured.
 *
 *  This is the case in which "0 cookies", "No trackers were detected" and "No findings" would be
 *  false reassurance: they read as a verified clean result when the truth is "we never looked". A
 *  scan that failed LATER (the analysis, say) after pages were collected is not this case -- its
 *  evidence is real and is still shown. */
export function scanEvidenceUnavailable(
  state: Pick<ConsentScanState, "phase" | "scan" | "stages"> & Partial<Pick<ConsentScanState, "evidence">>,
): boolean {
  if (state.phase !== "failed") return false;
  if ((state.scan?.evidence_counts.pages ?? 0) > 0) {
    // Pages were collected, but if the results could not be fetched (the backend became unreachable) the empty
    // lists below would read "No trackers were detected" over evidence that exists. `evidence` is absent from a
    // caller that does not track it, which keeps the earlier meaning.
    return "evidence" in state && state.evidence === null;
  }
  return true;
}

/** The pipeline stage a failed scan stopped at, for the "not scanned" notice. */
export function failedStageName(stages: StageResponse[]): string | null {
  return stages.find((s) => s.status === "failed")?.stage ?? null;
}

/** Whether the analysis pipeline has reached an end the UI should stop polling at.
 *
 *  A failed `llm_analysis` or `rag_retrieval` is NOT an end: the graph routes either to the rule-derived
 *  findings, which are written a moment later. Stopping at the failure would show "failed" and miss them. A
 *  failure in any other stage is terminal. */
export function analysisStagesAreDone(stages: StageResponse[]): boolean {
  const byName = Object.fromEntries(stages.map((s) => [s.stage, s]));
  return (
    byName.audit_saved?.status === "completed" ||
    byName.audit_saved?.status === "failed" ||
    byName.findings_generated?.status === "completed" ||
    // The fallback's own terminal stage.
    byName.rule_findings_generated?.status === "completed" ||
    stages.some((s) => s.status === "failed" && s.stage !== "llm_analysis" && s.stage !== "rag_retrieval")
  );
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
  /** The proposed DPDP Compliance Score. Null until loaded, and left null if it could not be (the page shows nothing rather
   *  than a made-up number). Optional so state built before it existed keeps working. */
  dpdpScore?: DpdpScoreResponse | null;
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
  dpdpScore: null,
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
    const [findings, audit, evidence, dpdpScore] = await Promise.all([
      api.getFindings(scanId).catch(() => null),
      api.getAudit(scanId).catch(() => null),
      api.getEvidence(scanId).catch(() => null),
      api.getDpdpScore(scanId).catch(() => null),
    ]);
    if (runIdRef.current !== myRunId) return;
    setState((s) => ({
      ...s,
      findings: findings ?? s.findings,
      audit: audit ?? s.audit,
      evidence: evidence ?? s.evidence,
      dpdpScore: dpdpScore ?? s.dpdpScore ?? null,
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
    const [evidence, dpdpScore] = await Promise.all([
      api.getEvidence(scanId).catch(() => null),
      api.getDpdpScore(scanId).catch(() => null),
    ]);
    if (runIdRef.current !== myRunId || !evidence) return;
    setState((s) => ({ ...s, evidence, dpdpScore: dpdpScore ?? s.dpdpScore ?? null }));
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
        analysisStagesAreDone,
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
          // "cancelled" is terminal too: with no client ceiling below, a state that is not listed
          // here would be polled forever.
          (s) => s.status === "completed" || s.status === "failed" || s.status === "cancelled",
          // No client-side ceiling. This used to give up after 600 s with "Timed out waiting for the
          // backend" -- but a slow site's scan (and the queue wait before it) can take longer than that
          // and still finish, and the user was told it had failed while it was still running. The
          // backend decides when a scan is over: it aborts a hung scan at its own ceiling, retries a
          // bounded number of times, and then reports "failed" with the reason -- which is shown.
          Number.POSITIVE_INFINITY,
          1_200
        );
        if (runIdRef.current !== myRunId) return;

        if (finalScan.status === "cancelled") {
          await loadEvidenceRegardless(scan.id, myRunId);
          setState((s) => ({ ...s, phase: "failed", error: "The scan was cancelled.", completedAt: Date.now() }));
          return;
        }

        if (finalScan.status === "failed") {
          // Load whatever evidence the crawl DID persist before giving up. A failed
          // scan still leaves real cookies, trackers, forms and pages behind, and
          // without this the report renders every itemized section as "none
          // detected" -- telling the customer their site is clean when the truth is
          // that the pipeline broke. Measured on a real hubspot.com scan: 840
          // trackers, 124 cookies, 50 forms and 175 policies were in the database
          // while the report said none of them existed.
          // The audit list too: it now carries the `scan.failed` entry (stage and reason), and the
          // Audit Timeline read "No audit events recorded yet" for a scan that had in fact failed.
          await refreshFindingsAuditEvidence(scan.id, myRunId);
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
