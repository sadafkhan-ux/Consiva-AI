import raw from "./assessmentFixtures.json";
import { formatScore } from "../report/scoreFormat";
import { fromApiAssessment, type TenAssessment } from "../report/tenChecks";

/**
 * Assessments as the server serves them, for tests of what the app draws from one. Each is the recorded output of the server engine (backend app/scoring/ten_checks.py) for a scenario,
 * so a test of the screen runs on a real result and never on a score worked out in the test.
 *   allPassed 100/100 · typical 90/100 (8 passed, 2 partial) · mixed 75/100 (6 passed, 3 partial, 1 failed) · someFailed 60/100 · policyMissing 75/100 on 3 / 4 (6 not assessed)
 *   noScore (nothing could be assessed) · withNotApplicable 94.4/100 on 8.5 / 9 · partiallyAssessed 94.4/100 (one check not assessed)
 */
export const A = Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, fromApiAssessment(v)])) as Record<keyof typeof raw, TenAssessment>;

/** The same assessment with another score, to test how a figure is drawn at that value. */
export const withScore = (score: number | null, base: TenAssessment = A.typical): TenAssessment => ({ ...base, score, scoreText: formatScore(score) });
