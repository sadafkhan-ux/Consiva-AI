import type { ConsentScanState } from "../hooks/useConsentScan";
import { ANALYSIS_INCOMPLETE_NOTE, buildReportModel, type ReportModel } from "./buildReport";
import { buildRequirementsView, isRequirements } from "./requirements";
import { buildTiming } from "./scanTransparency";

/** The report's data model from what the dashboard (or the report page) holds for one scan. One function, so the PDF button, the
 *  PDF the server prints and the web report all start from the very same values. */
export function reportModelFromState(state: ConsentScanState): ReportModel {
  const analysisNote = state.phase === "degraded" ? ANALYSIS_INCOMPLETE_NOTE : null;
  return buildReportModel(
    state.scan, state.findings, state.websiteScanMeta, state.evidence?.cookies ?? [], state.evidence?.trackers ?? [],
    isRequirements(state.requirements) ? buildRequirementsView(state.requirements) : null,
    state.evidence,
    analysisNote,
    state.policyAnalysis ?? null,
    state.screenshot ?? null,
    buildTiming(state.stages),
    state.assessment ?? null,
  );
}
