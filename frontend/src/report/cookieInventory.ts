import type { CookieObservation, EvidenceCookieItem } from "../api/types";

/** The cookie inventory shown in the Cookies section and appended to the PDF. One
 *  derivation for both, so the screen and the download can never disagree.
 *
 *  Nothing here is inferred. A value the scan did not record is the literal
 *  NOT_AVAILABLE, never a plausible default. */
export const NOT_AVAILABLE = "Not available";

const STATE_ORDER = ["pre_consent", "post_accept", "post_reject"] as const;

const STATE_LABELS: Record<string, string> = {
  pre_consent: "Pre-Consent",
  post_accept: "Post-Accept",
  post_reject: "Post-Reject",
};

export function stateLabel(state: string): string {
  return STATE_LABELS[state] ?? state;
}

const METHOD_LABELS: Record<string, string> = {
  set_cookie_header: "Set-Cookie response header",
  single_page_load: "Cookie jar read after loading only this page",
  concurrent_batch: "Cookie jar read after several pages loaded at once",
};

export interface CookieSighting {
  state: string;
  stateLabel: string;
  page: string;
  /** Pages that could have set it, when `page` is NOT_AVAILABLE for that reason. */
  candidatePages: string[];
  detectedAt: string;
  how: string;
  sourceRequest: string | null;
}

export interface CookieRow {
  id: string;
  name: string;
  domain: string;
  party: string;
  category: string;
  vendor: string;
  states: string[];
  sightings: CookieSighting[];
  expiry: string;
  path: string;
  secure: string;
  httpOnly: string;
  sameSite: string;
  /** Lower-cased text the search box matches against. */
  searchText: string;
}

export interface GroupCount {
  label: string;
  count: number;
}

export interface CookieInventory {
  total: number;
  byState: GroupCount[];
  byCategory: GroupCount[];
  byDomain: GroupCount[];
  rows: CookieRow[];
}

function yesNo(value: boolean | null | undefined): string {
  if (value === true) return "Yes";
  if (value === false) return "No";
  return NOT_AVAILABLE;
}

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return NOT_AVAILABLE;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return NOT_AVAILABLE;
  // Explicit UTC, so the PDF reads the same wherever it is opened.
  return `${d.toISOString().replace("T", " ").slice(0, 19)} UTC`;
}

/** The expiry date, with the lifetime the browser computed from it (Max-Age is turned
 *  into an absolute expiry by the browser, so this is the only form the scan has). */
function formatExpiry(cookie: EvidenceCookieItem): string {
  if (cookie.expiry === undefined) return NOT_AVAILABLE;
  // The scanner stores no expiry exactly when the browser reported a session cookie
  // (backend scanner/cookie_detector.py).
  if (cookie.expiry === null) return "Session (deleted when the browser closes)";
  const expires = new Date(cookie.expiry);
  if (Number.isNaN(expires.getTime())) return NOT_AVAILABLE;
  const seen = (cookie.observations ?? [])
    .map((o) => (o.observed_at ? new Date(o.observed_at).getTime() : NaN))
    .filter((t) => !Number.isNaN(t))
    .sort((a, b) => a - b)[0];
  const date = formatTimestamp(cookie.expiry);
  if (seen === undefined) return date;
  const days = Math.round((expires.getTime() - seen) / 86_400_000);
  return `${date} (about ${days} day${days === 1 ? "" : "s"} after it was seen)`;
}

function sighting(o: CookieObservation): CookieSighting {
  return {
    state: o.consent_state,
    stateLabel: stateLabel(o.consent_state),
    page: o.page_url ?? NOT_AVAILABLE,
    candidatePages: o.page_url ? [] : o.candidate_page_urls ?? [],
    detectedAt: formatTimestamp(o.observed_at),
    how: METHOD_LABELS[o.method] ?? o.method,
    sourceRequest: o.source_request_url,
  };
}

function rowFor(cookie: EvidenceCookieItem): CookieRow {
  const observations = cookie.observations ?? [];
  const sightings = observations.map(sighting);
  // A state the cookie was seen in but which has no observation (a scan from before
  // observations were recorded) still gets a line, saying the page is unknown.
  for (const state of cookie.consent_states) {
    if (!observations.some((o) => o.consent_state === state)) {
      sightings.push({
        state, stateLabel: stateLabel(state), page: NOT_AVAILABLE, candidatePages: [],
        detectedAt: NOT_AVAILABLE, how: NOT_AVAILABLE, sourceRequest: null,
      });
    }
  }
  sightings.sort((a, b) => stateRank(a.state) - stateRank(b.state));

  const row: Omit<CookieRow, "searchText"> = {
    id: cookie.id,
    name: cookie.name,
    domain: cookie.domain ?? NOT_AVAILABLE,
    party: cookie.is_first_party == null ? NOT_AVAILABLE : cookie.is_first_party ? "First-party" : "Third-party",
    // "Unclassified" is the classifier's actual result, not a missing value.
    category: cookie.category ?? "Unclassified",
    vendor: cookie.vendor ?? NOT_AVAILABLE,
    states: [...cookie.consent_states].sort((a, b) => stateRank(a) - stateRank(b)),
    sightings,
    expiry: formatExpiry(cookie),
    path: cookie.path ?? NOT_AVAILABLE,
    secure: yesNo(cookie.secure),
    httpOnly: yesNo(cookie.http_only),
    sameSite: cookie.same_site ?? NOT_AVAILABLE,
  };
  return {
    ...row,
    searchText: [row.name, row.domain, row.category, row.vendor, ...sightings.map((s) => s.page)]
      .join(" ")
      .toLowerCase(),
  };
}

function stateRank(state: string): number {
  const i = STATE_ORDER.indexOf(state as (typeof STATE_ORDER)[number]);
  return i === -1 ? STATE_ORDER.length : i;
}

function countBy(rows: CookieRow[], keys: (row: CookieRow) => string[]): GroupCount[] {
  const counts = new Map<string, number>();
  for (const row of rows) {
    for (const key of new Set(keys(row))) counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([label, count]) => ({ label, count }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

export function buildCookieInventory(cookies: EvidenceCookieItem[]): CookieInventory {
  const rows = cookies.map(rowFor).sort(
    (a, b) => stateRank(a.states[0] ?? "") - stateRank(b.states[0] ?? "") || a.name.localeCompare(b.name),
  );
  return {
    // Rows are already one per cookie: the scanner merges the three passes on
    // name + domain, so a cookie seen in several states is still one row.
    total: rows.length,
    // A cookie seen in two states counts in both -- these are "present in", not a split.
    byState: STATE_ORDER.map((state) => ({
      label: stateLabel(state),
      count: rows.filter((r) => r.states.includes(state)).length,
    })),
    byCategory: countBy(rows, (r) => [r.category]),
    byDomain: countBy(rows, (r) => [r.domain]),
    rows,
  };
}
