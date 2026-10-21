import type { EvidenceTrackerItem } from "../api/types";

const STATES = ["pre_consent", "post_accept", "post_reject"] as const;
type State = (typeof STATES)[number];

// Second-level public suffixes whose registrable domain needs three labels (bbc.co.uk, not co.uk).
const SECOND_LEVEL = new Set(["co", "com", "org", "net", "gov", "ac", "edu"]);

/** The domain an organisation registers: "cdn.example.com" -> "example.com", "x.bbc.co.uk" -> "bbc.co.uk". */
export function registrableDomain(url: string): string {
  let host: string;
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    return url;
  }
  const labels = host.split(".");
  if (labels.length <= 2) return host;
  const take = SECOND_LEVEL.has(labels[labels.length - 2]) && labels[labels.length - 1].length === 2 ? 3 : 2;
  return labels.slice(-take).join(".");
}

export interface TrackerStateSummary {
  /** Requests to a vendor the catalogue knows to be analytics or advertising. */
  analyticsAdRequests: number;
  /** How many distinct named vendors those requests belong to. */
  analyticsAdVendors: number;
  /** Requests to something the catalogue does not know. Not a violation: a person has to decide what they are. */
  unclassifiedRequests: number;
  unclassifiedDomains: number;
  /** Everything else: fonts, the consent tool's own script, static assets. */
  otherRequests: number;
}

export type TrackerSummary = Record<State, TrackerStateSummary>;

/**
 * What the raw "N trackers" figure is made of, per consent state. That figure counts every third-party request -- the
 * consent tool's own script, web fonts, images -- so quoting it as "trackers fired before consent" overstated the
 * problem (a scan of bbc.com: 424 requests before consent, 22 of them named analytics or advertising) and, on sites the
 * catalogue knows little about, understated it (most of the 424 are unknown, and need review rather than dismissal).
 */
export function summariseTrackers(trackers: EvidenceTrackerItem[]): TrackerSummary {
  const out = {} as TrackerSummary;
  for (const state of STATES) {
    const vendors = new Set<string>();
    const unknownDomains = new Set<string>();
    let adRequests = 0;
    let unknown = 0;
    let other = 0;
    for (const t of trackers) {
      if (!t.consent_states.includes(state)) continue;
      if (t.category === "analytics" || t.category === "marketing") {
        adRequests += 1;
        vendors.add(t.vendor ?? registrableDomain(t.script_src));
      } else if (t.category === null || t.category === undefined) {
        unknown += 1;
        unknownDomains.add(registrableDomain(t.script_src));
      } else {
        other += 1;
      }
    }
    out[state] = {
      analyticsAdRequests: adRequests, analyticsAdVendors: vendors.size,
      unclassifiedRequests: unknown, unclassifiedDomains: unknownDomains.size, otherRequests: other,
    };
  }
  return out;
}
