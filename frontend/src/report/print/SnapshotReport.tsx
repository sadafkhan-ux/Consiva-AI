import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { ReportModel } from "../buildReport";
import { CONSIVA_LOGO_PNG } from "../consivaLogo";
import { buildSummaryReport, SIGNUP_SUB, SIGNUP_TEXT, SIGNUP_URL, type SummaryView } from "../summaryReport";
import { KIND_LABEL, type TenAssessment, type TenCheck, type TenResult } from "../tenChecks";
import { BigRing, Icon, Pill, SheetPageStyle, type IconName } from "./PageParts";
import "./pages.css";
import "./snapshot.css";

/** The two-page "Website Privacy Readiness Report" (variant "summary"), laid out as the approved reference: page 1 is the website overview, page 2 the detailed findings. */
export const SNAPSHOT_COOKIES = 5;
/** How far a page may be tightened (spacing, then type) until its content fits the sheet. */
export const SNAPSHOT_MAX_FIT = 6;
/** One sheet, in CSS pixels: 296.6mm at 96 dpi, the height the print rule in pages.css gives it (just under A4, so it never spills onto a blank page). */
const PAGE_HEIGHT = (296.6 * 96) / 25.4;
/** True on the page the server prints (/?report=<id>&print=1). */
const PRINTING = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("print") === "1";
/** A sheet is 794px wide (210mm at 96 dpi). */
const PAGE_WIDTH = 794;
/** The disclaimer printed at the foot of page 2, exactly as worded. */
export const SNAPSHOT_DISCLAIMER =
  "Consiva AI's Website Privacy Snapshot is a limited, browser-based review of up to five publicly accessible website pages, intended solely for awareness, education and preliminary privacy benchmarking. The requester represents that they own the submitted domain or have obtained the necessary authorisation. Findings and proprietary scores are indicative, time-specific and subject to technical limitations. They do not constitute legal advice, a legal or regulatory determination, compliance certification, comprehensive privacy audit or security assessment. The requester is responsible for ensuring appropriate authorisation. Consiva AI remains responsible for its own service operations and obligations under applicable law.";

const ELEMENT_ORDER: { key: string; label: string; icon: IconName }[] = [
  { key: "Privacy policy", label: "Privacy Policy", icon: "doc" },
  { key: "Terms of use", label: "Terms of Use", icon: "form" },
  { key: "Cookie policy", label: "Cookie Policy", icon: "cookie" },
  { key: "Consent banner", label: "Consent Banner", icon: "board" },
  { key: "Forms", label: "Forms", icon: "form" },
  { key: "Per-purpose choices", label: "Per-purpose Choices", icon: "sliders" },
];
const TILE_ICON: IconName[] = ["nodes", "globe", "bars", "cookie"];
const CONSENT_FIELD = /consent|agree|accept|privacy|terms|gdpr|dpdp|policy/i;
/** Wording that asks for agreement: what a consent checkbox says. Read from the checkbox's own label, not guessed from its name. */
const CONSENT_WORDS = /\b(agree|accept|consent|authori[sz]e|permit|allow|subscribe|opt[- ]?in|receive|terms|privacy|policy)\b/i;

/** A URL as printed: no scheme, no trailing slash, and cut with an ellipsis when longer than `max` (the full address stays in the scan's evidence). */
const short = (url: string | null | undefined, max = 80): string => {
  const u = url ? url.replace(/^https?:\/\//, "").replace(/\/$/, "") : "";
  return u.length > max ? `${u.slice(0, max - 1)}…` : u;
};

/** `found` is true or false, null when the evidence was not loaded, or "checkbox": a checkbox was recorded in a form that collects personal data, but the scan does not record what the checkbox says. */
type Item = { label: string; found: boolean | null | "checkbox"; source: string | null; wording?: string | null };

/** What the forms and the consent choices the scan found, each with the page it was found on. Nothing is inferred beyond the scan's own records. */
function formsAndConsent(model: ReportModel, s: SummaryView): Item[] {
  const ev = model.evidence;
  const home = s.page.preview.address ?? null;
  const per = s.elements.find((e) => e.title === "Per-purpose choices");
  if (!ev) return [
    { label: "Contact / Enquiry form", found: null, source: null },
    { label: "Explicit consent (e.g. checkbox)", found: null, source: null },
    { label: "Consent purpose (choices per purpose)", found: null, source: null },
  ];
  const forms = ev.forms ?? [];
  const isBox = (x: { field_type: string }) => /checkbox/i.test(x.field_type);
  // What a checkbox says (its recorded label) decides first; its name is only a fallback for a scan that did not record the wording.
  const worded = forms.flatMap((f) => f.fields.filter((x) => isBox(x) && !!x.label && CONSENT_WORDS.test(x.label)).map((x) => ({ f, label: x.label as string })))[0];
  const consentForm = worded?.f ?? forms.find((f) => f.fields.some((x) => isBox(x) && CONSENT_FIELD.test(x.name)));
  // Forms built by a script often give a checkbox a generated name (for example "_R_2apbsnpfiv5stbH1_"), so the name says nothing. A checkbox in a form that also collects an e-mail address or
  // other personal data is then recorded as found, and the report says plainly that its wording was not recorded, instead of "Not found".
  const PERSONAL = /mail|phone|tel|name|address/i;
  const boxForm = consentForm ?? forms.find((f) => f.fields.some((x) => isBox(x) && !x.label) && f.fields.some((x) => !isBox(x) && (PERSONAL.test(x.name) || PERSONAL.test(x.field_type))));
  return [
    { label: "Contact / Enquiry form", found: forms.length > 0, source: forms.length > 0 ? forms[0].page_url ?? null : null },
    { label: "Explicit consent (e.g. checkbox)", found: consentForm ? true : boxForm ? "checkbox" : false, source: boxForm?.page_url ?? null, wording: worded?.label ?? null },
    { label: "Consent purpose (choices per purpose)", found: per ? per.label === "Found" : null, source: per?.label === "Found" ? home : null },
  ];
}

/** The page counts the scan records per page: forms (each form carries its page) and where a tracking script was first seen. The consent banner and tracking are not recorded for each page separately, so those two are not counted. */
function pageSummary(model: ReportModel, s: SummaryView) {
  const ev = model.evidence;
  const scannedUrls = new Set((ev?.pages ?? []).filter((p) => p.http_status && p.http_status < 400).map((p) => p.url));
  const withForms = new Set((ev?.forms ?? []).map((f) => f.page_url).filter((u): u is string => !!u && (scannedUrls.size === 0 || scannedUrls.has(u))));
  const trackerPages = new Set((ev?.trackers ?? []).map((t) => t.page_url).filter((u): u is string => !!u));
  return { scanned: s.pagesScanned, withForms: ev ? withForms.size : null, trackerPages: ev ? trackerPages.size : null };
}

/** What the scanner itself recorded about the consent banner, one line each; a fact the scan did not record is left out, never guessed. */
function bannerFacts(model: ReportModel): { label: string; value: string }[] {
  const sig = model.evidence?.consent_signals?.[0];
  if (!sig) return [];
  const yn = (v: boolean | null | undefined) => (v === true ? "Yes" : v === false ? "No" : null);
  const kind = sig.mechanism_type === "cmp" ? "Consent management platform" : sig.mechanism_type === "banner" ? "Cookie banner" : sig.mechanism_type === "none" ? "None detected" : null;
  const facts = [
    kind && { label: "Consent mechanism", value: kind },
    sig.cmp_vendor && { label: "Vendor", value: sig.cmp_vendor },
    yn(sig.banner_displayed) && { label: "Banner shown to the scanner", value: yn(sig.banner_displayed)! },
    yn(sig.has_reject_all) && { label: "Reject-all option", value: yn(sig.has_reject_all)! },
    yn(sig.has_granular_choices) && { label: "Choices per purpose", value: yn(sig.has_granular_choices)! },
  ];
  return facts.filter((f): f is { label: string; value: string } => !!f);
}

/** The Data Protection Officer / contact and the Grievance Officer: both are always shown; what the website's public text does not give is said so. */
function officers(s: SummaryView) {
  const find = (re: RegExp) => s.contacts.find((c) => re.test(c.title));
  return [
    { title: "Data Protection Officer / contact", icon: "home" as IconName, c: find(/data protection/i) },
    { title: "Grievance Officer", icon: "scale" as IconName, c: find(/grievance/i) },
  ];
}

const TONE: Record<TenResult, "good" | "warn" | "bad" | "mute"> = { Passed: "good", "Partially Passed": "warn", Failed: "bad", "Not Assessed": "mute", "Not Applicable": "mute" };

function Sheet({ n, fit, testId, children }: { n: 1 | 2; fit: number; testId: string; children: ReactNode }) {
  const steps = Array.from({ length: Math.max(0, Math.min(fit, SNAPSHOT_MAX_FIT)) }, (_, i) => ` sn-fit-${i + 1}`).join("");
  return <section className={`pg sn sn-p${n}${steps}`} data-testid={testId} data-page={n} data-fit={fit}>{children}</section>;
}

const Title = ({ icon, children, note }: { icon: IconName; children: ReactNode; note?: ReactNode }) => (
  <h2 className="sn-h"><i><Icon name={icon} size={15} /></i><span>{children}</span>{note && <em>{note}</em>}</h2>
);

function Page1({ s, ten, model, fit }: { s: SummaryView; ten: TenAssessment; model: ReportModel; fit: number }) {
  const { page } = s;
  const { preview } = page;
  // This report has no "Unable to Verify" result: an element the scan could not see (for example the buttons of a banner that was never shown to it) is "Not tested".
  const el = (key: string) => { const e = s.elements.find((x) => x.title === key); return e ? { ...e, label: e.label === "Unable to Verify" ? "Not tested" : e.label } : undefined; };
  const foundCount = ELEMENT_ORDER.filter((e) => el(e.key)?.label === "Found").length;
  const address = preview.address ?? `https://${preview.domain}`;
  // The four key metrics, each only when the scan recorded it.
  const metrics = [
    s.requests !== null && { value: String(s.requests), label: "Third-party requests" },
    s.domains !== null && { value: String(s.domains), label: "Third-party domains" },
    s.knownRequests !== null && { value: String(s.knownRequests), label: "Analytics and marketing requests" },
    { value: String(s.cookieCounts.found), label: "Cookies set" },
  ].filter((m): m is { value: string; label: string } => !!m);
  return (
    <Sheet n={1} fit={fit} testId="sn-page-1">
      <header className="sn-head" data-testid="sn-head">
        <img className="sn-logo" src={CONSIVA_LOGO_PNG} alt="Consiva.ai" data-testid="sn-logo" />
        <div className="sn-head-r">
          <b className="sn-pill">Privacy Policy Report</b>
          <span>Assessment based on {s.framework.name}</span>
        </div>
      </header>
      <div className="sn-panel">
        <div className="sn-title">
          <div className="sn-domain" data-testid="sn-domain">{preview.domain}<Icon name="arrow" size={16} className="sn-ext" /></div>
          <h1>Privacy Policy Assessment</h1>
        </div>
        <div className="sn-meta" data-testid="sn-meta">
          <span><Icon name="calendar" size={20} /><small>Scan Date</small><b>{page.scannedAt}</b></span>
          <span><Icon name="doc" size={20} /><small>Pages Scanned</small><b>{s.pagesLine}</b></span>
          <span><Icon name="page" size={20} /><small>Report ID</small><b>{page.reportId}</b></span>
        </div>

        <div className="sn-row sn-hero">
          <div className="sn-left">
            <div className="sn-card sn-score" data-testid="sn-score">
              <b className="sn-card-t">Privacy Readiness Score</b>
              <div className="pg-score-ring"><BigRing percent={ten.score} label={ten.score === null ? "" : String(ten.score)} id="snapshot" /></div>
            </div>
            <div className="sn-card sn-banner" data-testid="sn-banner">
              <div className="sn-card-head"><b><Icon name="shield" size={17} />Consent Banner</b></div>
              {el("Consent banner") && <div className="sn-status" data-tone={el("Consent banner")!.tone} data-testid="sn-banner-status">{el("Consent banner")!.label === "Found" ? "Detected" : el("Consent banner")!.label}</div>}
              {bannerFacts(model).length > 0 && (
                <dl className="sn-facts" data-testid="sn-banner-facts">
                  {bannerFacts(model).map((f) => <div key={f.label}><dt>{f.label}</dt><dd>{f.value}</dd></div>)}
                </dl>
              )}
              {bannerFacts(model).length === 0 && !el("Consent banner") && <p className="sn-muted" data-testid="sn-banner-none">No consent banner evidence was recorded for this scan.</p>}
              <div className="sn-opts">
                {["Accept option", "Reject option"].map((k) => el(k) && <span key={k}><b>{k}</b><Pill s={{ label: el(k)!.label, tone: el(k)!.tone }} /></span>)}
              </div>
            </div>
          </div>
          <figure className="sn-card sn-shot" data-testid="pg-hero-preview">
            {preview.src ? (
              <>
                <div className="sn-browser"><i /><i /><i /><span>{address}</span></div>
                <img className="sn-shot-img" src={preview.src} alt={`Homepage of ${preview.domain} as the scanner saw it`} data-testid="pg-hero-screenshot" />
              </>
            ) : (
              <div className="pg-shot-missing" data-testid="pg-hero-missing"><b>Homepage preview not available</b><span>{preview.unavailable}</span></div>
            )}
          </figure>
        </div>

        <div className="sn-contacts" data-testid="sn-contacts">
          {officers(s).map((o) => (
            <div key={o.title} data-testid="sn-contact" data-found={o.c ? "yes" : "no"}>
              <Icon name={o.icon} size={20} className="sn-contact-icon" />
              <div>
                <b>{o.title}</b>
                {o.c ? o.c.lines.map((l) => <span key={l}>{l}</span>) : <span className="sn-none">Not found in the website's public text</span>}
                {o.c?.source && <small>{short(o.c.source)}</small>}
              </div>
            </div>
          ))}
        </div>

        <div className="sn-row sn-one">
          <div className="sn-card sn-elements" data-testid="sn-elements">
            <div className="sn-card-head"><b><Icon name="doc" size={17} />Website Privacy Elements</b><em>{foundCount} of {ELEMENT_ORDER.length} found</em></div>
            <div className="sn-els">
              {ELEMENT_ORDER.map((d) => {
                const e = el(d.key);
                return (
                  <div key={d.key} className="sn-el">
                    <Icon name={d.icon} size={20} className="sn-el-icon" />
                    <b>{d.label}</b>
                    <Pill s={e ? { label: e.label, tone: e.tone } : { label: "Not Tested", tone: "mute" }} />
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        <div className="sn-dpdp" data-testid="sn-framework">
          <b>{s.framework.name}</b>
          <span className="sn-chip sn-mute" data-testid="sn-scope-pages">{ten.pages.scanned} of {ten.pages.discovered ?? "not recorded"} pages scanned</span>
          <span className="sn-chip sn-mute" data-testid="sn-scope-consent">Consent tested on the homepage only</span>
          <span className="sn-chip sn-mute" data-testid="sn-scope-checks">{ten.applicableMax} of {ten.checks.length} checks assessed</span>
        </div>

        <Title icon="bars" note="at a glance">Key Metrics</Title>
        <div className="sn-metrics" data-testid="sn-metrics">
          {metrics.map((t, i) => <div key={t.label} className="sn-tile"><Icon name={TILE_ICON[i] ?? "doc"} size={26} className="sn-tile-icon" /><div><b>{t.value}</b><span>{t.label}</span></div></div>)}
        </div>

        <div className="sn-readme" data-testid="sn-summary">
          <Icon name="info" size={18} />
          <div><b>How to read this score</b><p>{ten.summary}</p></div>
        </div>
      </div>
    </Sheet>
  );
}

/** The page a result rests on, as a link to the real address; when there is no address it says so, and never shows an invented one. */
function Source({ url, id }: { url: string | null; id: number }) {
  if (url && /^https?:\/\//i.test(url)) return <a className="sn-src" href={url} data-testid="sn-source">[{short(url)}]</a>;
  if (url) return <span className="sn-src" data-testid="sn-source">[{short(url)}]</span>;
  return id === 10 ? null : <span className="sn-nosrc" data-testid="sn-nosource">[no source URL available]</span>;
}

function CheckTable({ checks, ten }: { checks: TenCheck[]; ten: TenAssessment }) {
  return (
    <table className="sn-table sn-checks" data-testid="sn-checkpoints">
      <thead><tr><th>#</th><th>Assessment, evidence and finding</th><th>Result</th><th className="sn-num">Marks (Awarded / Max)</th></tr></thead>
      <tbody>
        {checks.map((c) => (
          <tr key={c.id} data-testid="sn-checkpoint" data-status={c.result} data-kind={c.kind}>
            <td className="sn-n">{c.id}</td>
            <td className="sn-cp">
              <b>{c.name}</b>
              {c.legal && <span className="sn-prov">{c.legal}</span>}<span className="sn-kind" data-testid="sn-kind" data-kind={c.kind}>{KIND_LABEL[c.kind]}</span>
              {c.evidence && <span className="sn-ev" data-testid="sn-evidence"><i>Evidence:</i> {c.id >= 2 && c.id <= 7 ? <>&ldquo;{c.evidence}&rdquo;</> : c.evidence} <Source url={c.sourceUrl} id={c.id} /></span>}
              {(c.result !== "Passed" || c.coverage || c.limitation) && <span className="sn-why" data-testid="sn-why"><i>{c.result === "Passed" ? "Scope:" : "Finding:"}</i> {c.result === "Passed" ? (c.limitation ?? c.coverage) : c.explanation}{!c.evidence && c.sourceUrl && c.completed && <> <Source url={c.sourceUrl} id={c.id} /></>}</span>}
            </td>
            <td><Pill s={{ label: c.result, tone: TONE[c.result] }} />{c.result !== "Passed" && c.result !== "Not Applicable" && <span className="sn-basis" data-testid="sn-basis">{c.basis}</span>}{c.result !== "Not Applicable" && c.result !== "Not Assessed" && <span className="sn-quality" data-testid="sn-quality" data-quality={c.quality}>Evidence: {c.quality}</span>}</td>
            <td className="sn-num" data-testid="sn-marks">{c.marksText}</td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr className="sn-total" data-testid="sn-total-row">
          <td colSpan={3}>Total Weightage Awarded (applicable checks) &middot; Privacy Readiness Score {ten.scoreText}{ten.counts.notAssessed > 0 && <> &middot; {ten.counts.notAssessed} not assessed, left out</>}</td>
          <td className="sn-num" data-testid="sn-total-marks">{ten.totalText}</td>
        </tr>
      </tfoot>
    </table>
  );
}

function Page2({ s, ten, model, fit }: { s: SummaryView; ten: TenAssessment; model: ReportModel; fit: number }) {
  const cc = s.cookieCounts;
  const lines = s.cookieLines.slice(0, SNAPSHOT_COOKIES);
  const forms = formsAndConsent(model, s);
  const ps = pageSummary(model, s);
  const unit = (n: number | null, one: string) => (n === null ? "Not recorded" : String(n) + (one ? ` ${one}` : ""));
  return (
    <Sheet n={2} fit={fit} testId="sn-page-2">
      <div className="sn-panel sn-panel2">
        <div className="sn-row sn-cookie-row">
          <div className="sn-block" data-testid="sn-cookie-block">
            <Title icon="cookie" note={s.cookieTotal > SNAPSHOT_COOKIES ? `Top ${SNAPSHOT_COOKIES} of ${s.cookieTotal} cookies` : `${s.cookieTotal} ${s.cookieTotal === 1 ? "cookie" : "cookies"}`}>Cookie Consent Analysis</Title>
            {lines.length === 0 ? <p className="sn-muted">No cookies were recorded on the pages scanned.</p> : (
              <table className="sn-table sn-cookies" data-testid="sn-cookies">
                <thead><tr><th>#</th><th>Cookie</th><th>Domain</th><th>Purpose</th><th>Before</th><th>After Accept</th><th>After Reject</th></tr></thead>
                <tbody>{lines.map((c, i) => <tr key={`${c.name}|${c.domain}`}><td className="sn-n">{i + 1}</td><td className="sn-break"><b>{c.name}</b></td><td className="sn-break">{c.domain}</td><td>{c.classification}</td><td>{c.before}</td><td>{c.accept}</td><td>{c.reject}</td></tr>)}</tbody>
              </table>
            )}
            <div className="sn-foot" data-testid="sn-cookie-counts">
              <span>Total Cookies: <b>{cc.found}</b></span>{cc.byCategory.map((g) => <span key={g.label}>{g.label}: <b>{g.count}</b></span>)}
              <span>Third-party: <b>{cc.thirdParty}</b></span>
            </div>
          </div>
          <div className="sn-block sn-forms" data-testid="sn-forms">
            <Title icon="form">Forms &amp; Consent</Title>
            {forms.map((f) => (
              <div key={f.label} className="sn-form-row" data-testid="sn-form-row">
                <div><b>{f.label}</b>{f.wording && <small className="sn-wording" data-testid="sn-checkbox-wording">&ldquo;{f.wording.length > 110 ? `${f.wording.slice(0, 109)}…` : f.wording}&rdquo;</small>}{f.found === "checkbox" && <small data-testid="sn-checkbox-note">wording not recorded</small>}{f.source ? <small>{short(f.source)}</small> : f.found ? <small>page not recorded</small> : null}</div>
                <Pill s={f.found === null ? { label: "Not Tested", tone: "mute" } : f.found === "checkbox" ? { label: "Checkbox found", tone: "good" } : f.found ? { label: "Found", tone: "good" } : { label: "Not found", tone: "warn" }} />
              </div>
            ))}
          </div>
        </div>

        <Title icon="cycle">Consent States Compared</Title>
        {s.consentRows.length === 0 ? <p className="sn-muted">The scan evidence was not loaded, so the consent comparison is not shown.</p> : (
          <table className="sn-table sn-consent" data-testid="sn-consent">
            <thead><tr><th>Metric</th><th>Before Consent</th><th>After Accept</th><th>After Reject</th></tr></thead>
            <tbody>{s.consentRows.map((r) => <tr key={r.label}><td>{r.label}</td>{r.cells.map((x, i) => <td key={i} className={x === "Not Tested" ? "sn-nt" : undefined}>{x}</td>)}</tr>)}</tbody>
          </table>
        )}

        <Title icon="shield" note="India DPDP Act, 2023 and Rules, 2025">Key Compliance Checks</Title>
        <CheckTable checks={ten.checks} ten={ten} />

        <Title icon="page" note={s.selectionNotes[0]}>Page URLs (Scanned)</Title>
        {s.selection.length === 0 ? <p className="sn-muted">The page list was not loaded for this scan.</p> : (
          <table className="sn-table sn-pages" data-testid="sn-pages">
            <thead><tr><th>#</th><th>Page URL</th><th>Why it was chosen</th><th>Result</th></tr></thead>
            <tbody>{s.selection.map((r) => <tr key={r.url}><td className="sn-n">{r.n}</td><td className="sn-url" title={r.url}>{short(r.url, 90)}</td><td>{r.reason}</td><td className="sn-nowrap">{r.status}</td></tr>)}</tbody>
          </table>
        )}

        <div className="sn-row sn-two sn-end">
          <div className="sn-block sn-summary" data-testid="sn-page-summary">
            <Title icon="board">Page Scan Summary</Title>
            <dl>
              <div><dt>Pages discovered</dt><dd>{ten.pages.discovered ?? "Not recorded"}</dd></div>
              <div><dt>Pages scanned</dt><dd>{ten.pages.scanned}</dd></div>
              <div><dt>Pages failed to load</dt><dd>{ten.pages.failed}</dd></div>
              <div><dt>Pages found, not scanned</dt><dd>{ten.pages.skipped}</dd></div>
              <div><dt>Pages with forms</dt><dd>{unit(ps.withForms, "")}</dd></div>
              <div><dt>Pages where tracking scripts were first seen</dt><dd>{unit(ps.trackerPages, "")}</dd></div>
              <div><dt>Consent banner / tracking, page by page</dt><dd>Not recorded per page</dd></div>
            </dl>
          </div>
          <div className="sn-block sn-reco" data-testid="sn-reco">
            <Title icon="target">Recommendations</Title>
            <ul data-testid="sn-next">{[...ten.recommendations.slice(0, 3).map((r) => (r.id ? `Check ${r.id}: ${r.text}` : r.text)), ...s.nextSteps.filter((t) => /full scan/i.test(t))].map((t) => <li key={t}>{t}</li>)}</ul>
            <ul className="sn-limits" data-testid="sn-limits">{[...s.limitations.filter((l) => !/found on the main domain/.test(l)), ...ten.limitations.filter((l) => !s.limitations.includes(l))].map((l) => <li key={l}>{l}</li>)}</ul>
          </div>
        </div>

        <div className="sn-cta" data-testid="sn-cta">
          <Icon name="bolt" size={22} />
          <div><b>{SIGNUP_TEXT}</b><span>{SIGNUP_SUB}</span></div>
          <a href={SIGNUP_URL} data-testid="sn-signup">Sign Up</a>
        </div>
        <div className="sn-disc" data-testid="sn-disclaimer"><Icon name="info" size={18} /><p><b>Disclaimer:</b> {SNAPSHOT_DISCLAIMER}</p></div>
      </div>
    </Sheet>
  );
}

/**
 * The two-page report (variant "summary"). Each sheet is a fixed A4 page; after layout the content is measured against the sheet and, when it does not fit, tightened one step at a time
 * (spacing, then type; see snapshot.css). Every figure comes from the report model; a value the scan does not record is shown as such, never filled in.
 */
export function SnapshotReport({ model }: { model: ReportModel }) {
  const s = buildSummaryReport(model);
  const ten = model.tenChecks;
  const doc = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState<[number, number]>([0, 0]);
  useLayoutEffect(() => {
    const sheets = doc.current?.querySelectorAll("section.pg");
    if (!sheets) return;
    // A sheet is as tall as its content (never clipped); it is over when that is more than one A4 page.
    const over = (i: number) => {
      const el = sheets[i] as HTMLElement | undefined;
      if (!el) return false;
      // On a narrow screen (the server prints from a 794px-wide viewport) pages.css zooms the document down; the printed page is not zoomed. Measure in the sheet's own units: undo the zoom
      // by the ratio of its drawn width to its real width, or a sheet that is too tall would look as if it fitted.
      const r = el.getBoundingClientRect();
      const zoom = r.width > 0 ? r.width / PAGE_WIDTH : 1;
      return r.height / zoom > PAGE_HEIGHT + 0.05;
    };
    const next: [number, number] = [fit[0], fit[1]];
    let changed = false;
    for (const i of [0, 1] as const) if (over(i) && next[i] < SNAPSHOT_MAX_FIT) { next[i] += 1; changed = true; }
    if (changed) setFit(next);
  });
  return (
    // The server prints from a narrow viewport, where pages.css would zoom the sheets down for a small screen. The printed page is not zoomed, so when printing the zoom is turned off:
    // the layout that is measured and fitted is then the layout that is printed.
    <div className="pg-doc" data-testid="summary-report" ref={doc} style={PRINTING ? { zoom: 1 } : undefined}>
      <SheetPageStyle />
      <Page1 s={s} ten={ten} model={model} fit={fit[0]} />
      <Page2 s={s} ten={ten} model={model} fit={fit[1]} />
    </div>
  );
}
