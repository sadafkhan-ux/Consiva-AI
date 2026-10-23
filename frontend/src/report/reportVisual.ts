import type { EvidenceCookieItem, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import type { ScoreView } from "./dpdpScore";
import { countByState, isKnownTracker } from "./stateCounts";
import { registrableDomain, summariseTrackers } from "./trackerSummary";

/**
 * Everything the visual two-page report draws from the scan itself: counts per consent state, the technologies seen, and which
 * privacy elements were found. Pure and side-effect free. Each figure is counted from the stored evidence and nothing is filled
 * in: a consent state the scan could not exercise is reported as "Not tested", and an element with no evidence is "Could not verify".
 */

export type ElementStatus = "found" | "not_found" | "unverified" | "not_assessed";

export interface ElementView {
  title: string;
  status: ElementStatus;
  note: string;
}

export interface TechnologyRow {
  name: string;
  type: string;
  requests: number;
  seen: string;
}

export interface ConsentComparison {
  /** True when that pass actually clicked a choice. An untested pass shows "Not tested", never zeros. */
  acceptTested: boolean;
  rejectTested: boolean;
  /** [before consent, after Accept, after Reject] */
  cookies: [number, number, number];
  known: [number, number, number];
  all: [number, number, number];
  banner: [string, string, string];
  takeaway: string;
}

export interface ReportVisual {
  pagesScanned: number;
  pageLimit: number | null;
  /** Every distinct page of the entered domain the scan saw a link or sitemap entry for. Null when the scan did not record it. */
  pagesFound: number | null;
  statesTested: number;
  requests: number;
  knownRequests: number;
  cookies: number;
  domains: number;
  consent: ConsentComparison;
  technologies: { rows: TechnologyRow[]; more: number };
  elements: ElementView[];
}

const STATES = ["pre_consent", "post_accept", "post_reject"] as const;
export const MAX_TECH_ROWS = 8;

const STATE_WORD: Record<string, string> = {
  pre_consent: "Before consent",
  post_accept: "After Accept",
  post_reject: "After Reject",
};

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function seenIn(states: Set<string>): string {
  if (STATES.every((s) => states.has(s))) return "All three states";
  return STATES.filter((s) => states.has(s)).map((s) => STATE_WORD[s]).join(", ") || "Not recorded";
}

function pathOf(url: string): string {
  try {
    const u = new URL(url);
    return `${u.hostname.replace(/^www\./, "")}${u.pathname === "/" ? "" : u.pathname}`;
  } catch {
    return url;
  }
}

function takeaway(c: Omit<ConsentComparison, "takeaway">, otherBefore: number): string {
  const [preK, accK, rejK] = c.known;
  const [preC, accC, rejC] = c.cookies;
  const notTested = [!c.acceptTested ? "Accept" : null, !c.rejectTested ? "Reject" : null].filter(Boolean).join(" and ");
  const lead = notTested
    ? `The ${notTested} choice could not be tested, so only what can be compared is stated here. `
    : "";
  const before = preK === 0 && preC === 0;
  let main: string;
  if (c.acceptTested && c.rejectTested && before && rejK === 0 && rejC === 0) {
    main = `No known analytics or marketing tracker and no cookie appeared before consent or after Reject. After Accept: ${plural(accK, "analytics or marketing request", "analytics or marketing requests")} and ${plural(accC, "cookie", "cookies")}.`;
  } else if (before) {
    main = "No known analytics or marketing tracker and no cookie appeared before consent.";
    if (c.rejectTested) {
      main += rejK === 0 && rejC === 0
        ? " None appeared after Reject either."
        : ` After Reject: ${plural(rejK, "analytics or marketing request", "analytics or marketing requests")} and ${plural(rejC, "cookie", "cookies")}, which a person should check.`;
    }
  } else {
    main = `Before any choice was made: ${plural(preK, "analytics or marketing request", "analytics or marketing requests")} and ${plural(preC, "cookie", "cookies")}. These are the first things to review.`;
    if (c.rejectTested && (rejK > 0 || rejC > 0)) {
      main += ` After Reject: ${plural(rejK, "request", "requests")} and ${plural(rejC, "cookie", "cookies")}.`;
    }
  }
  const rest = otherBefore > 0
    ? ` ${plural(otherBefore, "further request", "further requests")} before consent ${otherBefore === 1 ? "was" : "were"} not classified as analytics or marketing.`
    : "";
  return `${lead}${main}${rest}`;
}

function interaction(outcome: string | undefined): string {
  if (outcome === "clicked") return "Clicked";
  if (outcome === "click_failed") return "Found, click failed";
  return "Not tested";
}

export function buildReportVisual(
  evidence: ScanEvidenceResponse | null,
  meta: WebsiteScanMetadata | null,
  v: ScoreView | null,
  untestedStates: readonly string[] = [],
): ReportVisual | null {
  if (!evidence) return null;
  const { trackers, cookies, policies, forms, consent_signals: signals } = evidence;
  const summary = summariseTrackers(trackers);
  const acceptTested = !untestedStates.includes("post_accept");
  const rejectTested = !untestedStates.includes("post_reject");

  const counted = countByState(cookies as EvidenceCookieItem[], trackers);
  const cookieCounts = STATES.map((k) => counted.cookies[k]) as [number, number, number];
  const known = STATES.map((k) => counted.knownRequests[k]) as [number, number, number];
  const all = STATES.map((k) => counted.requests[k]) as [number, number, number];
  const mechanism = meta?.consent_mechanism;
  const bannerBefore = mechanism === "none" ? "No banner found" : mechanism === "banner" || mechanism === "cmp" ? "Banner found" : "Not known";
  const base = {
    acceptTested, rejectTested, cookies: cookieCounts, known, all,
    banner: [bannerBefore, interaction(meta?.accept_interaction), interaction(meta?.reject_interaction)] as [string, string, string],
  };
  const comparison: ConsentComparison = {
    ...base,
    takeaway: takeaway(base, summary.pre_consent.otherRequests + summary.pre_consent.unclassifiedRequests),
  };

  // One row per technology (the named vendor, else the registrable domain), however many requests it made.
  const byTech = new Map<string, { type: string; requests: number; states: Set<string> }>();
  for (const t of trackers) {
    const name = t.vendor ?? registrableDomain(t.script_src);
    const row = byTech.get(name) ?? { type: t.category ? capital(t.category) : "Unclassified", requests: 0, states: new Set<string>() };
    row.requests += 1;
    for (const s of t.consent_states) row.states.add(s);
    byTech.set(name, row);
  }
  const weight = (type: string) => (type === "Analytics" || type === "Marketing" ? 0 : type === "Unclassified" ? 1 : 2);
  const techRows = [...byTech.entries()]
    .map(([name, r]) => ({ name, type: r.type, requests: r.requests, seen: seenIn(r.states) }))
    .sort((a, b) => weight(a.type) - weight(b.type) || b.requests - a.requests || a.name.localeCompare(b.name));

  const policy = (type: string) => policies.find((p) => p.policy_type === type);
  const privacy = policy("privacy_policy");
  const terms = policy("terms");
  const cookiePolicy = policy("cookie_policy");
  const signal = signals[0];
  const vendor = signal?.cmp_vendor ?? meta?.cmp_vendor ?? null;
  const optionElement = (title: string, outcome: string | undefined): ElementView =>
    outcome === "clicked" ? { title, status: "found", note: "Clicked in the scan" }
      : outcome === "click_failed" ? { title, status: "found", note: "Found, could not be clicked" }
        : { title, status: "unverified", note: "Not reachable by the scan" };
  const granular = signal?.has_granular_choices;

  const elements: ElementView[] = [
    privacy ? { title: "Privacy policy", status: "found", note: pathOf(privacy.url) }
      : { title: "Privacy policy", status: "not_found", note: "Not found in scanned scope" },
    terms ? { title: "Terms of use", status: "found", note: pathOf(terms.url) }
      : { title: "Terms of use", status: "not_found", note: "Not found in scanned scope" },
    cookiePolicy ? { title: "Cookie policy", status: "found", note: pathOf(cookiePolicy.url) }
      : { title: "Cookie policy", status: "not_found", note: "Not found in scanned scope" },
    mechanism === "none" ? { title: "Consent banner", status: "not_found", note: "Not found in scanned scope" }
      : mechanism === "banner" || mechanism === "cmp"
        ? { title: "Consent banner", status: "found", note: vendor ?? "Vendor not identified" }
        : { title: "Consent banner", status: "unverified", note: "Not known" },
    optionElement("Accept option", meta?.accept_interaction),
    optionElement("Reject option", meta?.reject_interaction),
    forms.length ? { title: "Forms", status: "found", note: plural(forms.length, "form", "forms") }
      : { title: "Forms", status: "not_found", note: "None in scanned scope" },
    granular === true ? { title: "Per-purpose choices", status: "found", note: "Offered in the banner" }
      : granular === false ? { title: "Per-purpose choices", status: "not_found", note: "Not found in the banner" }
        : { title: "Per-purpose choices", status: "unverified", note: "Not readable by the scan" },
  ];

  const pages = meta?.pages_scanned ?? evidence.pages.length;
  return {
    pagesScanned: pages,
    pagesFound: meta?.pages_discovered !== undefined ? Math.max(meta.pages_discovered, pages) : null,
    // The page limit is one number for every scan the product makes: the server's (5). A stored scan that ran under an older limit
    // is still shown against that one number, never against whatever it happened to run with.
    pageLimit: v?.pageLimit ?? meta?.page_limit ?? null,
    statesTested: 1 + (acceptTested ? 1 : 0) + (rejectTested ? 1 : 0),
    requests: trackers.length,
    knownRequests: trackers.filter(isKnownTracker).length,
    cookies: cookies.length,
    domains: new Set(trackers.map((t) => registrableDomain(t.script_src))).size,
    consent: comparison,
    technologies: { rows: techRows.slice(0, MAX_TECH_ROWS), more: Math.max(0, techRows.length - MAX_TECH_ROWS) },
    elements,
  };
}
