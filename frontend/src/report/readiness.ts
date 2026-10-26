import type { EvidenceFormItem, EvidencePageItem, PolicyAnalysis, PolicyRequirementResult, ScanEvidenceResponse, SectorAssessment, WebsiteScanMetadata } from "../api/types";
import { ageGateSeen, childFieldNames } from "./insights";
import { formatRefs } from "./legalRefs";
import { framedSiteOf } from "./scanTransparency";
import { bannerNotDisplayed, observedBannerFact, scriptFailureNote } from "./consentObservation";

// The five-page DPDP readiness review: the five pages that matter most for the website-visible duties, each checked against the provisions the
// website can show. It is built from what the scan recorded (pages, forms, trackers, cookies, the consent banner, the policy analysis) and is the
// same on the dashboard and in the PDF.
//
//   Passes this technical check                 the scan saw (or the policy's own words, quoted exactly, say) that the check is met.
//   Gap                       the scan saw something that falls short, or the policy states the point only in part.
//   Not Found                 the page or the statement is not there in what the scan read.
//   Unable to Verify             the scan cannot see it (behind a login, not captured, not tested). Never counted as met.
//   Requires document review  cannot be judged from a website at all.
//
// A readiness check, not a finding of violation: most of Sections 3 to 17 apply from 13 May 2027. It never changes the DPDP Compliance Score.

export type ReadinessStatus = "Passes this technical check" | "Gap" | "Not Found" | "Unable to Verify" | "Requires document review";
export type EvidenceKind = "Quoted" | "Observed" | "Not seen" | "Not captured";

/** The fewest assessed checks that make a score: the same floor the compliance score uses (backend scoring/dpdp_score.py MIN_ASSESSED_REQUIREMENTS). */
export const MIN_ASSESSED_CHECKS = 2;

export const READINESS_CLOSING = "This is a compliance readiness review, not legal advice.";
export const READINESS_TIMING = "Most of Sections 3 to 17 of the Act and Rules 3 and 5 to 16 apply from 13 May 2027 (Section 6(9) and Rule 4, on Consent Managers, from 13 November 2026), so these are readiness findings, not violations.";

export interface SelectedPage {
  n: number;
  role: string;
  /** The URL chosen; null when no such page was found (NOT FOUND). */
  url: string | null;
  found: boolean;
  /** Whether the scan loaded it. A page seen only as a link or in the sitemap is named but could not be checked. */
  scanned: boolean;
  why: string;
}

export interface ReadinessRow {
  pageN: number;
  /** The short page name ("Sign-up", "Homepage", ...), or "Not judged from the website". */
  page: string;
  sections: string;
  check: string;
  status: ReadinessStatus;
  evidence: string;
  evidenceKind: EvidenceKind;
  fix: string;
  /** 1 (low) to 5 (high): how much it matters if the check fails. Used only to rank the gaps. */
  risk: number;
  /** Whether the row counts in the readiness score. A page that does not exist is a finding, not a failed check. */
  scored: boolean;
}

export interface GapSummary { rank: number; row: ReadinessRow; riskLabel: "High" | "Medium" | "Low" }

export interface ReadinessReview {
  available: boolean;
  reason: string | null;
  pages: SelectedPage[];
  rows: ReadinessRow[];
  counts: Record<ReadinessStatus, number>;
  assessed: number;
  /** Passes this technical check checks over those that could be assessed (Passes this technical check, Gap, Not Found), as a whole number out of 100; null when none could be. */
  scorePercent: number | null;
  scoreLine: string;
  topGaps: GapSummary[];
  riskiest: { n: number; page: string; why: string } | null;
  notes: string[];
  timing: string;
  closing: string;
}

// ─── finding the five pages ─────────────────────────────────────────────────────────────────────────────────────────────────────────

interface Candidate { url: string; title: string; scanned: boolean }

const SIGNUP = /sign[-_ ]?up|register|registration|create[-_ ]?(an[-_ ]?)?account|join([-_ ]?(us|now))?(\/|$)|get[-_ ]?started|free[-_ ]?trial|onboard/i;
const CHECKOUT = /checkout|\/cart|basket|payment|place[-_ ]?order/i;
const LEAD = /contact|enquir|inquir|demo|get[-_ ]?a[-_ ]?quote|\/quote|book[-_ ]|talk[-_ ]to|get[-_ ]in[-_ ]touch|request[-_ ]/i;
const ACCOUNT = /my[-_ ]?account|\/account|profile|settings|preferences|\/dashboard|\/portal/i;
const LOGIN = /log[-_ ]?in|sign[-_ ]?in/i;
const GRIEVANCE = /grievance|data[-_ ]principal|your[-_ ]?(privacy[-_ ])?rights|privacy[-_ ]rights|data[-_ ](subject[-_ ])?request|\bdsr\b|\bdpo\b|redress|rights[-_ ]request|subject[-_ ]access|privacy[-_ ]request/i;
const CONTACT = /contact|get[-_ ]in[-_ ]touch|reach[-_ ]us/i;

const pathOf = (url: string): string => {
  try { const u = new URL(url); return `${u.pathname}${u.search}`.toLowerCase(); } catch { return url.toLowerCase(); }
};
const depth = (url: string): number => pathOf(url).split("/").filter(Boolean).length;
const sameUrl = (a: string | null | undefined, b: string | null | undefined): boolean => {
  if (!a || !b) return false;
  const norm = (u: string) => u.replace(/#.*$/, "").replace(/\/+$/, "").toLowerCase();
  return norm(a) === norm(b);
};

function loaded(p: EvidencePageItem): boolean {
  if (p.discovered_via === "failed" || p.discovered_via === "timeout") return false;
  return p.http_status == null || p.http_status < 400;
}

function candidates(evidence: ScanEvidenceResponse | null | undefined, meta: WebsiteScanMetadata | null | undefined): Candidate[] {
  const out: Candidate[] = [];
  const seen = new Set<string>();
  for (const p of evidence?.pages ?? []) {
    const key = p.url.replace(/#.*$/, "").replace(/\/+$/, "").toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ url: p.url, title: p.title ?? "", scanned: loaded(p) });
  }
  for (const s of meta?.skipped_pages ?? []) {
    const key = s.url.replace(/#.*$/, "").replace(/\/+$/, "").toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ url: s.url, title: "", scanned: false });
  }
  return out;
}

/** The best page for a role: one the scan loaded before one it only knows of, then the shallowest path. */
// A blog post, article or press item is about a topic; it is not the page for that topic. Matching the five roles against its slug picked /blog/get-started-with-... as the sign-up page
// (the same rule the crawl uses to rank pages: scanner/url_normalize.py).
const CONTENT_SECTIONS = new Set(["blog", "blogs", "news", "article", "articles", "post", "posts", "insights", "press", "stories", "magazine", "tag", "tags", "category", "categories", "archive", "archives", "author", "authors"]);
const isEditorial = (url: string): boolean => pathOf(url).split("?")[0].split("/").some((segment) => CONTENT_SECTIONS.has(segment));

function pick(cands: Candidate[], test: (c: Candidate) => boolean): Candidate | null {
  const hits = cands.filter((c) => !isEditorial(c.url) && test(c));
  hits.sort((a, b) => Number(b.scanned) - Number(a.scanned) || depth(a.url) - depth(b.url) || a.url.length - b.url.length);
  return hits[0] ?? null;
}
const text = (c: Candidate) => `${pathOf(c.url)} ${c.title}`;

// ─── what the checks rest on ───────────────────────────────────────────────────────────────────────────────────────────────────────

const policyRow = (a: PolicyAnalysis | null | undefined, id: string): PolicyRequirementResult | undefined => a?.requirements?.find((r) => r.id === id);
const policyRead = (a: PolicyAnalysis | null | undefined): a is PolicyAnalysis => !!a && a.status === "analysed" && a.requirements.length > 0;
const RANK: Record<ReadinessStatus, number> = { "Passes this technical check": 0, "Unable to Verify": 1, "Requires document review": 1, Gap: 2, "Not Found": 3 };
const worst = (xs: ReadinessStatus[]): ReadinessStatus => xs.reduce((a, b) => (RANK[b] > RANK[a] ? b : a), "Passes this technical check" as ReadinessStatus);

const listText = (xs: string[]): string => xs.join(", ");
const fieldLabel = (f: { name: string; field_type: string; required: boolean }): string => `${f.name || f.field_type}${f.field_type && f.field_type !== "text" && f.name ? ` (${f.field_type})` : ""}${f.required ? ", required" : ""}`;

const hostOf = (src: string): string => { try { return new URL(src, "https://x.invalid").hostname || src; } catch { return src; } };
const NON_ESSENTIAL = new Set(["analytics", "marketing"]);

/** The whole review. `siteUrl` is the address that was scanned. */
export function buildReadinessReview(args: {
  siteUrl: string;
  evidence: ScanEvidenceResponse | null | undefined;
  meta: WebsiteScanMetadata | null | undefined;
  policy: PolicyAnalysis | null | undefined;
  sector: SectorAssessment | null | undefined;
}): ReadinessReview {
  const { evidence, meta, policy, sector } = args;
  const empty: ReadinessReview = {
    available: false, reason: null, pages: [], rows: [], counts: { "Passes this technical check": 0, Gap: 0, "Not Found": 0, "Unable to Verify": 0, "Requires document review": 0 }, assessed: 0,
    scorePercent: null, scoreLine: "", topGaps: [], riskiest: null, notes: [], timing: READINESS_TIMING, closing: READINESS_CLOSING,
  };
  const scannedPages = (evidence?.pages ?? []).filter(loaded);
  if (!evidence || scannedPages.length === 0) {
    return { ...empty, reason: "The scan recorded no loaded pages, so the five pages cannot be chosen or checked." };
  }
  const policyWhy: string = policy?.reason ?? "The privacy policy analysis was not run or not available for this scan, so its text was not read.";
  const cands = candidates(evidence, meta);
  const forms = evidence.forms ?? [];
  const formsOn = (url: string | null): { forms: EvidenceFormItem[]; located: boolean } => {
    if (!url) return { forms: [], located: false };
    const located = forms.some((f) => f.page_url !== undefined);
    return { forms: forms.filter((f) => sameUrl(f.page_url, url)), located };
  };

  // 1. Sign-up, else Checkout, else the main lead form's page.
  const signup = pick(cands, (c) => SIGNUP.test(text(c)) && !LOGIN.test(pathOf(c.url)));
  const checkout = signup ? null : pick(cands, (c) => CHECKOUT.test(text(c)));
  const lead = signup || checkout ? null : pick(cands, (c) => LEAD.test(text(c)));
  const firstPage = signup ?? checkout ?? lead;
  const firstRole = signup ? "Sign-up / Registration" : checkout ? "Checkout (no sign-up page found)" : lead ? "Lead / enquiry form (no sign-up or checkout page found)" : "Sign-up / Registration";
  // 2. Homepage: the page that was entered.
  const home = scannedPages.find((p) => p.discovered_via === "seed") ?? [...scannedPages].sort((a, b) => depth(a.url) - depth(b.url))[0];
  // 3. Privacy notice.
  const policyLinks = (evidence.policies ?? []).filter((p) => p.policy_type === "privacy_policy");
  const privacyUrl = policyLinks[0]?.url ?? pick(cands, (c) => /privacy/i.test(pathOf(c.url)))?.url ?? null;
  const privacyScanned = !!privacyUrl && (cands.some((c) => sameUrl(c.url, privacyUrl) && c.scanned) || policyRead(policy));
  // 4. Account settings, else the main contact / lead form.
  const account = pick(cands, (c) => ACCOUNT.test(text(c)) && !SIGNUP.test(pathOf(c.url)) && !LOGIN.test(pathOf(c.url)));
  const contact = pick(cands, (c) => CONTACT.test(text(c))) ?? pick(cands, (c) => LEAD.test(text(c)));
  const fourth = account ?? contact;
  // 5. Grievance / rights page, else Contact Us.
  const grievance = pick(cands, (c) => GRIEVANCE.test(text(c)));
  const fifth = grievance ?? contact;

  const pages: SelectedPage[] = [
    { n: 1, role: firstRole, url: firstPage?.url ?? null, found: !!firstPage, scanned: !!firstPage?.scanned,
      why: signup ? "Where personal data is first collected: the most important page for notice and consent." : checkout ? "No sign-up page was found, so the checkout is used: it is where personal data is collected." : lead ? "Neither a sign-up nor a checkout page was found, so the main enquiry form's page is used." : "NOT FOUND: no sign-up, checkout or enquiry page was found among the pages scanned or the links the scan discovered." },
    { n: 2, role: "Homepage", url: home.url, found: true, scanned: true, why: "Where the cookie banner, the footer and the trackers that fire on arrival are seen." },
    { n: 3, role: "Privacy Notice / Privacy Policy", url: privacyUrl, found: !!privacyUrl, scanned: privacyScanned,
      why: privacyUrl ? "The notice the Act and Rules require: what is collected, why, and how to exercise rights." : "NOT FOUND: no privacy policy link was found on the pages scanned." },
    { n: 4, role: account ? "Account Settings / Profile" : "Account Settings / Profile (no account page found; main contact form used)", url: fourth?.url ?? null, found: !!fourth, scanned: !!fourth?.scanned,
      why: account ? "Where a person would manage consent and their data. It is behind a login the scan does not use." : fourth ? "NOT FOUND: no account or profile page was found, so the main contact form's page is used. Account features cannot be tested without signing in." : "NOT FOUND: no account page and no contact form page were found." },
    { n: 5, role: grievance ? "Grievance / Data Principal Rights" : "Grievance / Data Principal Rights (no separate page found; Contact Us used)", url: fifth?.url ?? null, found: !!fifth, scanned: !!fifth?.scanned,
      why: grievance ? "Where a person is told how to exercise a right or complain." : fifth ? "NOT FOUND: no separate grievance or rights page was found, so Contact Us is used." : "NOT FOUND: no grievance, rights or contact page was found." },
  ];

  const rows: ReadinessRow[] = [];
  const add = (r: Omit<ReadinessRow, "page" | "scored"> & { scored?: boolean }) => {
    rows.push({
      ...r, evidence: r.evidence.replace(/\s+/g, " ").trim(), page: r.pageN === 0 ? "Not judged from the website" : shortName(pages[r.pageN - 1].role),
      scored: r.scored ?? (r.status !== "Unable to Verify" && r.status !== "Requires document review"),
    });
  };
  const ref = (...r: string[]) => formatRefs(r);

  // A page that does not exist, or that was only named, is a finding of its own, and the checks that need it cannot be made.
  const existence = (p: SelectedPage, wanted: string, fix: string, counts = false) => {
    if (!p.found) add({ pageN: p.n, sections: "—", check: `Page exists: ${wanted}`, status: "Not Found", evidence: `${p.why}`, evidenceKind: "Not seen", fix, risk: counts ? 4 : 2, scored: counts });
  };
  const notScannedNote = (p: SelectedPage): string | null =>
    p.found && !p.scanned ? `${p.url} was found as a link or in the sitemap but was not loaded in this scan, so its contents were not read.` : null;

  // ── Page 1: sign-up ────────────────────────────────────────────────────────────────────────────────────────────────────────
  {
    const p = pages[0];
    existence(p, "sign-up, checkout or enquiry page", "Add a sign-up or enquiry page that collects personal data with a notice at the point of collection, or confirm that none is needed.");
    const where = formsOn(p.url);
    // Older scans did not record a form's page: fall back to forms that look like a sign-up and say so.
    const fallbackForms = !where.located ? forms.filter((f) => f.purpose_guess === "login_or_signup" || f.purpose_guess === "newsletter_signup" || f.purpose_guess === "contact_or_support") : [];
    const fs = where.located ? where.forms : fallbackForms;
    const fieldsAll = fs.flatMap((f) => f.fields);
    const named = notScannedNote(p);
    const cannotRead = !p.found ? "No page to check." : named ?? null;
    const formNote = !p.found ? "" : where.located
      ? (fs.length ? `${fs.length} form${fs.length === 1 ? "" : "s"} recorded on this page. ` : "No form was recorded on this page. ")
      : (fs.length ? `The scan did not record which page each form was on (an older scan); ${fs.length} form${fs.length === 1 ? "" : "s"} elsewhere in the scan look like sign-up or enquiry forms. ` : "No sign-up-like form was recorded. ");
    const fieldsText = fieldsAll.length ? `Fields recorded: ${listText([...new Set(fieldsAll.map(fieldLabel))])}. ` : "";
    const base = cannotRead ?? `${formNote}${fieldsText}`;
    const supporting = (id: string, label: string): string => {
      const r = policyRow(policy, id);
      return r ? ` The privacy policy, separately: ${label} ${r.status === "verified" ? "quoted" : r.status === "partial" ? "partly stated" : "not found"}.` : "";
    };
    add({ pageN: 1, sections: ref("DPDP Act s.5", "DPDP Rules r.3"), check: "A notice is shown at the point of collection listing each item of personal data and the specific purpose", status: "Unable to Verify",
      evidence: `${base}The scan keeps a form's fields, not the notice text around them, so it cannot say whether a notice appears beside the form.${supporting("notice_data_itemised", "the itemised data list")}`.trim(), evidenceKind: "Not captured",
      fix: "To verify: open the page, and check that a notice beside or above the form lists each item collected and its purpose, and links to the full notice.", risk: 5 });
    const boxes = fieldsAll.filter((f) => f.field_type === "checkbox");
    const bundled = boxes.filter((f) => f.required && /market|newsletter|promo|offer|partner|third[-_ ]?party|share/i.test(f.name));
    add({ pageN: 1, sections: ref("DPDP Act s.6(1)"), check: "Consent is free, specific, informed, unconditional and unambiguous, by a clear affirmative action, with no pre-ticked box and no bundling",
      status: bundled.length ? "Gap" : "Unable to Verify",
      evidence: bundled.length
        ? `${base}A required checkbox that looks like an opt-in to something beyond the service was recorded: ${listText(bundled.map(fieldLabel))}. A required opt-in makes consent a condition of using the service.`
        : `${base}${boxes.length ? `Checkbox${boxes.length === 1 ? "" : "es"} recorded: ${listText(boxes.map(fieldLabel))}. ` : "No consent checkbox was recorded. "}The scan does not record whether a box is ticked in advance, or the wording beside it.`.trim(),
      evidenceKind: bundled.length ? "Observed" : "Not captured",
      fix: bundled.length ? "Make the marketing or sharing opt-in optional and unticked by default, and keep consent to the service itself separate from it." : "To verify: open the page; confirm no box is ticked in advance, consent is a clear action, and each purpose has its own choice.", risk: 5 });
    const sensitive = [...new Set(fieldsAll.map((f) => f.name).filter((n) => /aadhaar|aadhar|\bpan\b|passport|dob|birth|gender|religion|caste|income|salary|address|pincode/i.test(n)))];
    add({ pageN: 1, sections: ref("DPDP Act s.6(1)"), check: "Only the fields needed for the purpose are collected (data minimisation)", status: "Unable to Verify",
      evidence: `${base}${fieldsAll.length ? `${new Set(fieldsAll.map((f) => f.name)).size} distinct field${new Set(fieldsAll.map((f) => f.name)).size === 1 ? "" : "s"} recorded.${sensitive.length ? ` Fields beyond basic contact details: ${listText(sensitive)}.` : " No field beyond basic contact details was recorded."} ` : ""}Whether each field is needed depends on the purpose, which the scan cannot see.`.trim(),
      evidenceKind: fieldsAll.length ? "Observed" : "Not captured",
      fix: "To verify: for each field, write down the purpose that needs it; remove or make optional any field with no purpose.", risk: 3 });
    add({ pageN: 1, sections: ref("DPDP Act s.4", "DPDP Act s.7"), check: "Where no consent is taken, a valid legitimate use applies", status: "Requires document review",
      evidence: "Whether a legitimate use (Section 7) applies depends on why the data is collected and on records the website does not show.", evidenceKind: "Not captured",
      fix: "Review the lawful ground for each purpose; where it is a legitimate use, record which one in the processing records.", risk: 3, scored: false });
    add({ pageN: 1, sections: ref("DPDP Act s.6(7)", "DPDP Act s.6(8)", "DPDP Act s.6(9)", "DPDP Rules r.4"), check: "If a Consent Manager is used, it is integrated", status: "Unable to Verify",
      evidence: `${(meta?.cmp_vendor ? `A cookie consent tool was detected (${meta.cmp_vendor}). That is not a Consent Manager registered under the Act. ` : "")}Nothing on the page shows a Consent Manager integration. The provisions on Consent Managers apply from 13 November 2026.`,
      evidenceKind: "Not seen", fix: "If a Consent Manager is offered, confirm the page lets a person give, manage and withdraw consent through it.", risk: 1, scored: false });
    const gate = ageGateSeen(evidence);
    const childFields = childFieldNames(evidence);
    add({ pageN: 1, sections: ref("DPDP Act s.9(1)", "DPDP Rules r.10"), check: "There is an age gate, and verifiable parental consent for anyone under 18",
      status: gate ? "Passes this technical check" : "Unable to Verify",
      evidence: gate ? `An age gate was recognised: ${gate}.` : `${childFields.length ? `Fields that suggest age or children were recorded: ${listText(childFields)}. ` : "No field asking for age or a guardian was recorded. "}No age gate was recognised; the scan only recognises those that consent tools show, so this is not proof there is none. Parental consent is not visible from outside.`,
      evidenceKind: gate ? "Observed" : "Not seen",
      fix: gate ? "No fix needed on what the scan saw; confirm verifiable parental consent follows for under-18s." : "To verify: if the service can be used by under-18s, check for an age question and a verifiable parental-consent step before data is processed.", risk: childFields.length || sector?.declarations?.some((d) => d.flag === "children") ? 5 : 2 });
    const httpsOk = !!p.url && /^https:\/\//i.test(p.url);
    const q = (() => { try { return [...new URL(p.url ?? "").searchParams.entries()]; } catch { return [] as [string, string][]; } })();
    const leaky = q.filter(([k, v]) => /e-?mail|phone|mobile|name|dob|aadhaar|pan|otp|password|token/i.test(k) || /@/.test(v));
    const plainForms = fs.filter((f) => (f.submit_url ?? "").toLowerCase().startsWith("http://"));
    const httpsStatus: ReadinessStatus = !p.found || !p.scanned ? "Unable to Verify" : !httpsOk || leaky.length || plainForms.length ? "Gap" : "Passes this technical check";
    add({ pageN: 1, sections: ref("DPDP Act s.8(5)"), check: "The page is served over HTTPS, with no personal data in its URL", status: httpsStatus,
      evidence: !p.found ? "No page to check." : named ?? `${p.url} is ${httpsOk ? "served over HTTPS" : "not served over HTTPS"}. ${leaky.length ? `Its address carries what looks like personal data: ${listText(leaky.map(([k]) => k))}. ` : "Its address carries no parameter that looks like personal data. "}${plainForms.length ? `${plainForms.length} form${plainForms.length === 1 ? "" : "s"} on it post to an unencrypted address.` : ""}`.trim(),
      evidenceKind: !p.found || !p.scanned ? "Not captured" : "Observed",
      fix: httpsStatus === "Gap" ? "Serve the page over HTTPS only, post forms to HTTPS addresses, and keep personal data out of URLs." : "To verify: load the page and confirm the address bar shows HTTPS.", risk: 4 });
  }

  // ── Page 2: homepage ────────────────────────────────────────────────────────────────────────────────────────────────────────
  {
    const mech = meta?.consent_mechanism;
    const signals = (evidence.consent_signals ?? []).filter((s) => s.mechanism_type !== "none");
    // A consent tool's SCRIPT on the page is not a banner on screen: when the tool loaded but showed the scanner nothing (a region it does not target, a gate), whether a banner appears is not established.
    const toolOnly = bannerNotDisplayed(signals[0]);
    const bannerSeen = (mech === "banner" || mech === "cmp" || signals.length > 0) && !toolOnly;
    add({ pageN: 2, sections: ref("DPDP Act s.6(1)", "DPDP Act s.5(1)"), check: "A cookie banner appears", status: bannerSeen ? "Passes this technical check" : mech === "none" ? "Not Found" : "Unable to Verify",
      evidence: toolOnly ? `A consent tool${meta?.cmp_vendor ? ` (${meta.cmp_vendor})` : ""} is loaded on the page, but no banner was shown to the scanner, so whether a banner appears to visitors is not established.${scriptFailureNote(signals[0])}` : bannerSeen ? `A consent ${mech === "cmp" || meta?.cmp_vendor ? `tool${meta?.cmp_vendor ? ` (${meta.cmp_vendor})` : ""}` : "banner"} was detected on the homepage.` : mech === "none" ? "No cookie banner or consent tool was detected on the homepage." : "The scan could not tell whether a banner appeared (the mechanism is unknown, or the scan predates it).",
      evidenceKind: bannerSeen ? "Observed" : mech === "none" ? "Not seen" : "Not captured",
      fix: bannerSeen ? "No fix needed on what the scan saw." : mech === "none" ? "Add a consent banner that asks before any analytics or advertising tag runs." : "To verify: load the homepage in a clean browser and note whether a banner appears.", risk: 4 });
    const rejectAll = observedBannerFact(signals[0], signals.find((s) => s.has_reject_all !== null)?.has_reject_all ?? null);
    const rejectClicked = meta?.reject_interaction === "clicked";
    add({ pageN: 2, sections: ref("DPDP Act s.6(1)", "DPDP Rules r.3(c)(i)"), check: "Reject is as easy as Accept",
      status: !bannerSeen ? "Unable to Verify" : rejectAll === true && rejectClicked ? "Passes this technical check" : rejectAll === false ? "Gap" : "Unable to Verify",
      evidence: toolOnly ? "A consent tool is loaded but its banner was never shown to the scanner, so Accept and Reject could not be compared." : !bannerSeen ? "No banner appeared, so there is no choice to compare." : rejectAll === false ? "The banner has an Accept option but no Reject option on its first layer." : rejectAll === true ? (rejectClicked ? "A Reject option was found and the scan clicked it." : "A Reject option was found, but the scan could not click it, so how easy it is was not tested.") : "The scan could not tell whether the banner offers a Reject option.",
      evidenceKind: toolOnly ? "Not captured" : !bannerSeen ? "Not seen" : rejectAll === null ? "Not captured" : "Observed",
      fix: rejectAll === false ? "Add a Reject all button of the same size and prominence as Accept, on the first layer of the banner." : "To verify: open the banner and compare the steps and prominence of Accept and Reject.", risk: 4 });
    const pre = (evidence.trackers ?? []).filter((t) => t.consent_states.includes("pre_consent") && NON_ESSENTIAL.has(t.category ?? ""));
    const preCookies = (evidence.cookies ?? []).filter((c) => c.consent_states.includes("pre_consent") && NON_ESSENTIAL.has(c.category ?? ""));
    const nameT = (t: { vendor: string | null; script_src: string; category: string | null }) => `${t.vendor ?? hostOf(t.script_src)} (${t.category})`;
    const preNames = [...new Set([...pre.map(nameT), ...preCookies.map((c) => `${c.provider ?? c.vendor ?? c.domain ?? "unknown"} cookie ${c.name} (${c.category})`)])];
    add({ pageN: 2, sections: ref("DPDP Act s.6(1)", "DPDP Act s.5(1)"), check: "No analytics or advertising tag fires before the visitor consents", status: preNames.length ? "Gap" : "Passes this technical check",
      evidence: preNames.length ? `Before any choice, ${preNames.length} analytics or advertising item${preNames.length === 1 ? "" : "s"} loaded: ${listText(preNames)}.` : "Before any choice, no analytics or advertising tag or cookie was recorded.",
      evidenceKind: preNames.length ? "Observed" : "Not seen",
      fix: preNames.length ? "Load these tags only after a person accepts, and confirm in a clean browser that nothing non-essential runs on arrival." : "No fix needed on what the scan saw.", risk: 5 });
    add({ pageN: 2, sections: ref("DPDP Act s.6(4)"), check: "There is a way to change or withdraw cookie consent later", status: "Unable to Verify",
      evidence: `${meta?.cmp_vendor ? `A consent tool (${meta.cmp_vendor}) was detected; ` : ""}whether a footer link or button reopens the choices was not tested, so this is not proof it is missing.`,
      evidenceKind: "Not captured", fix: "To verify: after choosing on the banner, look for a persistent link or button (often in the footer) that reopens the choices.", risk: 3 });
    const svc = (evidence.third_party_services ?? []).map((s) => s.service_name);
    const vendors = (evidence.trackers ?? []).map((t) => t.vendor ?? hostOf(t.script_src));
    const names = [...new Set([...svc, ...vendors])];
    const domains = [...new Set([...(evidence.third_party_services ?? []).flatMap((s) => s.domains), ...(evidence.trackers ?? []).map((t) => hostOf(t.script_src))])];
    add({ pageN: 2, sections: ref("DPDP Act s.8(1)", "DPDP Act s.8(2)", "DPDP Act s.16"), check: "Third-party scripts are listed, with where each sends data (India or abroad)", status: "Unable to Verify",
      evidence: names.length ? `${names.length} third-party service${names.length === 1 ? "" : "s"} or script${names.length === 1 ? "" : "s"} recorded: ${listText(names)}. Domains contacted: ${listText(domains)}. The scan records the address of each, not the country its servers are in, so where the data goes is not determined.` : "No third-party script or service was recorded. Where data goes was therefore not in question on what the scan saw.",
      evidenceKind: names.length ? "Observed" : "Not seen",
      fix: "To verify: for each service, confirm from its documentation or contract where it processes the data, and disclose any transfer outside India.", risk: 3 });
    const sig = meta?.language_signals;
    const eng = (sig?.html_lang ?? "").toLowerCase().startsWith("en");
    const indic = !!sig && (sig.pages_with_indic_script > 0 || sig.switcher_scripts.length > 0);
    const langRow = policyRow(policy, "notice_language");
    add({ pageN: 2, sections: ref("DPDP Act s.5(3)"), check: "The notice is available in English or any Eighth Schedule language",
      status: sig && eng && indic ? "Passes this technical check" : "Unable to Verify",
      evidence: !sig ? "Not recorded for this scan." : `The homepage declares language ${sig.html_lang ?? "none"}. ${indic ? `Indian-script content was seen on ${sig.pages_with_indic_script} of ${sig.pages_checked} pages${Object.keys(sig.indic_scripts).length ? ` (${listText(Object.keys(sig.indic_scripts))})` : ""}. ` : "No Indian-script content was seen. "}${langRow?.status === "verified" ? "The policy itself says the notice is available in other languages (quoted in the policy checks). " : ""}Seeing a language on a page does not show the privacy notice itself is offered in it.`.trim(),
      evidenceKind: !sig ? "Not captured" : "Observed",
      fix: "To verify: open the privacy notice and look for a language chooser with English and Indian-language versions.", risk: 2 });
    const dpo = policyRow(policy, "dpo_contact");
    add({ pageN: 2, sections: ref("DPDP Act s.8(9)", "DPDP Rules r.9"), check: "The footer or policy publishes the business contact of a Data Protection Officer or answering person",
      status: !policyRead(policy) ? "Unable to Verify" : dpo?.status === "verified" ? "Passes this technical check" : dpo?.status === "partial" ? "Gap" : "Unable to Verify",
      evidence: !policyRead(policy) ? "The privacy policy analysis was not available. The footer's text is not kept by the scan." : dpo?.status === "verified" ? `Quoted from the policy: “${dpo.quote}”${dpo.source_url ? ` (${dpo.source_url})` : ""}.` : dpo?.status === "partial" ? `A role is named without a contact: “${dpo.quote}”` : "The policy text has no such contact. The homepage footer's text is not kept by the scan, so a contact there was not checked.",
      evidenceKind: dpo?.status === "verified" || dpo?.status === "partial" ? "Quoted" : "Not captured",
      fix: dpo?.status === "verified" ? "No fix needed on what the scan read." : "Publish an email or phone number for a Data Protection Officer or a person who can answer questions, in the footer and the policy.", risk: 4 });
    const kids = !!sector?.declarations?.some((d) => d.flag === "children") || childFieldNames(evidence).length > 0;
    const ads = [...new Set((evidence.trackers ?? []).filter((t) => t.category === "marketing").map(nameT))];
    add({ pageN: 2, sections: ref("DPDP Act s.9(3)"), check: "No tracking, behavioural monitoring or targeted advertising is aimed at children",
      status: kids && ads.length ? "Gap" : "Unable to Verify",
      evidence: kids && ads.length ? `The sector or forms suggest children's data may be involved, and advertising trackers were observed: ${listText(ads)}.` : kids ? "The sector or forms suggest children's data may be involved. No advertising tracker was observed, but who the advertising is aimed at cannot be seen." : `Nothing suggests the site is aimed at children${ads.length ? `, though advertising trackers were observed (${listText(ads)})` : ""}. The scan cannot see who the audience is.`,
      evidenceKind: kids ? "Observed" : "Not seen",
      fix: kids && ads.length ? "Remove advertising and behavioural trackers from pages and flows used by children, or confirm those pages are not." : "To verify: confirm from the audience data that children are not a target, and that no behavioural advertising runs for under-18s.", risk: kids ? 5 : 2 });
  }

  // ── Page 3: privacy notice ──────────────────────────────────────────────────────────────────────────────────────────────────
  {
    const p = pages[2];
    existence(p, "privacy notice", "Publish a privacy notice that stands alone, in plain language, and link it from every page that collects personal data.");
    const noPolicy = !p.found;
    const unread = p.found && !policyRead(policy);
    const rowFor = (ids: string[], pageN: number, sections: string, check: string, risk: number, fix: string, fixIfOk = "No fix needed on what the scan read.") => {
      const rs = ids.map((id) => policyRow(policy, id));
      let status: ReadinessStatus;
      let evidence: string;
      let kind: EvidenceKind = "Quoted";
      if (noPolicy) { status = "Not Found"; evidence = "No privacy policy was found, so there is no text to carry this."; kind = "Not seen"; }
      else if (unread) { status = "Unable to Verify"; evidence = policyWhy; kind = "Not captured"; }
      else if (rs.every((r) => !r || r.status === "not_applicable")) { status = "Unable to Verify"; evidence = "Not assessed for this site."; kind = "Not captured"; }
      else {
        const sts = rs.filter((r): r is PolicyRequirementResult => !!r && r.status !== "not_applicable").map((r): ReadinessStatus => r.status === "verified" ? "Passes this technical check" : r.status === "partial" ? "Gap" : "Not Found");
        status = worst(sts);
        evidence = rs.filter((r): r is PolicyRequirementResult => !!r && r.status !== "not_applicable").map((r) => {
          const label = r.title;
          if (r.status === "verified" || r.status === "partial") return `${label}: ${r.status === "verified" ? "quoted" : "partly stated"}: “${r.quote}”${r.source_url ? ` (${r.source_url})` : ""}`;
          return `${label}: not found in the policy text`;
        }).join("; ");
        if (sts.every((s) => s === "Not Found")) kind = "Not seen";
      }
      add({ pageN, sections, check, status, evidence, evidenceKind: kind, fix: status === "Passes this technical check" ? fixIfOk : fix, risk });
    };
    rowFor(["notice_data_itemised", "notice_purposes"], 3, ref("DPDP Act s.5(1)", "DPDP Rules r.3(b)"), "An itemised list of the personal data collected and the purpose for each item", 5, "List each kind of personal data in plain language, with the purpose and the service it enables beside each.");
    rowFor(["consent_withdrawal"], 3, ref("DPDP Act s.6(4)", "DPDP Rules r.3(c)"), "How to withdraw consent", 4, "Say that consent can be withdrawn at any time and give the means, as easy as giving it.");
    rowFor(["rights_access", "rights_correction_erasure", "rights_nominate"], 3, ref("DPDP Act s.11", "DPDP Act s.12", "DPDP Act s.14"), "How to exercise rights: access, correction, erasure and nomination", 5, "State each right (summary, correction and erasure, nomination) and how to request it.");
    rowFor(["complaint_to_board"], 3, ref("DPDP Act s.13", "DPDP Rules r.3(c)(iii)"), "How to complain to the Data Protection Board", 4, "Explain that a person can complain to the Data Protection Board if the grievance is not resolved, and how.");
    rowFor(["retention"], 3, ref("DPDP Act s.8(7)"), "The retention or erasure period", 3, "State how long each kind of data is kept, or the event after which it is erased.");
    rowFor(["dpo_contact"], 3, ref("DPDP Act s.8(9)", "DPDP Rules r.9"), "The Data Protection Officer's or contact person's details", 4, "Publish the business contact of a Data Protection Officer or of a person who can answer questions.");
    add({ pageN: 3, sections: ref("DPDP Act s.10"), check: "If the owner is a Significant Data Fiduciary, the Data Protection Officer is based in India", status: "Requires document review",
      evidence: "Whether the organisation has been notified as a Significant Data Fiduciary, and where its officer is based, are not shown on a website.", evidenceKind: "Not captured",
      fix: "Confirm whether the organisation is notified as a Significant Data Fiduciary; if so, appoint a Data Protection Officer based in India.", risk: 2, scored: false });
    rowFor(["cross_border"], 3, ref("DPDP Act s.16", "DPDP Rules r.15"), "Disclosure of cross-border transfers", 3, "State whether personal data is transferred outside India and to which countries or categories of recipient.");
    add({ pageN: 3, sections: ref("DPDP Act s.5", "DPDP Rules r.3"), check: "The effective date and the last-updated date are shown", status: "Unable to Verify",
      evidence: unread || noPolicy ? "The policy text was not read." : "The policy analysis does not record a policy's dates, so the scan cannot say whether they are shown.", evidenceKind: "Not captured",
      fix: "To verify: open the notice and look for an effective date and a last-updated date near the top or the bottom.", risk: 1 });
  }

  // ── Page 4: account settings ────────────────────────────────────────────────────────────────────────────────────────────────
  {
    const p = pages[3];
    existence(p, "account settings or profile page (and, failing that, a contact form)", "If the service has accounts, give each person a settings page for consent, their data, deletion and nomination.");
    const why = account
      ? "An account area was found, but it is behind a login and the scan does not sign in, so what it offers was not tested."
      : p.found ? "No account area was found; this is the contact form's page, not an account page. Account features can only be tested after signing in." : "No account area was found, so there was nothing to test.";
    const sup = (id: string, label: string) => { const r = policyRow(policy, id); return r && policyRead(policy) ? ` The privacy policy, separately, says this: ${label} ${r.status === "verified" ? "quoted" : r.status === "partial" ? "partly stated" : "not found"}.` : ""; };
    const four = (check: string, sections: string, id: string, label: string, how: string) =>
      add({ pageN: 4, sections, check, status: "Unable to Verify", evidence: `${why}${sup(id, label)}`, evidenceKind: "Not captured", fix: `To verify: ${how}`, risk: 3, scored: false });
    four("The person can withdraw consent", ref("DPDP Act s.6(4)"), "consent_withdrawal", "the means to withdraw consent", "sign in with a test account and confirm there is a setting or link to withdraw each consent.");
    four("The person can edit or correct their data", ref("DPDP Act s.12", "DPDP Act s.8(3)"), "rights_correction_erasure", "the right to correction", "sign in and confirm profile data can be edited and saved.");
    four("The person can view a summary of the data held about them", ref("DPDP Act s.11"), "rights_access", "the right to a summary", "sign in and look for a download or summary of the data held.");
    four("The person can delete their account", ref("DPDP Act s.12", "DPDP Act s.8(7)"), "rights_correction_erasure", "the right to erasure", "sign in and confirm an account-deletion option exists and works.");
    four("The person can nominate another person", ref("DPDP Act s.14"), "rights_nominate", "the right to nominate", "sign in and look for a way to name another person to exercise rights.");
  }

  // ── Page 5: grievance / rights ──────────────────────────────────────────────────────────────────────────────────────────────
  {
    const p = pages[4];
    existence(p, "grievance or rights page (and, failing that, Contact Us)", "Publish a page that says how to raise a grievance or exercise a right, who handles it, and how fast it is answered.");
    const where = formsOn(p.url);
    const fs = where.forms;
    const named = notScannedNote(p);
    const dpo = policyRow(policy, "dpo_contact");
    const fromPolicy = (id: string, pageN: number, check: string, sections: string, risk: number, fixIfNot: string, extra = ""): void => {
      const r = policyRow(policy, id);
      let status: ReadinessStatus;
      let evidence: string;
      let kind: EvidenceKind = "Quoted";
      if (!policyRead(policy)) { status = "Unable to Verify"; evidence = policyWhy + " The grievance page's own text is not kept by the scan."; kind = "Not captured"; }
      else if (!r || r.status === "not_applicable") { status = "Unable to Verify"; evidence = "Not assessed."; kind = "Not captured"; }
      else if (r.status === "verified") { status = "Passes this technical check"; evidence = `Quoted from the policy: “${r.quote}”${r.source_url ? ` (${r.source_url})` : ""}.${extra}`; }
      else if (r.status === "partial") { status = "Gap"; evidence = `Partly stated: “${r.quote}”${r.source_url ? ` (${r.source_url})` : ""}.${extra}`; }
      else { status = "Not Found"; evidence = `Not found in the policy text. The grievance page's own text is not kept by the scan, so a statement only there was not seen.${extra}`; kind = "Not seen"; }
      add({ pageN, sections, check, status, evidence, evidenceKind: kind, fix: status === "Passes this technical check" ? "No fix needed on what the scan read." : fixIfNot, risk });
    };
    const formNote = where.located ? (fs.length ? ` A form on this page asks for: ${listText([...new Set(fs.flatMap((f) => f.fields).map(fieldLabel))])}.` : " No form was recorded on this page.") : "";
    fromPolicy("rights_means", 5, "A request process or form is published for each right", ref("DPDP Act s.11", "DPDP Act s.12", "DPDP Act s.13", "DPDP Act s.14", "DPDP Rules r.14"), 5, "Publish a form or an address for each right (summary, correction and erasure, nomination, grievance) and say how to use it.", `${formNote}${named ? ` ${named}` : ""}`);
    const idFields = fs.flatMap((f) => f.fields).filter((f) => /e-?mail|phone|mobile|customer|account|user[-_ ]?id|order/i.test(f.name) || f.field_type === "email" || f.field_type === "tel");
    add({ pageN: 5, sections: ref("DPDP Rules r.14"), check: "It states which identifier is needed to make a request (email, mobile, customer ID)", status: idFields.length ? "Passes this technical check" : "Unable to Verify",
      evidence: idFields.length ? `A form on this page asks for: ${listText([...new Set(idFields.map(fieldLabel))])}. (Seen from the form's fields; the wording around it is not kept.)` : `${p.found ? (where.located ? "No form asking for an identifier was recorded on this page. " : "The scan did not record which page each form was on. ") : "No page to check. "}The page's text is not kept by the scan, so a stated identifier was not seen.`,
      evidenceKind: idFields.length ? "Observed" : "Not captured",
      fix: idFields.length ? "No fix needed on what the scan saw." : "To verify: read the page and confirm it says which identifier (email, mobile number or customer ID) a request needs.", risk: 2 });
    fromPolicy("grievance_timeline", 5, "A response period of 90 days or less is stated", ref("DPDP Rules r.14(3)"), 4, "State the period within which grievances are answered, and keep it to 90 days or less.");
    {
      const g = policyRow(policy, "grievance_contact");
      const d = dpo;
      const best = g?.status === "verified" ? g : d?.status === "verified" ? d : g ?? d;
      let status: ReadinessStatus; let evidence: string; let kind: EvidenceKind = "Quoted";
      if (!policyRead(policy)) { status = "Unable to Verify"; evidence = "The privacy policy analysis was not available, and the grievance page's own text is not kept by the scan."; kind = "Not captured"; }
      else if (best?.status === "verified") { status = "Passes this technical check"; evidence = `Quoted from the policy: “${best.quote}”${best.source_url ? ` (${best.source_url})` : ""}.${best.contacts ? ` Contact given: ${[...best.contacts.emails, ...best.contacts.phones, ...best.contacts.urls].join(" · ")}.` : ""}`; }
      else if (best?.status === "partial") { status = "Gap"; evidence = `A role is named without a contact beside it: “${best.quote}”${best.source_url ? ` (${best.source_url})` : ""}.`; }
      else { status = "Not Found"; evidence = "No named grievance officer, Data Protection Officer or contact person was found in the policy text. The grievance page's own text is not kept by the scan."; kind = "Not seen"; }
      add({ pageN: 5, sections: ref("DPDP Act s.8(9)", "DPDP Act s.8(10)", "DPDP Rules r.9"), check: "A named Data Protection Officer or contact person is published", status, evidence, evidenceKind: kind,
        fix: status === "Passes this technical check" ? "No fix needed on what the scan read." : "Publish the name or title and a business contact (email or phone) of the person who handles grievances and questions.", risk: 4 });
    }
  }

  // ── what a website cannot show ──────────────────────────────────────────────────────────────────────────────────────────────
  add({ pageN: 0, sections: ref("DPDP Act s.8(6)", "DPDP Rules r.7"), check: "Personal data breach reporting", status: "Requires document review", evidence: "Breach detection and the report to the Board and each person are internal processes; no website shows them.", evidenceKind: "Not captured", fix: "Review the incident-response plan against the breach-reporting timelines.", risk: 3, scored: false });
  add({ pageN: 0, sections: ref("DPDP Act s.10"), check: "Data protection impact assessment and audit for a Significant Data Fiduciary", status: "Requires document review", evidence: "These are internal records and apply only to a notified Significant Data Fiduciary.", evidenceKind: "Not captured", fix: "If notified, review the impact assessments and audits on file.", risk: 2, scored: false });
  add({ pageN: 0, sections: ref("DPDP Act s.8(7)", "DPDP Rules r.8"), check: "Internal retention schedules and erasure practice", status: "Requires document review", evidence: "What the notice says can be read; whether data is actually erased on schedule is shown by internal records only.", evidenceKind: "Not captured", fix: "Review retention schedules and erasure logs against what the notice states.", risk: 3, scored: false });

  // ── a framed wrapper (rule R-012) ──────────────────────────────────────────────────────────────────────────────────────────
  // The scanned address only displays another site, so what the scan saw is the wrapper page. What it SAW stands (a tracker that fired, a form, a quoted
  // policy sentence). A row that rests on NOTHING being seen (no banner, no page, no policy link, a clean "nothing fired") says nothing about the framed site, so
  // it becomes Unable to Verify, is not scored, and says why. This is the same rule the compliance score applies, requirement by requirement
  // (backend scoring/dpdp_score.py `_framed_limit`).
  const framed = framedSiteOf(evidence);
  if (framed) {
    const note = `The scanned address only displays another site (${framed}) in a frame, so this describes the wrapper page, not the site visitors see (rule R-012).`;
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      if ((r.status === "Passes this technical check" || r.status === "Not Found") && r.evidenceKind === "Not seen") {
        rows[i] = { ...r, status: "Unable to Verify", evidenceKind: "Not captured", scored: false, evidence: `${note} ${r.evidence}`, fix: `To verify: scan ${framed} directly.` };
      }
    }
    for (const page of pages) if (!page.found) page.why = `${page.why} The scanned address is a wrapper, so the framed site (${framed}) may have this page; it was not scanned.`;
  }

  // ── the summary ────────────────────────────────────────────────────────────────────────────────────────────────────────────
  const counts: Record<ReadinessStatus, number> = { "Passes this technical check": 0, Gap: 0, "Not Found": 0, "Unable to Verify": 0, "Requires document review": 0 };
  for (const r of rows) counts[r.status] += 1;
  const scored = rows.filter((r) => r.scored && (r.status === "Passes this technical check" || r.status === "Gap" || r.status === "Not Found"));
  const assessed = scored.length;
  const ok = scored.filter((r) => r.status === "Passes this technical check").length;
  // The compliance score is withheld below two assessed requirements (backend MIN_ASSESSED_REQUIREMENTS); a one-check "100 out of 100" would be the same misleading number.
  const scorePercent = assessed >= MIN_ASSESSED_CHECKS ? Math.round((ok / assessed) * 100) : null;
  const gapsOf = rows.filter((r) => r.scored && (r.status === "Gap" || r.status === "Not Found")).map((r, i) => ({ r, i }))
    .sort((a, b) => b.r.risk - a.r.risk || a.r.pageN - b.r.pageN || a.i - b.i);
  const topGaps: GapSummary[] = gapsOf.slice(0, 5).map(({ r }, i) => ({ rank: i + 1, row: r, riskLabel: r.risk >= 4 ? "High" : r.risk === 3 ? "Medium" : "Low" }));
  const byPage = new Map<number, number>();
  for (const { r } of gapsOf) byPage.set(r.pageN, (byPage.get(r.pageN) ?? 0) + r.risk);
  const topPage = [...byPage.entries()].sort((a, b) => b[1] - a[1] || a[0] - b[0])[0];
  const riskiest = topPage
    ? (() => {
        const mine = gapsOf.filter(({ r }) => r.pageN === topPage[0]).map(({ r }) => r);
        const page = pages[topPage[0] - 1];
        return { n: topPage[0], page: shortName(page.role), why: `${mine.length} gap${mine.length === 1 ? "" : "s"} here carry the most weight, led by: ${mine.slice(0, 2).map((r) => r.check.replace(/\.$/, "")).join("; ")}.` };
      })()
    : null;

  return {
    ...empty, available: true, pages, rows, counts, assessed, scorePercent,
    scoreLine: scorePercent === null
      ? "Not enough could be verified from the website to give a five-page review score."
      : `${scorePercent} out of 100: ${ok} of the ${assessed} checks the scan could assess are met (${counts.Gap} gap${counts.Gap === 1 ? "" : "s"}, ${counts["Not Found"]} not found). The ${counts["Unable to Verify"]} it could not verify and the ${counts["Requires document review"]} that need document review are not counted.`,
    topGaps, riskiest,
    notes: [
      ...(framed ? [`The scanned address is a wrapper that displays ${framed}. Checks that rest on something not being seen are Unable to Verify and are not scored, as in the compliance score; scan ${framed} directly to assess that site.`] : []),
      "This is a separate five-page reading, not the DPDP Compliance Score. It does not change that score, which weighs every check by its own weight and is the report's headline.",
      "Each check counts equally. A page that does not exist is a finding, not a failed check, except the privacy notice. Only what the scan saw or the policy's own words say is marked Passes this technical check; what could not be seen is marked Unable to Verify, never Passes this technical check.",
      "Plain language, the wording beside a form, and a footer's text are not kept by the scan, so they are marked Unable to Verify rather than judged.",
    ],
  };
}

/** The short page name used in the findings table. */
function shortName(role: string): string {
  if (/^Sign-up|^Checkout|^Lead/.test(role)) return role.startsWith("Sign-up") ? "Sign-up" : role.startsWith("Checkout") ? "Checkout" : "Lead form";
  if (role.startsWith("Homepage")) return "Homepage";
  if (role.startsWith("Privacy")) return "Privacy notice";
  if (role.startsWith("Account")) return "Account settings";
  if (role.startsWith("Grievance")) return "Grievance / rights";
  return role;
}

export const READINESS_STATUS_TONE: Record<ReadinessStatus, "good" | "bad" | "warn" | "mute"> = {
  "Passes this technical check": "good", Gap: "bad", "Not Found": "bad", "Unable to Verify": "warn", "Requires document review": "mute",
};
