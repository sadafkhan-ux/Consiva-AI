import type { ScoreResponse } from "../api/types";
import { fromScoreResponse, type TenAssessment } from "../report/tenChecks";
import fixture from "./scoreFixture.json";
import gaps from "./scoreFixtureGaps.json";

/** A real Privacy Readiness Score as the backend returns it (made by backend app/scoring/ten_checks.py from a recorded scan):
 *  5 passed, 4 partially passed, 1 failed. Tests render it instead of computing a score, because the score is the backend's. */
export const SCORE_RESPONSE = fixture as unknown as ScoreResponse;
export const TEN: TenAssessment = fromScoreResponse(SCORE_RESPONSE);

/** A real backend score in which some checks could not be assessed (left out of the score), for the reports' wording about them. */
export const TEN_WITH_GAPS: TenAssessment = fromScoreResponse(gaps as unknown as ScoreResponse);
