import type {
  DiscoveredDomain, EvidenceCookieItem, EvidenceTrackerItem, FindingResponse, WebsiteScanMetadata,
} from "../api/types";
import { registrableDomain } from "../report/trackerSummary";

/** A host for the main-domain comparison: lowercase, without a leading "www.". */
export function hostKey(url: string): string {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch {
    return "";
  }
}

/** The host of the site that was ENTERED. The scan covers this host (www or not) and nothing else. */
export function mainHostOf(scanUrl: string | null | undefined): string {
  return scanUrl ? hostKey(scanUrl) : "";
}

export type DomainKind = "subdomain" | "external";
/** Whose result an item is: the scanned site's own, or a subdomain / separate website the scan came across. */
export type Scope = "main" | "discovered";

const isNamedTracker = (category: string | null | undefined) => category === "analytics" || category === "marketing";

function classify(mainHost: string, host: string): { key: string; kind: DomainKind } | null {
  if (!host || host === mainHost) return null;                       // the entered site itself
  const mainRegistrable = registrableDomain(`https://${mainHost}/`);
  if (registrableDomain(`https://${host}/`) === mainRegistrable) return { key: host, kind: "subdomain" };
  return { key: registrableDomain(`https://${host}/`), kind: "external" };
}

/**
 * A request made by a scanned page. It is the scanned site's own result when it goes to the site itself, or to a NAMED
 * analytics or advertising service the site embedded (Google Tag Manager, Meta Pixel): the site chose to load that tracker.
 * A request to a subdomain, or to a separate website or service that is not a named tracker (a booking widget, a
 * payment frame, a font host, the site displayed in a frame), is that other site's activity, not the scanned site's.
 */
export function trackerScope(mainHost: string, t: EvidenceTrackerItem): { scope: Scope; key: string; kind: DomainKind | null } {
  const c = classify(mainHost, hostKey(t.script_src));
  if (!c) return { scope: "main", key: mainHost, kind: null };
  if (c.kind === "external" && isNamedTracker(t.category)) return { scope: "main", key: mainHost, kind: null };
  return { scope: "discovered", key: c.key, kind: c.kind };
}

/** A cookie: the scanned site's own when set for its host or a parent domain, or by a named third-party tracker. */
export function cookieScope(mainHost: string, k: EvidenceCookieItem): { scope: Scope; key: string; kind: DomainKind | null } {
  const host = k.domain ? hostKey(`https://${k.domain.replace(/^\./, "")}/`) : "";
  if (!host || host === mainHost || mainHost.endsWith(`.${host}`)) return { scope: "main", key: mainHost, kind: null };
  const c = classify(mainHost, host);
  if (!c) return { scope: "main", key: mainHost, kind: null };
  if (c.kind === "external" && isNamedTracker(k.category)) return { scope: "main", key: mainHost, kind: null };
  return { scope: "discovered", key: c.key, kind: c.kind };
}

export interface SplitEvidence {
  mainTrackers: EvidenceTrackerItem[];
  mainCookies: EvidenceCookieItem[];
  discoveredTrackers: EvidenceTrackerItem[];
  discoveredCookies: EvidenceCookieItem[];
}

/** The evidence divided by whose it is. Nothing is dropped: every item lands in exactly one side. */
export function splitEvidence(mainHost: string, trackers: EvidenceTrackerItem[], cookies: EvidenceCookieItem[]): SplitEvidence {
  const out: SplitEvidence = { mainTrackers: [], mainCookies: [], discoveredTrackers: [], discoveredCookies: [] };
  for (const t of trackers) (trackerScope(mainHost, t).scope === "main" ? out.mainTrackers : out.discoveredTrackers).push(t);
  for (const k of cookies) (cookieScope(mainHost, k).scope === "main" ? out.mainCookies : out.discoveredCookies).push(k);
  return out;
}

/** Counts per consent state of items that are in that state. */
export function countByState(items: { consent_states: string[] }[]): Record<string, number> {
  const out: Record<string, number> = { pre_consent: 0, post_accept: 0, post_reject: 0 };
  for (const i of items) for (const s of i.consent_states) if (s in out) out[s] += 1;
  return out;
}

export interface DomainFinding {
  id: string;
  text: string;
  risk: string;
  /** Evidence items of this finding involving this domain, and how many it cites in all. */
  fromDomain: number;
  total: number;
}

export interface DomainRow {
  /** A subdomain is listed by its full host; an external site by the domain an organisation registers (google.com). */
  key: string;
  kind: DomainKind;
  /** Requests the scanned pages made to this domain, per consent state. */
  requests: { pre_consent: number; post_accept: number; post_reject: number };
  /** Cookies this domain set while the scanned pages loaded, per consent state, and a few of their names. */
  cookies: { pre_consent: number; post_accept: number; post_reject: number; names: string[] };
  /** Named analytics or advertising vendors among those requests (on a subdomain only: external ones are the site's own). */
  namedVendors: string[];
  /** Requests to something the catalogue does not know: for a person to look at, not a violation. */
  unclassifiedRequests: number;
  /** A few of the URLs the scanned pages requested from it (up to three, distinct), for the report. */
  requestUrls: string[];
  /** Times a link to it was found while crawling (links are not followed). */
  linksFound: number;
  sampleUrl: string | null;
  /** One URL per host that was found through a link or frame and merged into this row (a separate website is one row for
   *  all of its hosts), up to six; `moreHosts` counts the rest. */
  foundUrls: string[];
  moreHosts: number;
  /** The scanned page displays this domain in a frame. */
  framed: boolean;
  findings: DomainFinding[];
  needsReview: boolean;
}

export interface DomainReport {
  mainHost: string;
  subdomains: DomainRow[];
  external: DomainRow[];
}

const STATES = ["pre_consent", "post_accept", "post_reject"] as const;

/** evidence id -> the domain it involves: the scanned site (`mainHost`) or a discovered row's key. */
export function evidenceDomains(
  mainHost: string, trackers: EvidenceTrackerItem[], cookies: EvidenceCookieItem[],
): Map<string, string> {
  const byId = new Map<string, string>();
  for (const t of trackers) byId.set(t.id, trackerScope(mainHost, t).key);
  for (const k of cookies) byId.set(k.id, cookieScope(mainHost, k).key);
  return byId;
}

/** For one finding: how many of its evidence items involve each domain (the scanned site included). */
export function findingDomainCounts(finding: FindingResponse, index: Map<string, string>): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const id of finding.evidence ?? []) {
    const key = index.get(id);
    if (key) counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}

export type FindingScope = "main" | "discovered" | "mixed";

/**
 * Whom a finding is about. "discovered" only when ALL the evidence it cites belongs to subdomains or separate websites;
 * "mixed" when it cites both; "main" otherwise, including a finding that cites no evidence this page can place (it is the
 * scanned site's until shown otherwise, never silently moved).
 */
export function findingScope(counts: Record<string, number>, mainHost: string): FindingScope {
  const keys = Object.keys(counts);
  if (keys.length === 0) return "main";
  const onMain = keys.includes(mainHost);
  const onOther = keys.some((k) => k !== mainHost);
  return onMain && onOther ? "mixed" : onOther ? "discovered" : "main";
}

/**
 * The subdomains and separate websites the scan came across, for review. The scan covers only the entered host, so none of
 * these is a page that was scanned, and their requests and cookies are not the scanned site's trackers and cookies. A domain
 * appears because a scanned page made requests to it, linked to it, or displayed it in a frame -- evidence to look at, not
 * a verdict.
 */
export function deriveDomainReport(args: {
  scanUrl: string | null | undefined;
  trackers: EvidenceTrackerItem[];
  cookies: EvidenceCookieItem[];
  findings: FindingResponse[];
  meta: Pick<WebsiteScanMetadata, "discovered_domains"> | null;
}): DomainReport {
  const mainHost = mainHostOf(args.scanUrl);
  const rows = new Map<string, DomainRow>();
  const row = (key: string, kind: DomainKind): DomainRow => {
    let r = rows.get(key);
    if (!r) {
      r = {
        key, kind, requests: { pre_consent: 0, post_accept: 0, post_reject: 0 },
        cookies: { pre_consent: 0, post_accept: 0, post_reject: 0, names: [] },
        namedVendors: [], unclassifiedRequests: 0, requestUrls: [], linksFound: 0, sampleUrl: null, foundUrls: [], moreHosts: 0,
        framed: false, findings: [], needsReview: false,
      };
      rows.set(key, r);
    }
    return r;
  };

  for (const t of args.trackers) {
    const s = trackerScope(mainHost, t);
    if (s.scope !== "discovered" || !s.kind) continue;
    const r = row(s.key, s.kind);
    for (const state of STATES) if (t.consent_states.includes(state)) r.requests[state] += 1;
    if (r.requestUrls.length < 3 && !r.requestUrls.includes(t.script_src)) r.requestUrls.push(t.script_src);
    if (isNamedTracker(t.category)) {
      const vendor = t.vendor ?? s.key;
      if (!r.namedVendors.includes(vendor)) r.namedVendors.push(vendor);
    } else if (t.category == null && t.consent_states.includes("pre_consent")) {
      r.unclassifiedRequests += 1;
    }
  }

  for (const k of args.cookies) {
    const s = cookieScope(mainHost, k);
    if (s.scope !== "discovered" || !s.kind) continue;
    const r = row(s.key, s.kind);
    for (const state of STATES) if (k.consent_states.includes(state)) r.cookies[state] += 1;
    if (r.cookies.names.length < 5 && !r.cookies.names.includes(k.name)) r.cookies.names.push(k.name);
  }

  for (const d of (args.meta?.discovered_domains ?? []) as DiscoveredDomain[]) {
    const c = classify(mainHost, hostKey(`https://${d.host}/`));
    if (!c) continue;
    const r = row(c.key, c.kind);
    r.linksFound += d.links;
    r.sampleUrl ??= d.sample_url;
    if (!r.foundUrls.includes(d.sample_url)) {
      if (r.foundUrls.length < 6) r.foundUrls.push(d.sample_url);
      else r.moreHosts += 1;
    }
    r.framed = r.framed || d.framed;
  }

  const index = evidenceDomains(mainHost, args.trackers, args.cookies);
  for (const f of args.findings) {
    for (const [key, n] of Object.entries(findingDomainCounts(f, index))) {
      const r = rows.get(key);
      if (r) r.findings.push({ id: f.id, text: f.finding_text, risk: f.risk_level, fromDomain: n, total: (f.evidence ?? []).length });
    }
  }

  for (const r of rows.values()) {
    r.needsReview = r.kind === "subdomain" || r.framed || r.unclassifiedRequests > 0
      || (r.namedVendors.length > 0 && r.requests.pre_consent > 0) || r.cookies.pre_consent > 0;
  }

  // What most needs a look comes first: framed sites, then pre-consent cookies and unknown requests.
  const weight = (r: DomainRow) =>
    (r.framed ? 100 : 0) + (r.cookies.pre_consent ? 30 : 0) + (r.unclassifiedRequests ? 10 : 0) + Math.min(r.requests.pre_consent, 9);
  const sorted = [...rows.values()].sort((a, b) => weight(b) - weight(a) || a.key.localeCompare(b.key));
  return {
    mainHost,
    subdomains: sorted.filter((r) => r.kind === "subdomain"),
    external: sorted.filter((r) => r.kind === "external"),
  };
}
