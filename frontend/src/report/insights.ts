import type { PolicyAnalysis, PolicyRequirementResult, ScanEvidenceResponse, SectorAssessment, WebsiteScanMetadata } from "../api/types";
import { formatRefs } from "./legalRefs";

// The dedicated sections of the report that sit beside the score: the grievance officer, the Data Protection Officer, Indian-language support,
// children's data and the age gate, and observable security signals. One reading of each, used by the dashboard and the PDF alike.
//
// Each is built from what the scan actually recorded and says what KIND of knowledge it is:
//   Confirmed     the scan measured it, or the policy's own words (quoted exactly) say it.
//   Observed      the scan saw it; seeing it does not prove more than it shows.
//   Not verified  the scan did not see it; that is not evidence that it does not exist.
// An absence is never reported as a violation, and nothing here changes the DPDP Compliance Score.

export type Knowledge = "Confirmed" | "Observed" | "Not verified";
export type Tone = "good" | "warn" | "mute";

export interface Fact {
  label: string;
  value: string;
  tone: Tone;
}

export interface InsightSection {
  key: "grievance" | "dpo" | "language" | "children" | "security";
  title: string;
  /** One line: where things stand. */
  status: string;
  tone: Tone;
  /** What kind of knowledge the status rests on. */
  knowledge: Knowledge;
  summary: string;
  facts: Fact[];
  /** The policy's own words, exactly, and the page they are on. */
  quote: { text: string; source: string | null } | null;
  /** What the scan cannot tell. */
  notes: string[];
  legal: string;
}

const row = (a: PolicyAnalysis | null | undefined, id: string): PolicyRequirementResult | undefined => a?.requirements?.find((r) => r.id === id);

/** Whether the policy analysis ran and produced points to read. */
function policyAvailable(a: PolicyAnalysis | null | undefined): a is PolicyAnalysis {
  return !!a && a.status === "analysed" && a.requirements.length > 0;
}

function unavailableReason(a: PolicyAnalysis | null | undefined): string {
  return !a || a.status === "not_run" ? "The privacy policy analysis was not run for this scan." : a.reason ?? "The privacy policy analysis is not available for this scan.";
}

function contactsText(r: PolicyRequirementResult | undefined): string {
  const c = r?.contacts;
  if (!c) return "";
  return [...c.emails, ...c.phones, ...c.urls].join(" · ");
}

function statusFact(label: string, r: PolicyRequirementResult | undefined, found: string, partial: string, missing: string): Fact {
  if (!r) return { label, value: "Not assessed", tone: "mute" };
  if (r.status === "verified") return { label, value: found, tone: "good" };
  if (r.status === "partial") return { label, value: partial, tone: "warn" };
  if (r.status === "not_applicable") return { label, value: "Not applicable", tone: "mute" };
  return { label, value: missing, tone: "warn" };
}

function statusFor(r: PolicyRequirementResult | undefined): { tone: Tone; knowledge: Knowledge } {
  if (!r || r.status === "not_applicable") return { tone: "mute", knowledge: "Not verified" };
  if (r.status === "verified") return { tone: "good", knowledge: "Confirmed" };
  // A partial statement is confirmed as far as it goes; a point not found has no confirmation at all.
  return { tone: "warn", knowledge: r.status === "partial" ? "Confirmed" : "Not verified" };
}

// ─── grievance officer ────────────────────────────────────────────────────────────────────────────────────────────────────────────

export function buildGrievanceSection(policy: PolicyAnalysis | null | undefined): InsightSection {
  const base = { key: "grievance" as const, title: "Grievance officer", notes: [] as string[] };
  if (!policyAvailable(policy)) {
    return { ...base, status: "Not assessed", tone: "mute", knowledge: "Not verified", summary: unavailableReason(policy), facts: [], quote: null,
      legal: formatRefs(["DPDP Act s.13(1)", "DPDP Act s.8(10)", "DPDP Rules r.14(3)"]), notes: ["Without the policy text the scan cannot say who handles grievances."] };
  }
  const contact = row(policy, "grievance_contact");
  const timeline = row(policy, "grievance_timeline");
  const board = row(policy, "complaint_to_board");
  const { tone, knowledge } = statusFor(contact);
  const days = timeline?.response_days ?? null;
  const facts: Fact[] = [
    statusFact("Grievance contact", contact, contactsText(contact) || "Stated in the policy (see the quotation)", "A grievance officer is named, but no email, phone number or form is given beside the name", "Not found in the policy text"),
    timeline?.status === "verified"
      ? { label: "Response period", value: days !== null ? `Within about ${Number.isInteger(days) ? days : days.toFixed(1)} day${days === 1 ? "" : "s"}; the Rules allow up to ninety` : "Stated in the policy (see the quotation)", tone: "good" }
      : statusFact("Response period", timeline, "", timeline?.detail ?? "A period is mentioned but not within the ninety days the Rules allow", "No response period found in the policy text"),
    statusFact("Complaint to the Data Protection Board", board, "Explained in the policy", "Mentioned, without how to make it", "Not found in the policy text"),
  ];
  const first = [contact, timeline, board].find((r) => r?.quote);
  return {
    ...base,
    status: !contact ? "Not assessed" : contact.status === "verified" ? "Grievance contact found" : contact.status === "partial" ? "Grievance officer named, no contact beside the name" : "No grievance contact found in the policy text",
    tone, knowledge,
    summary: contact?.status === "verified"
      ? `The policy names who handles grievances and how to reach them${timeline?.status === "verified" ? ", and says how quickly they are answered" : ""}.`
      : "The Act and Rules require a grievance system to be published, with a response period of at most ninety days.",
    facts,
    quote: first?.quote ? { text: first.quote, source: first.source_url } : null,
    legal: formatRefs([...new Set([...(contact?.provisions ?? []), ...(timeline?.provisions ?? []), ...(board?.provisions ?? [])])]),
    notes: ["The scan reads the published policy; it does not test whether the contact answers or whether grievances are resolved in the stated time."],
  };
}

// ─── Data Protection Officer ──────────────────────────────────────────────────────────────────────────────────────────────────────

export function buildDpoSection(policy: PolicyAnalysis | null | undefined): InsightSection {
  const base = { key: "dpo" as const, title: "Data Protection Officer", notes: [] as string[] };
  const legal = formatRefs(["DPDP Act s.8(9)", "DPDP Act s.10(2)(a)"]);
  const rule = "A Data Protection Officer is required only of a Significant Data Fiduciary notified by the Central Government (Section 10(2)(a)). Every data fiduciary must publish the business contact of a Data Protection Officer or of a person who can answer questions about the processing (Section 8(9)).";
  if (!policyAvailable(policy)) {
    return { ...base, status: "Not assessed", tone: "mute", knowledge: "Not verified", summary: unavailableReason(policy), facts: [], quote: null, legal, notes: [rule] };
  }
  const r = row(policy, "dpo_contact");
  const { tone, knowledge } = statusFor(r);
  return {
    ...base,
    status: !r ? "Not assessed" : r.status === "verified" ? "Contact for a Data Protection Officer or answering person found" : r.status === "partial" ? "A role is named, with no contact beside it" : "No Data Protection Officer or answering person found in the policy text",
    tone, knowledge,
    summary: r?.status === "verified" ? "The policy gives a business contact for questions about how personal data is handled." : "The policy text was searched for the business contact of a Data Protection Officer or of a person who can answer questions.",
    facts: [statusFact("Business contact", r, contactsText(r) || "Stated in the policy (see the quotation)", "A role is named, but no email, phone number or address is given beside it", "Not found in the policy text")],
    quote: r?.quote ? { text: r.quote, source: r.source_url } : null,
    legal,
    notes: [rule, "Whether the organisation has been notified as a Significant Data Fiduciary cannot be seen from a website, so the scan does not assess it (it is a declared applicability, left out of the score unless declared)."],
  };
}

// ─── Indian-language support ──────────────────────────────────────────────────────────────────────────────────────────────────────

/** The language codes of the Eighth Schedule languages (the part of an hreflang before any region). */
const EIGHTH_SCHEDULE = new Set(["as", "bn", "brx", "doi", "gu", "hi", "kn", "ks", "kok", "mai", "ml", "mni", "mr", "ne", "or", "pa", "sa", "sat", "sd", "ta", "te", "ur"]);

export function buildLanguageSection(meta: WebsiteScanMetadata | null | undefined, policy: PolicyAnalysis | null | undefined): InsightSection {
  const legal = formatRefs(["DPDP Act s.5(3)"]);
  const sig = meta?.language_signals;
  const notice = row(policy, "notice_language");
  const noticeFact: Fact = policyAvailable(policy)
    ? statusFact("The policy says the notice is available in other languages", notice, "Yes (see the quotation)", "Partly", "Not found in the policy text")
    : { label: "The policy says the notice is available in other languages", value: "Not assessed", tone: "mute" };
  const caution = "A page can offer other languages behind a choice the scan does not click, and a notice in another language can exist even where the pages show none. So an absence here is not evidence that the option is missing. This section does not change the score.";
  if (!sig) {
    return { key: "language", title: "Indian-language support", status: "Not recorded for this scan", tone: "mute", knowledge: "Not verified",
      summary: "This scan was made before the pages' languages were recorded, so nothing can be said about them.", facts: [noticeFact],
      quote: notice?.quote ? { text: notice.quote, source: notice.source_url } : null, legal, notes: [caution] };
  }
  const codes = sig.hreflang.filter((h) => EIGHTH_SCHEDULE.has(h.toLowerCase().split("-")[0]));
  const scripts = Object.entries(sig.indic_scripts).sort((a, b) => b[1] - a[1]);
  const observed = sig.pages_with_indic_script > 0 || sig.switcher_scripts.length > 0 || codes.length > 0;
  const facts: Fact[] = [
    { label: "Pages checked", value: String(sig.pages_checked), tone: "mute" },
    { label: "Pages with text in an Indian script", value: `${sig.pages_with_indic_script} of ${sig.pages_checked}`, tone: sig.pages_with_indic_script > 0 ? "good" : "mute" },
    { label: "Scripts seen", value: scripts.length ? scripts.map(([n, c]) => `${n} (${c.toLocaleString("en")} characters)`).join("; ") : "None", tone: scripts.length ? "good" : "mute" },
    { label: "A link or button in an Indian script (a language chooser)", value: sig.switcher_scripts.length ? sig.switcher_scripts.join("; ") : "None seen", tone: sig.switcher_scripts.length ? "good" : "mute" },
    { label: "Alternate-language links declared (hreflang)", value: sig.hreflang.length ? sig.hreflang.join(", ") : "None", tone: codes.length ? "good" : "mute" },
    { label: "Language the homepage declares", value: sig.html_lang ?? "Not declared", tone: "mute" },
    noticeFact,
  ];
  return {
    key: "language", title: "Indian-language support",
    status: observed ? "Indian-language content observed" : "No Indian-language content observed",
    tone: observed ? "good" : "mute", knowledge: observed ? "Observed" : "Not verified",
    summary: observed
      ? "The scanned pages carry text, links or declared alternates in the scripts and languages of India's Eighth Schedule. Seeing them does not show that the privacy notice itself is offered in those languages."
      : "The scanned pages carried no text in an Indian script and declared no Indian-language alternates. The Act gives a person the option to read the notice in English or any Eighth Schedule language (Section 5(3)).",
    facts, quote: notice?.quote ? { text: notice.quote, source: notice.source_url } : null, legal, notes: [caution],
  };
}

// ─── children's data and the age gate ────────────────────────────────────────────────────────────────────────────────────────────

const CHILD_FIELD = /parent|guardian|date[_ -]?of[_ -]?birth|\bdob\b|birth|\bage\b|\bgrade\b|\bclass\b|student|ward|child|children|infant|\bkids?\b|minor/i;

function childFields(evidence: ScanEvidenceResponse | null | undefined): string[] {
  const seen: string[] = [];
  for (const f of evidence?.forms ?? []) {
    for (const fld of f.fields) {
      const name = fld.name ?? "";
      if (name && CHILD_FIELD.test(name.replace(/[-_]/g, " ")) && !seen.includes(name)) seen.push(name);
    }
  }
  return seen;
}

/** The age gate a consent tool showed before its banner, if the scan recognised one: what it was and how it was answered. */
function ageGate(evidence: ScanEvidenceResponse | null | undefined): string | null {
  for (const s of evidence?.consent_signals ?? []) {
    const gate = (s.evidence as Record<string, unknown> | undefined)?.consent_gate as Record<string, { kind?: string; cmp?: string; answer?: string; outcome?: string } | null> | undefined;
    const g = gate?.post_accept ?? gate?.post_reject;
    if (g && g.kind) return `${String(g.kind).replace(/_/g, " ")} from ${g.cmp ?? "a consent tool"}; the scanner answered as an ${g.answer ?? "adult"} visitor (outcome: ${String(g.outcome ?? "not recorded").replace(/_/g, " ")})`;
  }
  return null;
}

export function buildChildrenSection(
  sector: SectorAssessment | null | undefined, evidence: ScanEvidenceResponse | null | undefined, policy: PolicyAnalysis | null | undefined,
): InsightSection {
  const fields = childFields(evidence);
  const gate = ageGate(evidence);
  const policyRow = row(policy, "children_data");
  const applies = !!sector?.declarations?.some((d) => d.flag === "children") || fields.length > 0;
  const facts: Fact[] = [
    { label: "Sector reading", value: sector ? `${sector.label}${sector.confidence === "declared" ? " (declared)" : ` (${sector.confidence} confidence)`}` : "Not available", tone: "mute" },
    { label: "Form fields that suggest children's data", value: fields.length ? fields.join(", ") : "None seen", tone: fields.length ? "warn" : "mute" },
    { label: "Age gate before the consent banner", value: gate ?? "None recognised. The scan only recognises age gates shown by consent tools it knows, so this is not proof there is none", tone: gate ? "good" : "mute" },
    policyAvailable(policy)
      ? (policyRow?.status === "not_applicable"
        ? { label: "The policy's statement on children's data", value: "Not assessed: no sign the site is aimed at children", tone: "mute" as Tone }
        : statusFact("The policy's statement on children's data", policyRow, "Stated (see the quotation)", "Partly stated", "Not found in the policy text"))
      : { label: "The policy's statement on children's data", value: "Not assessed", tone: "mute" },
  ];
  return {
    key: "children", title: "Children's data and age gate",
    status: applies ? "Children's data may be involved" : "Not applicable on the evidence",
    tone: applies ? "warn" : "mute",
    knowledge: applies ? "Observed" : "Not verified",
    summary: applies
      ? "The sector or the forms suggest children may use this service. Before processing a child's data a verifiable parent's or guardian's consent is needed, and tracking, behavioural monitoring and advertising aimed at children are barred."
      : "Nothing in the site's sector, its forms or its consent tool suggests it is aimed at, or collects data from, children, so this duty is not assessed. The scan cannot see who actually uses a service.",
    facts, quote: policyRow?.quote ? { text: policyRow.quote, source: policyRow.source_url } : null,
    legal: `${formatRefs(["DPDP Act s.9(1)", "DPDP Act s.9(3)", "DPDP Rules r.10"])}; exemptions: ${formatRefs(["DPDP Act s.9(4)", "DPDP Rules r.12, Fourth Schedule Part A"])}`,
    notes: ["The exemptions apply only to the entities and purposes the Fourth Schedule names (for example clinical establishments and educational institutions, for the processing it restricts them to). They are a fact about the organisation, which the scan cannot see."],
  };
}

// ─── observable security signals ──────────────────────────────────────────────────────────────────────────────────────────────

const HEADER_LABELS: Record<string, string> = {
  hsts: "Strict-Transport-Security (HSTS)",
  csp: "Content-Security-Policy",
  x_frame_options: "Clickjacking protection (X-Frame-Options or CSP frame-ancestors)",
  x_content_type_options: "X-Content-Type-Options",
  referrer_policy: "Referrer-Policy",
  permissions_policy: "Permissions-Policy",
};

/** Which page the header and HTTPS signals describe. They are read from the entered address, so when that is the bare front door it IS the homepage; when the scan started from a deeper address
 *  it is that page, named, and calling it "the homepage" would put another page's headers on the homepage. With no address known, the older wording is kept. */
export function securityPageName(siteUrl: string | null | undefined): { onThe: string; title: string } {
  if (!siteUrl) return { onThe: "the homepage", title: "Homepage" };
  let host = siteUrl;
  let path = "";
  try {
    const u = new URL(/^https?:\/\//.test(siteUrl) ? siteUrl : `https://${siteUrl}`);
    host = u.hostname;
    path = `${u.pathname === "/" ? "" : u.pathname}${u.search}`;
  } catch { /* keep the text as given */ }
  if (!path) return { onThe: `the homepage (${host})`, title: "Homepage" };
  return { onThe: `the entered page (${host}${path})`, title: `Entered page (${host}${path})` };
}

export function buildSecuritySection(meta: WebsiteScanMetadata | null | undefined, evidence: ScanEvidenceResponse | null | undefined, siteUrl?: string | null): InsightSection {
  const sig = meta?.security_signals;
  const page = securityPageName(siteUrl);
  const facts: Fact[] = [];
  const notRecorded = (label: string): Fact => ({ label, value: "Not recorded for this scan", tone: "mute" });
  let observed = 0;
  let counted = 0;
  const signal = (label: string, ok: boolean, yes: string, no: string) => {
    counted += 1;
    if (ok) observed += 1;
    facts.push({ label, value: ok ? yes : no, tone: ok ? "good" : "warn" });
  };
  if (sig) {
    signal("Scanned pages served over HTTPS", sig.pages_total > 0 && sig.pages_https === sig.pages_total, `${sig.pages_https} of ${sig.pages_total}`, `${sig.pages_https} of ${sig.pages_total}: not all`);
    if (sig.homepage_https !== undefined) signal(`${page.title} served over HTTPS`, sig.homepage_https, "Yes", "No");
    const h = sig.homepage_headers;
    if (h) {
      for (const key of Object.keys(HEADER_LABELS) as (keyof typeof h)[]) {
        const present = key === "x_frame_options" ? !!h.x_frame_options || /frame-ancestors/i.test(h.csp ?? "") : !!h[key];
        signal(HEADER_LABELS[key], present, `Observed on ${page.onThe}`, `Not observed on ${page.onThe}`);
      }
    }
  } else {
    for (const l of ["Scanned pages served over HTTPS", `${page.title} served over HTTPS`, ...Object.values(HEADER_LABELS)]) facts.push(notRecorded(l));
  }
  const cookies = evidence?.cookies ?? [];
  if (cookies.length) {
    const have = (pick: (c: (typeof cookies)[number]) => boolean | string | null | undefined) => cookies.filter((c) => pick(c) !== null && pick(c) !== undefined);
    const secure = have((c) => c.secure), http = have((c) => c.http_only), same = have((c) => c.same_site);
    facts.push({
      label: "Cookie attributes recorded", tone: "mute",
      value: [`Secure: ${secure.filter((c) => c.secure).length} of ${secure.length}`, `HttpOnly: ${http.filter((c) => c.http_only).length} of ${http.length}`, `SameSite set: ${same.filter((c) => !!c.same_site).length} of ${same.length}`].join(" · ")
        + ". Cookies that analytics scripts set cannot be HttpOnly, so a lower count is expected.",
    });
  }
  const allForms = evidence?.forms ?? [];
  const forms = allForms.filter((f) => f.submit_url !== undefined);
  if (allForms.length) {
    if (forms.length === 0) facts.push(notRecorded("Forms post over HTTPS"));
    else {
      const insecure = forms.filter((f) => (f.submit_url ?? "").toLowerCase().startsWith("http://"));
      signal("Forms post over HTTPS", insecure.length === 0, `All ${forms.length} post over HTTPS or to their own page`, `${insecure.length} of ${forms.length} post to an unencrypted address`);
    }
  }
  const all = sig && counted > 0 && observed === counted;
  return {
    key: "security", title: "Observable security signals",
    status: !sig ? "Not recorded for this scan" : `${observed} of ${counted} signals observed`,
    tone: !sig ? "mute" : all ? "good" : "warn", knowledge: sig ? "Observed" : "Not verified",
    summary: sig
      ? "What a visitor's browser can see of the site's transport and safeguards: whether pages are served over HTTPS, and which protective headers ${page.onThe} sends."
      : "This scan was made before these signals were recorded, so nothing can be said about them.",
    facts, quote: null,
    legal: formatRefs(["DPDP Act s.8(5)", "DPDP Rules r.6"]),
    notes: [
      "The Act and Rules require reasonable security safeguards. Most of them (access control, encryption at rest, logging, backups, staff training) are inside the organisation and cannot be seen from outside.",
      "A signal not observed is not a finding that the safeguard is missing, and one observed is not a finding that the safeguards are adequate. This section does not change the score.",
    ],
  };
}

export function buildInsightSections(args: {
  policy: PolicyAnalysis | null | undefined; meta: WebsiteScanMetadata | null | undefined; evidence: ScanEvidenceResponse | null | undefined; sector: SectorAssessment | null | undefined;
  /** The address that was entered: the security signals describe that page. */
  siteUrl?: string | null;
}): InsightSection[] {
  return [
    buildGrievanceSection(args.policy), buildDpoSection(args.policy), buildLanguageSection(args.meta, args.policy),
    buildChildrenSection(args.sector, args.evidence, args.policy), buildSecuritySection(args.meta, args.evidence, args.siteUrl),
  ];
}

/** Shared with the five-page readiness review (report/readiness.ts). */
export const ageGateSeen = ageGate;
export const childFieldNames = childFields;
