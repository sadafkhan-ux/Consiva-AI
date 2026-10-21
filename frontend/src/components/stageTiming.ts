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

/**
 * How long the pipeline took, by the SERVER's clock: from the first stage's start to the last stage's end (to `nowMs` if a
 * stage is still running). The browser's own elapsed time is not that: it keeps counting while a laptop sleeps or a tab is
 * frozen, which once showed 8,413 s for a pipeline that worked for 21 s. Null when the server sent no timestamps.
 */
export function serverSpanMs(stages: StageResponse[], nowMs: number): number | null {
  const starts = stages.map((s) => (s.started_at ? Date.parse(s.started_at) : NaN)).filter((n) => !Number.isNaN(n));
  if (!starts.length) return null;
  const ends = stages.map((s) => {
    if (s.status === "running" || s.status === "pending") return nowMs;
    return s.completed_at ? Date.parse(s.completed_at) : NaN;
  }).filter((n) => !Number.isNaN(n));
  if (!ends.length) return null;
  return Math.max(0, Math.max(...ends) - Math.min(...starts));
}

/** Stages that have been "running" for longer than `thresholdMs` by the server's start time: probably stuck, not working. */
export function stuckStages(stages: StageResponse[], nowMs: number, thresholdMs = 10 * 60_000): { stage: string; runningMs: number }[] {
  return stages
    .filter((s) => s.status === "running" && s.started_at && !Number.isNaN(Date.parse(s.started_at)))
    .map((s) => ({ stage: s.stage, runningMs: nowMs - Date.parse(s.started_at as string) }))
    .filter((s) => s.runningMs > thresholdMs);
}

/** Time inside the run that no stage accounts for (the job queue's hand-off between steps), or null if unknown. */
export function unaccountedMs(totalMs: number | null, summary: Record<string, StageTiming>): number | null {
  if (totalMs == null) return null;
  const accounted = Object.values(summary).reduce((n, s) => n + (s.durationMs ?? 0), 0);
  return Math.max(0, totalMs - accounted);
}
