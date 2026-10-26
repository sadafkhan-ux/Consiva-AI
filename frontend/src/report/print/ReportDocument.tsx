import type { ReactNode } from "react";
import { ScoreRing } from "../../components/ScanHero";
import type { ReportModel } from "../buildReport";
import { buildHeroView, buildPreviewView } from "../hero";
import { formatRef, formatRefShort, formatRefs } from "../legalRefs";
import { buildPolicyView, POLICY_BASIS_LABEL, POLICY_STATUS_LABEL } from "../policyAnalysis";
import { READINESS_STATUS_TONE } from "../readiness";
import { isReportCategory, OMITTED_DUTIES_NOTE, REPORT_DISCLAIMER } from "../reportScope";
import { checkContribution, CTA_URLS, MAX_LISTED_PAGES, stripId, stripRuleCodes, topFindingRows, type RowTone } from "../reportText";
import type { ElementStatus } from "../reportVisual";
import { REQUEST_UNIT_NOTE } from "../stateCounts";
import { ScanTransparency } from "./Transparency";
import "./report.css";

// The printable report: the same sections, in the same order, as the PDF the browser draws (report/downloadReport.ts), built from the same
// ReportModel the dashboard and that PDF read. The server prints this page to PDF (api: GET /api/v1/consent/scans/{id}/report.pdf), and the web
// report page shows it on screen, so the two cannot disagree: there is one set of values and one design (report.css). Every figure comes from the
// scan; a value the scan does not have is shown as "Not assessed", "Not tested" or "Could not verify", never filled in. Nothing is cut: a block
// grows to fit its text and the print engine carries it onto the next page, repeating a table's header.

type Tone = RowTone;

function Pill({ tone = "mute", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return <span className={`r-pill r-${tone}`} title={title}>{children}</span>;
}

function Sec({ n, title, count, id, appendix, children }: { n: number; title: string; count?: string; id: string; appendix?: boolean; children: ReactNode }) {
  return (
    <section className={`r-sec${appendix ? " r-appx" : ""}`} id={id} data-testid={`rpt-sec-${id}`}>
      <h2><span className="r-badge">{n}</span><span className="r-sec-title">{title}</span>{count && <Pill>{count}</Pill>}</h2>
      {children}
    </section>
  );
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const label = (k: string) => k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const bare = (u: string | null | undefined) => (u ? u.replace(/^https?:\/\//, "") : "Not recorded");
const yesNo = (b: boolean | null | undefined) => (b === null || b === undefined ? "Not recorded" : b ? "Yes" : "No");
const STATE_NAME: Record<string, string> = { pre_consent: "Before consent", post_accept: "After Accept", post_reject: "After Reject" };
const states = (s: string[]) => (s.length ? s.map((x) => STATE_NAME[x] ?? x).join(", ") : "Not recorded");
const show = (val: unknown): string => {
  if (val === null || val === undefined || val === "") return "Not recorded";
  if (typeof val === "boolean") return val ? "Yes" : "No";
  if (Array.isArray(val)) return val.length ? val.map(String).join(", ") : "None";
  if (typeof val === "object") return JSON.stringify(val);
  return String(val);
};
const ELEMENT_PILL: Record<ElementStatus, { label: string; tone: Tone }> = {
  found: { label: "Found", tone: "good" },
  not_found: { label: "Not found in scanned scope", tone: "warn" },
  unverified: { label: "Could not verify", tone: "mute" },
  not_assessed: { label: "Not assessed", tone: "mute" },
};
const verdictTone = (t: string): Tone => (t === "pass" ? "good" : t === "fail" ? "bad" : t === "na" ? "mute" : "warn");

function Para({ children, muted, small, bold, indent }: { children: ReactNode; muted?: boolean; small?: boolean; bold?: boolean; indent?: boolean }) {
  return <p className={`r-p${muted ? " r-muted" : ""}${small ? " r-small" : ""}${bold ? " r-bold" : ""}${indent ? " r-indent" : ""}`}>{children}</p>;
}

function Quote({ text, source }: { text: string; source: string | null }) {
  return (
    <blockquote className="r-quote">
      “{text}”
      {source && <div className="r-quote-src"><a href={source}>{source}</a></div>}
    </blockquote>
  );
}

function Table({ head, rows, wide, className }: { head: { h: string; w?: string; num?: boolean }[]; rows: ReactNode[][]; wide?: boolean; className?: string }) {
  return (
    <table className={`r-table${wide ? " r-wide" : ""}${className ? ` ${className}` : ""}`}>
      <thead><tr>{head.map((c, i) => <th key={i} style={c.w ? { width: c.w } : undefined} className={c.num ? "r-num" : undefined}>{c.h}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, k) => <td key={k} className={head[k]?.num ? "r-num" : undefined}>{c}</td>)}</tr>)}</tbody>
    </table>
  );
}

/** The hero: the score at the left, what it means in the middle, the scanned site's preview at the right. One container, three areas, side by side. */
function Hero({ model }: { model: ReportModel }) {
  const hero = buildHeroView(model.dpdpScore);
  const preview = buildPreviewView(model.screenshot, model.siteUrl, model.scanId);
  return (
    <div className="r-hero" data-testid="rpt-hero">
      <div className="r-hero-score" data-testid="rpt-hero-score">
        <ScoreRing percent={hero.percent} label={hero.percentLabel} />
        <div className="r-hero-label">{hero.scoreLabel}</div>
      </div>
      <div className="r-hero-text">
        <Pill tone={hero.tone === "good" ? "good" : hero.tone === "warn" ? "warn" : "mute"}>{hero.band}</Pill>
        <p className="r-hero-headline" data-testid="rpt-hero-headline">{hero.headline}</p>
        <a className="r-cta" href={CTA_URLS.improve}>Improve My Score with Consiva AI</a>
      </div>
      <figure className="r-hero-preview" data-testid="rpt-hero-preview">
        <figcaption className="r-hero-domain">{preview.domain}</figcaption>
        {preview.src ? (
          <img src={preview.src} alt={`Homepage of ${preview.domain} as the scanner saw it`} width={preview.width} height={preview.height} data-testid="rpt-hero-screenshot" />
        ) : (
          <div className="r-hero-missing" data-testid="rpt-hero-missing"><b>Homepage preview not available</b><span>{preview.unavailable}</span></div>
        )}
        <div className="r-small r-muted">{preview.caption}</div>
        {preview.address && <div className="r-small r-muted r-break">{preview.address}</div>}
      </figure>
    </div>
  );
}

/** Sections the fixed-page reports (Free and Full) already carry in full. In the appendix they are left out; every other section follows the pages. */
const ON_FIXED_PAGES: ReadonlySet<string> = new Set(["summary", "transparency", "readiness", "areas", "top-findings", "consent", "technologies", "elements"]);

export function ReportDocument({ model, appendixOnly = false, noFront = false }: { model: ReportModel; appendixOnly?: boolean; noFront?: boolean }) {
  const v = model.dpdpScore;
  const vis = model.visual;
  const ev = model.evidence;
  const policyView = buildPolicyView(model.policyAnalysis);
  const assessed = v ? v.assessed : [];
  const countOf = (k: string) => assessed.filter((r) => r.statusKey === k).length;
  const sections: { id: string; title: string; count?: string; appendix?: boolean; node: ReactNode }[] = [];
  const add = (id: string, title: string, node: ReactNode, opts: { count?: string; appendix?: boolean } = {}) => sections.push({ id, title, node, ...opts });

  // 1. Scan summary
  const tiles: { value: string; label: string }[] = vis
    ? [
        { value: vis.pagesFound !== null && vis.pagesFound >= vis.pagesScanned ? `${vis.pagesScanned} of ${vis.pagesFound}` : String(vis.pagesScanned), label: vis.pageLimit ? `Pages scanned (limit ${vis.pageLimit})` : "Pages scanned" },
        { value: String(vis.statesTested), label: "Consent states tested" },
        { value: String(vis.requests), label: "Third-party requests" },
        { value: String(vis.knownRequests), label: "Analytics and marketing requests" },
        { value: String(vis.cookies), label: "Cookies set" },
        { value: String(vis.domains), label: "Third-party domains" },
      ]
    : model.counts.slice(0, 6).map((c) => ({ value: c.value, label: c.label }));
  add("summary", "Scan summary", (
    <div className="r-tiles">{tiles.map((t) => <div className="r-tile" key={t.label}><b>{t.value}</b><span>{t.label}</span></div>)}</div>
  ));

  // Scan transparency: what the scan found, tried, finished and could not verify, the three coverages, the consent states exercised, and why the score is what it is.
  add("transparency", "Scan transparency", <ScanTransparency view={model.transparency} />);

  // 2. Framework readiness
  add("readiness", "Framework readiness", (
    <div className={`r-box r-framework${v && v.scoreValue !== null ? "" : " r-soft"}`}>
      <div><div className="r-bold">India DPDP Act</div><div className="r-big" data-testid="rpt-framework-score">{v ? v.headline : "Not available"}</div></div>
      <div className="r-small r-muted">
        {v ? (<>{countOf("met")} met · {countOf("partially_met")} partly met · {countOf("not_met")} not met<br />{v.unableToVerify.length} could not be verified · {v.excluded.length} not applicable</>) : "The score was not loaded."}
      </div>
    </div>
  ));

  // 3. Privacy areas
  if (v) {
    const cats = v.categories.filter((c) => isReportCategory(c.name)).slice(0, 4);
    add("areas", "Privacy areas", (
      <>
        <Para muted small>Each bar is the share of the weighted points earned in that area. Coverage is the share of the area's checks that a scan could test.</Para>
        <div className="r-areas">
          {cats.map((c) => {
            const pctValue = c.score === "Not assessed" ? null : parseFloat(c.score);
            return (
              <div className="r-area" key={c.name}>
                <b>{c.name}</b>
                <div className={pctValue === null ? "r-muted r-bold" : "r-big"}>{pctValue === null ? "Not assessed" : c.score}</div>
                {pctValue !== null && <div className="r-bar"><i style={{ width: `${Math.max(2, Math.min(100, pctValue))}%` }} /></div>}
                <span className="r-small r-muted">{pctValue === null ? "Cannot be tested from outside" : c.coverage.replace("coverage", "Coverage")}</span>
              </div>
            );
          })}
        </div>
      </>
    ));
  }

  // 4. Top findings
  const rows = topFindingRows(model);
  const shown = rows.slice(0, 3);
  add("top-findings", "Top findings", (
    <>
      {shown.length === 0 && <Para>No gaps were found in the checks we could test.</Para>}
      {shown.map((f, i) => (
        <div className="r-finding" key={i}>
          <span className={`r-num-dot r-${f.tone}`}>{i + 1}</span>
          <div><b>{f.title}</b>{f.detail && <div className="r-small r-muted">{f.detail}</div>}</div>
          <Pill tone={f.tone}>{f.label}</Pill>
        </div>
      ))}
      {rows.length > shown.length && <Para muted small>+ {rows.length - shown.length} more in the Appendix (Compliance findings in full).</Para>}
    </>
  ));

  // 5-7. Consent behaviour, tracking technologies, privacy elements
  if (vis) {
    const c = vis.consent;
    const num = (n: number, tested: boolean) => (tested ? String(n) : "Not tested");
    const cell = (s: string, bold: boolean) => (/^\d+$/.test(s) ? <span className={`r-count-cell${bold ? " r-bold" : ""}`}>{s}</span> : <span className={bold ? "r-muted" : ""}>{s}</span>);
    add("consent", "Consent behaviour", (
      <>
        <Table
          head={[{ h: "What was observed" }, { h: "Before consent" }, { h: "After Accept" }, { h: "After Reject" }]}
          className="r-center"
          rows={[
            ["Cookies set", cell(num(c.cookies[0], true), true), cell(num(c.cookies[1], c.acceptTested), true), cell(num(c.cookies[2], c.rejectTested), true)],
            ["Analytics and marketing requests", cell(num(c.known[0], true), true), cell(num(c.known[1], c.acceptTested), true), cell(num(c.known[2], c.rejectTested), true)],
            ["All third-party requests", cell(num(c.all[0], true), true), cell(num(c.all[1], c.acceptTested), true), cell(num(c.all[2], c.rejectTested), true)],
            ["Banner action", cell(c.banner[0], false), cell(c.banner[1], false), cell(c.banner[2], false)],
          ]}
        />
        <Para muted small>{REQUEST_UNIT_NOTE}</Para>
        <div className="r-callout"><b>KEY TAKEAWAY</b><p>{c.takeaway}</p></div>
      </>
    ));
    add("technologies", "Tracking technologies detected", (
      <>
        <Table
          head={[{ h: "Technology" }, { h: "Type", w: "90px" }, { h: "Requests", w: "70px", num: true }, { h: "Seen", w: "140px" }]}
          rows={vis.technologies.rows.length === 0 ? [["No third-party technologies were recorded on the pages scanned.", "", "", ""]] : vis.technologies.rows.map((t) => [<b key="n">{t.name}</b>, t.type, String(t.requests), t.seen])}
        />
        {vis.technologies.more > 0 && <Para muted small>+ {vis.technologies.more} more: every third-party request is listed in the Appendix (Third-party requests and scripts).</Para>}
      </>
    ));
    add("elements", "Privacy elements on the site", (
      <div className="r-elements">
        {vis.elements.slice(0, 12).map((e) => {
          const p = ELEMENT_PILL[e.status];
          return <div className="r-element" key={e.title}><b>{e.title}</b><Pill tone={p.tone}>{p.label}</Pill><span className="r-small r-muted">{e.note}</span></div>;
        })}
      </div>
    ));
  } else {
    add("consent", "Consent behaviour", <Para muted>The scan evidence could not be loaded, so the consent comparison, technologies and privacy elements are not shown.</Para>);
  }

  // 8. Every check behind the score
  if (v) {
    const checks = v.allChecks.filter((c) => isReportCategory(c.category));
    const listed = checks.filter((c) => c.statusKey !== "not_applicable");
    const notApplicable = checks.filter((c) => c.statusKey === "not_applicable");
    add("checks", "Every check behind the score", (
      <>
        <Para muted small>Detailed results for each requirement we could test.</Para>
        <Table
          head={[{ h: "ID", w: "36px" }, { h: "What is checked, and the evidence" }, { h: "Requirement", w: "130px" }, { h: "Weight", w: "54px", num: true }, { h: "Status", w: "78px" }, { h: "Contribution", w: "92px" }]}
          rows={[
            ...listed.map((r) => [
              <b key="i" className={r.tone === "fail" ? "r-failrow" : undefined}>{r.id.replace("DPDP-", "")}</b>,
              <div key="t"><b>{r.title}</b><div className="r-small r-muted">{stripRuleCodes(r.evidenceLine)}</div></div>,
              <span key="r" className="r-small r-muted">{formatRefShort(r.provisions)}</span>,
              String(r.weight),
              <Pill key="s" tone={verdictTone(r.tone)}>{r.verdict}</Pill>,
              <span key="c" className="r-small">{checkContribution(r)}</span>,
            ]),
            ...(notApplicable.length ? [["", <span key="n" className="r-small"><b>{notApplicable.map((r) => r.id.replace("DPDP-", "")).join(", ")}</b><span className="r-muted"> · Not applicable: left out unless declared.</span></span>, "", "", <Pill key="s">N/A</Pill>, ""]] : []),
          ]}
        />
        <Para muted small>{OMITTED_DUTIES_NOTE}</Para>
      </>
    ));
  }

  // 9. Prioritised fixes
  {
    const scoreFixes = v ? v.remediationPlan : [];
    const policyFixes = policyView?.fixes ?? [];
    add("fixes", "Prioritised fixes", (
      <>
        <Para muted small>In the order to do them. Where a fix changes the score, its impact is the score's own arithmetic: that one check fixed on its own and nothing else changed. Where it changes the policy, it is the policy's own completeness.</Para>
        {!v && !policyView && <Para>The score and the policy analysis were not available for this report, so no fixes can be listed.</Para>}
        {v && (
          <>
            <h3 className="r-h3">Fixes that change the score</h3>
            {scoreFixes.length === 0 && <Para small indent>No fix is listed for the score: no check failed with enough evidence.</Para>}
            {scoreFixes.map((f) => (
              <div className="r-item" key={f.rank}>
                <div className="r-item-head"><b>{f.rank}. {f.title}</b><span><Pill tone={f.timing === "Current" ? "bad" : "warn"}>{f.priority}</Pill> <Pill>{f.outcome}</Pill></span></div>
                <Para small indent>Action: {f.action}</Para>
                {f.scoreLine && <Para small bold indent>Impact on the score: {f.scoreLine}</Para>}
                {f.impact && <Para small indent>Why it matters: {f.impact}</Para>}
                <Para small indent>How to verify: {f.howToVerify}</Para>
                <Para small muted indent>{formatRef(f.legal)}</Para>
              </div>
            ))}
          </>
        )}
        {policyView && (
          <>
            <h3 className="r-h3">Fixes to the privacy policy's wording</h3>
            {!policyView.available && <Para small indent>{policyView.reason ?? "The policy analysis is not available for this scan."}</Para>}
            {policyView.available && policyFixes.length === 0 && <Para small indent>The policy covers every point that applies to this site.</Para>}
            {policyFixes.map((f) => (
              <div className="r-item" key={f.rank}>
                <div className="r-item-head"><b>{f.rank}. {f.row.title}</b><span><Pill tone={f.kind === "Required" ? "warn" : "mute"}>{f.kind}</Pill> <Pill tone={f.row.status === "partial" ? "warn" : "bad"}>{POLICY_STATUS_LABEL[f.row.status] ?? f.row.status}</Pill></span></div>
                <Para small indent>Action: {f.row.fix}</Para>
                {f.impactLine && <Para small bold indent>Impact on the policy: {f.impactLine}</Para>}
                <Para small indent>Why it matters: {f.why}</Para>
              </div>
            ))}
          </>
        )}
        <Para muted small>What each rests on: the score fixes rest on what the scan measured. The policy fixes rest on the policy's own text, where a point not found is not a finding that a duty is unmet.</Para>
      </>
    ));
  }

  // 10-14. Grievance officer, DPO, Indian-language support, children's data, security signals
  for (const s of model.insights) {
    add(`insight-${s.key}`, s.title, (
      <>
        <div className="r-pills"><Pill tone={s.tone === "good" ? "good" : s.tone === "warn" ? "warn" : "mute"}>{s.status}</Pill><Pill>{s.knowledge}</Pill></div>
        <Para small>{s.summary}</Para>
        {s.facts.length > 0 && (
          <Table head={[{ h: "What the scan looked at", w: "36%" }, { h: "What it found" }]} rows={s.facts.map((f) => [<b key="l">{f.label}</b>, f.value])} />
        )}
        {s.quote && <Quote text={s.quote.text} source={s.quote.source} />}
        <Para small muted>{s.legal}</Para>
        {s.notes.map((n) => <Para key={n} small muted>{n}</Para>)}
      </>
    ));
  }

  // 15. Five-page readiness review
  {
    const rv = model.readiness;
    add("five-page", "Five-page DPDP readiness review", (
      <>
        <Para muted small>{rv.timing}</Para>
        {!rv.available ? <Para>{rv.reason ?? "The review is not available for this scan."}</Para> : (
          <>
            <h3 className="r-h3">A. The five pages</h3>
            <Table
              head={[{ h: "#", w: "24px" }, { h: "Page", w: "130px" }, { h: "URL" }, { h: "Why selected" }]}
              rows={rv.pages.map((p) => [String(p.n), <b key="r">{p.role}</b>, p.url ? <span key="u" className="r-break">{p.scanned ? p.url : `${p.url} (named, not loaded in this scan)`}</span> : <Pill key="u" tone="bad">NOT FOUND</Pill>, <span key="w" className="r-small">{p.why}</span>])}
            />
            <h3 className="r-h3">B. Findings, one row per check</h3>
            <div className="r-pills">
              {(["Compliant", "Gap", "Not Found", "Cannot Verify", "Requires document review"] as const).map((k) => <Pill key={k} tone={READINESS_STATUS_TONE[k]}>{rv.counts[k]} {k}</Pill>)}
            </div>
            <Table
              className="r-dense"
              head={[{ h: "Page", w: "58px" }, { h: "Section", w: "74px" }, { h: "Check", w: "120px" }, { h: "Status", w: "62px" }, { h: "Evidence" }, { h: "Fix", w: "120px" }]}
              rows={rv.rows.map((r) => [<b key="p">{r.page}</b>, r.sections, r.check, <span key="s"><b>{r.status}</b><br />({r.evidenceKind})</span>, r.evidence, r.fix])}
            />
            <h3 className="r-h3">C. Summary</h3>
            <Para bold>Five-page review score: {rv.scoreLine}</Para>
            <Para bold small>Top gaps, ranked by risk</Para>
            {rv.topGaps.length === 0 && <Para small indent>No gap or missing item was found in the checks the scan could assess.</Para>}
            {rv.topGaps.map((g) => (
              <div className="r-item" key={g.rank}>
                <div className="r-item-head"><b>{g.rank}. {g.row.check}</b><Pill tone={g.riskLabel === "High" ? "bad" : g.riskLabel === "Medium" ? "warn" : "mute"}>{g.riskLabel} risk</Pill></div>
                <Para small indent>{g.row.page}: {g.row.status}. Fix: {g.row.fix}</Para>
              </div>
            ))}
            <Para bold small>Highest-risk page: {rv.riskiest ? `${rv.riskiest.n}. ${rv.riskiest.page}. ${rv.riskiest.why}` : "None: no page carries a gap on what the scan could assess."}</Para>
            {rv.notes.map((n) => <Para key={n} small muted>{n}</Para>)}
            <Para bold small>{rv.closing}</Para>
          </>
        )}
      </>
    ));
  }

  // 16. Website pages scanned
  {
    const sp = model.scanPages;
    const siteHost = model.siteUrl.replace(/^https?:\/\//, "").replace(/\/$/, "");
    add("pages", "Website Pages Scanned", (
      <>
        <Para muted small>Scan run {model.scanId ? model.scanId.slice(0, 8) : "not recorded"} of {siteHost}. Listed as the scan recorded them: the homepage first, then the pages that loaded, then any attempt that did not.</Para>
        {sp && <p className="r-big r-line">{sp.coverageLine}</p>}
        {!sp || sp.rows.length === 0 ? (
          <Para muted>{sp ? "The scan recorded no pages." : "The list of pages could not be loaded for this report, so the pages are not shown. The scan itself is unaffected."}</Para>
        ) : (
          <>
            <Table
              head={[{ h: "No.", w: "42px" }, { h: "Page type", w: "120px" }, { h: "Page URL" }, { h: "Scan status", w: "100px" }]}
              rows={sp.rows.slice(0, MAX_LISTED_PAGES).map((r) => [
                String(r.no), <span key="t" className={r.no === 1 ? "r-bold" : undefined}>{r.pageType}</span>,
                /^https?:\/\//i.test(r.url) ? <a key="u" className="r-break" href={r.url}>{r.url}</a> : <span key="u" className="r-break">{r.url}</span>,
                r.scanned ? <Pill key="s" tone="good">{r.status}</Pill> : <b key="s" className="r-badtext">{r.status}</b>,
              ])}
            />
            <Para muted small>
              {Math.min(sp.rows.length, MAX_LISTED_PAGES)} {Math.min(sp.rows.length, MAX_LISTED_PAGES) === 1 ? "page" : "pages"} listed{sp.pageLimit ? `, of a limit of ${sp.pageLimit}` : ""}
              {sp.rows.length > MAX_LISTED_PAGES ? `; ${sp.rows.length - MAX_LISTED_PAGES} more recorded by an earlier, larger scan are not listed` : ""}. Select a URL to open it.
            </Para>
          </>
        )}
        {sp && (
          <>
            <h3 className="r-h3">Scan Review</h3>
            {sp.review.map((item) => <Para key={item.label} small><b>{item.label}:</b> {item.text}</Para>)}
          </>
        )}
      </>
    ));
  }

  // Evidence appendix. Each section is numbered on from the summary, so the badges run on without a gap.
  if (ev) {
    const sec = v?.sector ?? null;
    if (sec) {
      add("sector", "Sector and applicability", (
        <>
          <Para bold small>{sec.label}. {sec.summary}{sec.also_possible.length ? ` Also possible: ${sec.also_possible.join(", ")}.` : ""}</Para>
          {sec.signals.length > 0 && <Para small muted>Seen in the scan: {sec.signals.map((s) => `"${s.match}" (${s.source})`).join(", ")}.</Para>}
          {sec.notes.map((n) => (
            <div className="r-item" key={n.id}>
              <div className="r-item-head"><b>{n.title}</b></div>
              <Para small muted>{formatRefs(n.provisions)}</Para>
              <Para small>{n.text}</Para>
              {n.observed.map((o) => <Para key={o} small indent>Observed: {o}</Para>)}
              <Para small bold indent>What to check: {n.action}</Para>
            </div>
          ))}
          {sec.declarations.length > 0 && (
            <>
              <Para small bold>Worth declaring (these requirements stay out of the score until declared):</Para>
              {sec.declarations.map((d) => <Para key={d.requirement_id} small indent>{d.requirement_id}: {d.label}. {d.declared === null ? "Not declared" : d.declared ? "Declared applicable" : "Declared not applicable"}. {d.why}</Para>)}
            </>
          )}
          <Para small muted>{sec.limitations.join(" ")}</Para>
        </>
      ), { count: sec.confidence === "declared" ? "Declared" : `${sec.confidence} confidence`, appendix: true });
    }

    if (policyView) {
      add("policy", "Privacy policy content analysis", (
        <>
          {!policyView.available ? (
            <>
              <Para bold>{policyView.headline}. {policyView.reason ?? ""}</Para>
              {policyView.notes.map((n) => <Para key={n} small muted>{n}</Para>)}
            </>
          ) : (
            <>
              <p className="r-big r-line">{policyView.headline}</p>
              <Para small muted>{policyView.summaryLine}.</Para>
              <h3 className="r-h3">Checklist</h3>
              <Table head={[{ h: "Point the policy should cover" }, { h: "Status", w: "100px" }, { h: "Basis", w: "160px" }]} rows={policyView.checklist.map((c) => [<b key="t">{c.title}</b>, c.status, c.basis])} />
              {policyView.groups.map((g) => (
                <div key={g.name}>
                  <h3 className="r-h3">{g.name}</h3>
                  {g.rows.map((r) => (
                    <div className="r-item" key={r.id}>
                      <div className="r-item-head"><b>{r.title}</b><span>
                        <Pill tone={r.status === "verified" ? "good" : r.status === "not_applicable" ? "mute" : r.status === "partial" ? "warn" : "bad"}>{POLICY_STATUS_LABEL[r.status] ?? r.status}</Pill> <Pill>{POLICY_BASIS_LABEL[r.basis] ?? r.basis}</Pill>
                      </span></div>
                      <Para small muted indent>{r.provisions.join(" · ")}</Para>
                      {r.quote && <Quote text={r.quote} source={r.source_url} />}
                      <Para small indent>{r.detail}</Para>
                    </div>
                  ))}
                </div>
              ))}
            </>
          )}
          {policyView.documents.length > 0 && <Para small muted>Read: {policyView.documents.join(" · ")}</Para>}
          <Para small muted>{policyView.limitations.join(" ")}</Para>
        </>
      ), { count: policyView.available ? policyView.headline.replace("Policy completeness ", "") : "Not available", appendix: true });
    }

    add("banner", "Consent banner", (
      <>
        {ev.consent_signals.length === 0 && <Para>No consent mechanism was detected on the pages scanned.</Para>}
        {ev.consent_signals.map((s, i) => (
          <div key={i}>
            {ev.consent_signals.length > 1 && <Para small bold>Consent mechanism {i + 1}</Para>}
            <Table
              head={[{ h: "Detail", w: "190px" }, { h: "Recorded" }]}
              rows={[
                ["Mechanism", s.mechanism_type], ["Consent tool (vendor)", s.cmp_vendor ?? "Not identified"],
                ["Reject-all option", yesNo(s.has_reject_all)], ["Granular choices", yesNo(s.has_granular_choices)],
                ...Object.entries(s.evidence ?? {}).map(([k, val]) => [label(k), show(val)]),
              ].map(([a, b]) => [<b key="a">{a}</b>, <span key="b" className="r-break">{b}</span>])}
            />
          </div>
        ))}
      </>
    ), { count: ev.consent_signals.length === 0 ? "None detected" : plural(ev.consent_signals.length, "mechanism"), appendix: true });

    add("forms", "Forms found", (
      <>
        {ev.forms.length === 0 && <Para>No forms were found on the pages scanned.</Para>}
        {ev.forms.map((f, i) => (
          <div className="r-item" key={f.id}>
            <div className="r-item-head"><b>Form {i + 1}{f.purpose_guess ? `: ${label(f.purpose_guess)}` : ""} — {plural(f.fields.length, "field")}{f.selector ? ` (${f.selector})` : ""}</b></div>
            <Table
              head={[{ h: "Field name" }, { h: "Type", w: "130px" }, { h: "Required", w: "90px" }]}
              rows={f.fields.length ? f.fields.map((fl) => [<b key="n">{fl.name || "(unnamed)"}</b>, fl.field_type || "Not recorded", fl.required ? "Yes" : "No"]) : [["No fields were recorded", "", ""]]}
            />
          </div>
        ))}
      </>
    ), { count: plural(ev.forms.length, "form"), appendix: true });

    const inv = model.cookies;
    const iso = (s: string) => /^\d{4}-\d{2}-\d{2}/.exec(s)?.[0] ?? null;
    add("cookies", "Cookie inventory", (
      <>
        {inv.total === 0 ? <Para>No cookies were recorded.</Para> : (
          <>
            <div className="r-tiles r-tiles5">
              {([
                ["Cookies found", String(inv.summary.found)], ["Third-party", String(inv.summary.thirdParty)], ["Set before consent", String(inv.summary.beforeConsent)],
                ["Long-lived (over a year)", String(inv.summary.longLived)], ["Seen after Reject", inv.summary.afterReject === null ? "Not tested" : String(inv.summary.afterReject)],
              ] as [string, string][]).map(([l, val]) => <div className="r-tile" key={l}><b>{val}</b><span>{l}</span></div>)}
            </div>
            <Para small muted>Long-lived means the cookie expires more than a year after it was set. The Act and Rules set no limit on a cookie's lifetime, so this is information, not a finding. A purpose is given only for a recognised cookie, in its provider's own words; for any other the scan says what it could establish and that the purpose could not be identified from the name. “Not tested” means no choice was made in that pass, so the cookie's absence there says nothing.</Para>
            {inv.groups.map((g) => (
              <div key={g.title}>
                <h3 className="r-h3">{g.title} ({g.rows.length})</h3>
                <Para small muted>{g.note}</Para>
                <Table
                  className="r-dense"
                  head={[{ h: "Cookie", w: "100px" }, { h: "Provider", w: "62px" }, { h: "Party", w: "46px" }, { h: "Lifetime", w: "84px" }, { h: "Purpose" }, { h: "Before", w: "46px" }, { h: "Accept", w: "46px" }, { h: "Reject", w: "46px" }]}
                  rows={g.rows.map((r) => {
                    const pages = [...new Set(r.sightings.map((s) => s.page).filter((p) => p !== "Not available").map((p) => p.replace(/^https?:\/\//, "")))];
                    const flags = `Secure: ${r.secure === "Not available" ? "n/a" : r.secure} · HttpOnly: ${r.httpOnly === "Not available" ? "n/a" : r.httpOnly} · SameSite: ${r.sameSite === "Not available" ? "n/a" : r.sameSite}`;
                    const expires = iso(r.expiry);
                    return [
                      <div key="c" className="r-break"><b>{r.name}</b><br />{r.domain}{pages.map((p) => <div key={p}>On {p}</div>)}</div>,
                      r.provider, r.party === "Not available" ? "Not recorded" : r.party,
                      <div key="l">{r.lifetime + (r.longLived ? " (long-lived)" : "")}{expires && <div>expires {expires}</div>}<div className="r-muted">{flags}</div></div>,
                      r.purpose + (r.purposeSource === "catalogue" ? "" : r.purposeSource === "category" ? " (from its classification only)" : ""),
                      r.behaviour.before, r.behaviour.accept, r.behaviour.reject,
                    ];
                  })}
                />
              </div>
            ))}
          </>
        )}
      </>
    ), { count: plural(inv.total, "cookie"), appendix: true });

    const trackerRank = (t: { category: string | null }) => (t.category === "marketing" ? 0 : t.category === "analytics" ? 1 : t.category ? 3 : 2);
    const trackers = [...ev.trackers].sort((a, b) => trackerRank(a) - trackerRank(b));
    add("requests", "Third-party requests and scripts", (
      <>
        {trackers.length === 0 ? <Para>No third-party requests were recorded.</Para> : (
          <>
            <Para small muted>Listed with marketing first, then analytics, then everything else. A request shows the browser contacted a service; it does not show that a tracker ran or what it sent.</Para>
            <Table
              className="r-dense"
              head={[{ h: "Vendor", w: "88px" }, { h: "Category", w: "62px" }, { h: "Seen", w: "84px" }, { h: "Request URL" }, { h: "On page", w: "100px" }]}
              rows={trackers.map((t) => [<b key="v">{t.vendor ?? "Unclassified"}</b>, t.category ?? "unclassified", states(t.consent_states), <span key="u" className="r-break">{t.script_src}</span>, <span key="p" className="r-break">{bare(t.page_url)}</span>])}
            />
          </>
        )}
      </>
    ), { count: plural(trackers.length, "request"), appendix: true });

    add("policies", "Policies found", (
      ev.policies.length === 0 ? <Para>No privacy, cookie or terms page was found on the pages scanned.</Para>
        : <Table head={[{ h: "Type", w: "130px" }, { h: "URL" }]} rows={ev.policies.map((p) => [<b key="t">{label(p.policy_type)}</b>, <a key="u" className="r-break" href={p.url}>{p.url}</a>])} />
    ), { count: plural(ev.policies.length, "policy", "policies"), appendix: true });

    add("services", "Third-party services", (
      ev.third_party_services.length === 0 ? <Para>No third-party service was identified.</Para>
        : <Table head={[{ h: "Service", w: "150px" }, { h: "Category", w: "100px" }, { h: "Domains" }]} rows={ev.third_party_services.map((s) => [<b key="n">{s.service_name}</b>, s.category ?? "unclassified", <span key="d" className="r-break">{s.domains.join(", ")}</span>])} />
    ), { count: plural(ev.third_party_services.length, "service"), appendix: true });

    if (v) {
      const hidden = new Set(v.allChecks.filter((c) => !isReportCategory(c.category)).map((c) => c.id));
      const groups = v.groups.map((g) => ({ ...g, entries: g.entries.filter((e) => !hidden.has(e.id)) })).filter((g) => g.entries.length);
      const outcomeTone = (key: string): Tone => (key === "confirmed" ? "bad" : key === "unable_to_verify" ? "mute" : "warn");
      add("findings", "Compliance findings in full", (
        <>
          {groups.length === 0 && <Para>No findings were recorded for the checks that could be tested.</Para>}
          {groups.map((g) => (
            <div key={g.key}>
              <h3 className="r-h3">{g.title} ({g.entries.length})</h3>
              {g.entries.map((e) => (
                <div className="r-item" key={e.id}>
                  <div className="r-item-head"><b>{stripId(e.title)}</b><span>
                    <Pill tone={outcomeTone(g.key)}>{e.outcome}</Pill>{e.severity && !/not rated/.test(e.severity) && <> <Pill tone="warn">{e.severity.replace(/^Severity:\s*/, "")}</Pill></>}
                  </span></div>
                  {e.legal && <Para small muted indent>Legal basis: {formatRef(e.legal)}</Para>}
                  {e.timingNote && <Para small muted indent>{e.timingNote}</Para>}
                  {e.observed.map((o) => <Para key={o} small indent>Observed: {stripRuleCodes(o)}</Para>)}
                  {e.pages.length > 0 && <Para small muted indent>Pages: {e.pages.join(", ")}</Para>}
                  {e.explanation && <Para small indent>{stripRuleCodes(e.explanation)}</Para>}
                  {e.missing.map((m) => <Para key={m} small indent>Missing: {m}</Para>)}
                  {e.remediation && <Para small indent>Remediation: {e.remediation}</Para>}
                </div>
              ))}
            </div>
          ))}
        </>
      ), { count: plural(groups.reduce((n, g) => n + g.entries.length, 0), "finding"), appendix: true });
    }
  }

  const listed = appendixOnly ? sections.filter((x) => !ON_FIXED_PAGES.has(x.id)) : sections;
  const firstAppendix = appendixOnly ? -1 : listed.findIndex((x) => x.appendix);
  return (
    <article className={`rpt${appendixOnly ? " rpt-appendix" : ""}`} data-testid={appendixOnly ? "rpt-appendix" : "rpt"} lang="en">
      {!appendixOnly && !noFront && <div className="r-screen-brand" aria-hidden="true">Consiva.ai · Privacy | Compliance | Trust</div>}
      {!noFront && <div className="r-title">
        <h1>{appendixOnly ? "Appendix: the evidence in full" : "Privacy Compliance Report"}</h1>
        <p className="r-muted">{appendixOnly ? "Everything the scan recorded for this site, and the detail behind the pages before it. The figures are the same ones." : "Evidence from a live scan of the main domain: before consent, after Accept and after Reject."}</p>
        {model.provisional && <p className="r-small r-warn-text">Provisional: some findings have not yet been reviewed by a person.</p>}
      </div>}
      {!appendixOnly && !noFront && <Hero model={model} />}
      {!appendixOnly && !noFront && model.analysisNote && <p className="r-small r-bold r-warn-text" data-testid="rpt-analysis-note">{model.analysisNote}</p>}
      <nav className="r-contents" aria-label="Contents">
        <b>CONTENTS</b>
        <ol>{listed.map((x, i) => <li key={x.id}><a href={`#${x.id}`}>{i + 1}  {x.title}</a></li>)}</ol>
      </nav>
      {listed.map((x, i) => (
        <div key={x.id} className={i === firstAppendix ? "r-appendix-start" : undefined}>
          <Sec n={i + 1} id={x.id} title={x.title} count={x.count} appendix={x.appendix}>{x.node}</Sec>
        </div>
      ))}
      <section className="r-about" data-testid="rpt-about">
        <b>About this report</b>
        <p>{REPORT_DISCLAIMER}</p>
      </section>
    </article>
  );
}
