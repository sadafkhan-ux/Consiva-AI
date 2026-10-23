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

export function stateLabel(state: string, untested: readonly string[] = []): string {
  const label = STATE_LABELS[state] ?? state;
  // The scanner revisits the page for the Accept and Reject passes even when it found nothing to click. What it records
  // then is the same page seen again, not what happens after a choice, and must not be presented as if it were.
  return untested.includes(state) ? `${label} (no choice was made — not tested)` : label;
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

/** What a cookie did in each consent state. "Not tested" where no choice was made in that pass, so it cannot be read as "not set". */
export type StateBehaviour = "Set" | "Not seen" | "Not tested";

export interface CookieRow {
  id: string;
  name: string;
  domain: string;
  party: string;
  category: string;
  vendor: string;
  /** Who provides it: the recognised provider, else the classified vendor, else "Not identified". */
  provider: string;
  /** What it is for, in one sentence, and how that is known: from the provider's description of a recognised cookie, from its classification only, or not known. */
  purpose: string;
  purposeSource: "catalogue" | "category" | "unknown";
  /** "Session", "29 minutes", "12 months", or the expiry date when the time it was first seen was not recorded. */
  lifetime: string;
  isSession: boolean;
  /** Lives more than a year after it was set. Informational: the Act and Rules set no cookie lifetime. */
  longLived: boolean;
  /** What it did before any choice, after Accept and after Reject. */
  behaviour: { before: StateBehaviour; accept: StateBehaviour; reject: StateBehaviour };
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

/** The headline counts of the inventory: each is a count of cookies, from the same rows as the table. */
export interface CookieSummary {
  found: number;
  thirdParty: number;
  beforeConsent: number;
  longLived: number;
  /** Cookies seen after Reject; null when no choice was made, so the pass says nothing about Reject. */
  afterReject: number | null;
}

export interface CookieGroup {
  /** "Necessary and functional", "Analytics", "Advertising and marketing" or "Not classified". */
  title: string;
  note: string;
  rows: CookieRow[];
}

export interface CookieInventory {
  total: number;
  summary: CookieSummary;
  groups: CookieGroup[];
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

/** "29 minutes", "3 days", "12 months", "2 years": the lifetime in the unit a person would say it in. */
export function humanDuration(days: number): string {
  const unit = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;
  if (days < 1 / 24) return unit(Math.max(1, Math.round(days * 1440)), "minute");
  if (days < 1) return unit(Math.round(days * 24), "hour");
  if (days < 60) return unit(Math.round(days), "day");
  if (days < 730) return unit(Math.round(days / 30.4), "month");
  return unit(Math.round(days / 365), "year");
}

function behaviourOf(cookie: EvidenceCookieItem, state: string, untested: readonly string[]): StateBehaviour {
  if (cookie.consent_states.includes(state)) return "Set";
  return untested.includes(state) ? "Not tested" : "Not seen";
}

const LONG_LIVED_DAYS = 365;

function sighting(o: CookieObservation, untested: readonly string[]): CookieSighting {
  return {
    state: o.consent_state,
    stateLabel: stateLabel(o.consent_state, untested),
    page: o.page_url ?? NOT_AVAILABLE,
    candidatePages: o.page_url ? [] : o.candidate_page_urls ?? [],
    detectedAt: formatTimestamp(o.observed_at),
    how: METHOD_LABELS[o.method] ?? o.method,
    sourceRequest: o.source_request_url,
  };
}

function rowFor(cookie: EvidenceCookieItem, untested: readonly string[]): CookieRow {
  const observations = cookie.observations ?? [];
  const sightings = observations.map((o) => sighting(o, untested));
  // A state the cookie was seen in but which has no observation (a scan from before
  // observations were recorded) still gets a line, saying the page is unknown.
  for (const state of cookie.consent_states) {
    if (!observations.some((o) => o.consent_state === state)) {
      sightings.push({
        state, stateLabel: stateLabel(state, untested), page: NOT_AVAILABLE, candidatePages: [],
        detectedAt: NOT_AVAILABLE, how: NOT_AVAILABLE, sourceRequest: null,
      });
    }
  }
  sightings.sort((a, b) => stateRank(a.state) - stateRank(b.state));

  const isSession = cookie.is_session ?? (cookie.expiry === null);
  const days = cookie.lifetime_days ?? null;
  const row: Omit<CookieRow, "searchText"> = {
    provider: cookie.provider ?? cookie.vendor ?? "Not identified",
    purpose: cookie.purpose ?? "The purpose is not recorded for this scan.",
    purposeSource: cookie.purpose_source === "catalogue" || cookie.purpose_source === "category" ? cookie.purpose_source : "unknown",
    isSession,
    lifetime: isSession ? "Session" : days !== null ? humanDuration(days) : cookie.expiry ? formatTimestamp(cookie.expiry).slice(0, 10) : NOT_AVAILABLE,
    longLived: !isSession && days !== null && days > LONG_LIVED_DAYS,
    behaviour: { before: behaviourOf(cookie, "pre_consent", untested), accept: behaviourOf(cookie, "post_accept", untested), reject: behaviourOf(cookie, "post_reject", untested) },
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
    searchText: [row.name, row.domain, row.category, row.vendor, row.provider, row.purpose, ...sightings.map((s) => s.page)]
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

export function buildCookieInventory(cookies: EvidenceCookieItem[], untestedStates: readonly string[] = []): CookieInventory {
  const rows = cookies.map((c) => rowFor(c, untestedStates)).sort(
    (a, b) => stateRank(a.states[0] ?? "") - stateRank(b.states[0] ?? "") || a.name.localeCompare(b.name),
  );
  const groupOf = (r: CookieRow): string =>
    r.category === "functional" || r.category === "necessary" ? "Necessary and functional" : r.category === "analytics" ? "Analytics" : r.category === "marketing" ? "Advertising and marketing" : "Not classified";
  const GROUPS: [string, string][] = [
    ["Necessary and functional", "Cookies the site or a service it uses needs in order to work, or to remember a choice such as consent or language."],
    ["Analytics", "Cookies that count visitors and show how a site is used."],
    ["Advertising and marketing", "Cookies that measure advertising or build a profile of the visitor across sites."],
    ["Not classified", "Cookies the scan could not place in a category."],
  ];
  const rejectTested = !untestedStates.includes("post_reject");
  return {
    summary: {
      found: rows.length,
      thirdParty: rows.filter((r) => r.party === "Third-party").length,
      beforeConsent: rows.filter((r) => r.behaviour.before === "Set").length,
      longLived: rows.filter((r) => r.longLived).length,
      afterReject: rejectTested ? rows.filter((r) => r.behaviour.reject === "Set").length : null,
    },
    groups: GROUPS.map(([title, note]) => ({ title, note, rows: rows.filter((r) => groupOf(r) === title) })).filter((g) => g.rows.length > 0),
    // Rows are already one per cookie: the scanner merges the three passes on
    // name + domain, so a cookie seen in several states is still one row.
    total: rows.length,
    // A cookie seen in two states counts in both -- these are "present in", not a split.
    byState: STATE_ORDER.map((state) => ({
      label: stateLabel(state, untestedStates),
      count: rows.filter((r) => r.states.includes(state)).length,
    })),
    byCategory: countBy(rows, (r) => [r.category]),
    byDomain: countBy(rows, (r) => [r.domain]),
    rows,
  };
}
