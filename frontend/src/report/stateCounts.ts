import type { EvidenceCookieItem, EvidenceTrackerItem } from "../api/types";

/**
 * The ONE way cookies and third-party requests are counted per consent state, used by the dashboard's three-state cards, the
 * DPDP score's evidence summary (backend scoring/review.py counts the same way) and the PDF report, so the same scan can never
 * show different numbers in different places.
 *
 * Unit: one stored evidence row each. A third-party request is one distinct address (the query string removed, so a beacon that
 * changes its parameters on every load is one row); a cookie is one name + domain + path. A row seen in more than one state is
 * counted in each of them, so the three columns are "present in this state", not a split of the total.
 *
 * The backend's own `trackers_by_consent_state` / `cookies_by_consent_state` count distinct analytics and marketing VENDORS only
 * (scan_service.py). That is a different unit and is not used for display; it remains in the API for its existing consumers.
 */
export const CONSENT_STATES = ["pre_consent", "post_accept", "post_reject"] as const;
export type ConsentStateKey = (typeof CONSENT_STATES)[number];

export type PerState = Record<ConsentStateKey, number>;

export interface StateCounts {
  /** Cookies present in each state. */
  cookies: PerState;
  /** Third-party requests (distinct addresses) in each state. */
  requests: PerState;
  /** Of those, the requests matched to a known analytics or marketing service. */
  knownRequests: PerState;
}

const inState = (rows: { consent_states: string[] }[], key: ConsentStateKey): number =>
  rows.filter((r) => r.consent_states.includes(key)).length;

const perState = (rows: { consent_states: string[] }[]): PerState => ({
  pre_consent: inState(rows, "pre_consent"),
  post_accept: inState(rows, "post_accept"),
  post_reject: inState(rows, "post_reject"),
});

export const isKnownTracker = (t: Pick<EvidenceTrackerItem, "category">): boolean =>
  t.category === "analytics" || t.category === "marketing";

export function countByState(cookies: EvidenceCookieItem[], trackers: EvidenceTrackerItem[]): StateCounts {
  return {
    cookies: perState(cookies),
    requests: perState(trackers),
    knownRequests: perState(trackers.filter(isKnownTracker)),
  };
}

/** What a "third-party request" is, in one sentence, for every place that shows the number. */
export const REQUEST_UNIT_NOTE =
  "A third-party request is one distinct address the pages contacted, counted once however many times it was requested. " +
  "It is counted in every consent state it appeared in.";
