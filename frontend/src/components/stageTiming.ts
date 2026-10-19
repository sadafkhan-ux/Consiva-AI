import type { StageResponse } from "../api/types";

export interface StageTiming {
  status: string;
  durationMs: number | null;
  attempts: number;
}

/**
 * One entry per stage NAME. A stage can run more than once -- a failed output validation sends the analysis
 * back for a second attempt, so `llm_analysis` has two rows -- and showing only the last row hid the first
 * attempt's time (14.1s on scan ac537cef, where the page showed 3.3s). Durations are summed, the status is
 * the latest attempt's, and `attempts` says how many ran.
 */
export function summariseStages(stages: StageResponse[]): Record<string, StageTiming> {
  const out: Record<string, StageTiming> = {};
  for (const s of stages) {
    const prev = out[s.stage];
    const duration = s.duration_ms == null ? null : s.duration_ms + (prev?.durationMs ?? 0);
    out[s.stage] = {
      status: s.status,
      durationMs: duration ?? prev?.durationMs ?? null,
      attempts: (prev?.attempts ?? 0) + 1,
    };
  }
  return out;
}

/** Time inside the run that no stage accounts for (the job queue's hand-off between steps), or null if unknown. */
export function unaccountedMs(totalMs: number | null, summary: Record<string, StageTiming>): number | null {
  if (totalMs == null) return null;
  const accounted = Object.values(summary).reduce((n, s) => n + (s.durationMs ?? 0), 0);
  return Math.max(0, totalMs - accounted);
}
