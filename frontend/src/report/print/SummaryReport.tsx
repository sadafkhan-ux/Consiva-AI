import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { ReportModel } from "../buildReport";
import { buildSummaryReport, COOKIE_ROWS, DISCLAIMER, provisionLines, SELECTION_RULE, SIGNUP_SUB, SIGNUP_TEXT, SIGNUP_URL, SUMMARY_PAGES, weightage, type CheckRow, type SummaryView } from "../summaryReport";
import { CONSIVA_LOGO_PNG } from "../consivaLogo";
import { FrameworkBlock, SummaryTiles } from "./Blocks";
import { BigRing, Pill, SheetPageStyle } from "./PageParts";
import "./pages.css";
import "./summary.css";

/** How far a page's layout may be tightened: 0 is the full layout, each step sets it a little closer (spacing, then the optional explanations, then the type and the picture; see summary.css). */
export const MAX_LAYOUT_FIT = 3;
/** Only when the layout is as tight as it goes does a page show fewer cookies: this many fewer for each further step, never below COOKIE_MIN. */
export const COOKIE_STEP = 3;
export const COOKIE_MIN = 4;
/** The last fit step of a page: the layout steps, then enough cookie steps to go from the most cookies a page lists down to COOKIE_MIN. */
export const MAX_FIT = MAX_LAYOUT_FIT + Math.ceil((COOKIE_ROWS - COOKIE_MIN) / COOKIE_STEP);
/** The step before the first: the roomier layout both pages start in. */
export const ROOMY = -1;
/** The checkpoints page 1 always keeps: when it is too full the rest move to page 2, never below this many. */
export const MIN_ON_PAGE_1 = 4;

/** The cookies a page lists at a fit step, of `total` observed. */
export const cookieRowsAt = (step: number, total: number): number => {
  const all = Math.min(total, COOKIE_ROWS);
  return step <= MAX_LAYOUT_FIT ? all : Math.max(Math.min(COOKIE_MIN, all), all - COOKIE_STEP * (step - MAX_LAYOUT_FIT));
};

/**
 * Where the checkpoint table is cut between the two pages. Page 1 holds all the checkpoints, the assessed ones first and then those that could not be assessed; `moved` of them, from the
 * end of the list, go to the top of page 2 when page 1 cannot hold them (see SummaryReport), never leaving page 1 with fewer than MIN_ON_PAGE_1. Every checkpoint is on exactly one page.
 */
export function splitCheckpoints(assessed: CheckRow[], unassessed: CheckRow[], moved: number): { first: CheckRow[]; second: CheckRow[] } {
  const all = [...assessed, ...unassessed].map((r, i) => ({ ...r, n: i + 1 }));
  const keep = Math.max(Math.min(MIN_ON_PAGE_1, all.length), all.length - Math.max(0, moved));
  return { first: all.slice(0, keep), second: all.slice(keep) };
}

type Frame = { domain: string; scannedAt: string; headerPages: string; reportId: string };

/** One A4 sheet. A black band carries the logo and what identifies the report (the scanned site, the scan time, the report id) and the page number, in white; there is no footer. */
function Sheet({ n, frame, fit, testId, children }: { n: number; frame: Frame; fit: number; testId: string; children: ReactNode }) {
  // Both pages start at step -1, roomier (larger type, more air); a page that does not fit falls back to step 0, the normal layout, and only then is it tightened.
  const steps = (fit === ROOMY ? " sm-roomy" : "") + Array.from({ length: Math.max(0, Math.min(fit, MAX_LAYOUT_FIT)) }, (_, i) => ` sm-fit-${i + 1}`).join("");
  return (
    <section className={`pg pg-free pg-summary${steps}`} data-testid={testId} data-page={n} data-fit={fit}>
      <header className="sm-head" data-testid={`sm-head-${n}`}>
        <img className="sm-logo" src={CONSIVA_LOGO_PNG} alt="Consiva.ai" data-testid="sm-logo" />
        <div className="sm-head-r">
          <b>{frame.domain}</b>
          <div>Scanned {frame.scannedAt}{frame.headerPages ? ` · ${frame.headerPages}` : ""}</div>
          <div>Report ID: {frame.reportId} · <span className="sm-pageno">{`Page ${n} of ${SUMMARY_PAGES}`}</span></div>
        </div>
      </header>
      <div className="pg-body">{children}</div>
    </section>
  );
}

const H = ({ title, note }: { title: string; note?: string }) => <h2 className="sm-h">{title}{note && <span>{note}</span>}</h2>;

function CheckpointRow({ r, sameAs }: { r: CheckRow; sameAs: number | null }) {
  const prov = provisionLines(r.provisions);
  return (
    <tr className={r.scored ? undefined : "sm-unscored"} data-testid="sm-checkpoint" data-status={r.status.label}>
      <td className="sm-n">{r.n}</td>
      <td className="sm-cp">
        <b>{r.title}</b>
        {/* The Act and the Rules the checkpoint rests on sit on the title's own line. */}
        {prov.length > 0 && <span className="sm-prov" data-testid="sm-prov">{prov.map((l) => <span key={l.label + l.text}>{l.label && <b>{l.label}: </b>}{l.text}</span>)}</span>}
        {/* The evidence is given whole. When the same passage supports an earlier checkpoint it is shown there and referred to here, so it is read once, not cut. */}
        {r.note && <span className="sm-ev" data-testid="sm-evidence">{sameAs !== null ? `Same evidence as checkpoint ${sameAs}: see the passage quoted there.` : r.note}</span>}
      </td>
      <td><Pill s={r.status} /></td>
      <td className="sm-num">{r.scored ? weightage(r.awarded ?? 0) : "–"}</td>
      <td className="sm-num">{r.scored ? weightage(r.max ?? 0) : "–"}</td>
    </tr>
  );
}

/** A table of checkpoints; `seen` carries the evidence already printed on this page and the page before it, so a passage repeated for a second checkpoint is referred to, not printed again. */
function CheckpointTable({ rows, seen, total, testId }: { rows: CheckRow[]; seen: Map<string, number>; total?: { awarded: number; max: number }; testId: string }) {
  return (
    <table className="pg-table sm-table sm-checks" data-testid={testId}>
      <thead><tr><th>#</th><th>Checkpoint and supporting evidence</th><th>Status</th><th className="sm-num">Weightage Awarded</th><th className="sm-num">Maximum Weightage</th></tr></thead>
      <tbody>
        {rows.map((r) => {
          const first = r.note.length > 80 ? seen.get(r.note) : undefined;
          if (r.note.length > 80 && first === undefined) seen.set(r.note, r.n);
          return <CheckpointRow key={r.id} r={r} sameAs={first ?? null} />;
        })}
        {rows.length === 0 && <tr><td colSpan={5} className="pg-muted-cell">No applicable checkpoint could be identified for this scan.</td></tr>}
        {total && <tr className="sm-total"><td colSpan={3}>Total Weightage (assessed checkpoints)</td><td className="sm-num">{weightage(total.awarded)}</td><td className="sm-num">{weightage(total.max)}</td></tr>}
      </tbody>
    </table>
  );
}

/** Page 1: the site and its first view, framework readiness, the one Compliance Score beside the key metrics, and the applicable checkpoints with their status and evidence. */
function Page1({ s, frame, fit, rows, seen }: { s: SummaryView; frame: Frame; fit: number; rows: CheckRow[]; seen: Map<string, number> }) {
  const { page } = s;
  const { preview, hero } = page;
  const applicable = s.assessedRows.length + s.unassessedRows.length;
  return (
    <Sheet n={1} frame={frame} fit={fit} testId="sm-page-1">
      <div className="sm-hero" data-testid="sm-hero">
        <div className="sm-hero-text">
          <div className="sm-domain">{preview.domain}</div>
          <h1>Website Privacy Readiness Report</h1>
        </div>
        <figure className="sm-shot" data-testid="pg-hero-preview">
          {preview.src ? (
            <>
              <div className="pg-browser"><i /><i /><i /><span>{preview.address ?? `https://${preview.domain}`}</span></div>
              <img src={preview.src} alt={`Homepage of ${preview.domain} as the scanner saw it`} data-testid="pg-hero-screenshot" />
              <figcaption>First view of {preview.domain} as scanned on {page.scannedAt}</figcaption>
            </>
          ) : (
            <div className="pg-shot-missing" data-testid="pg-hero-missing"><b>Homepage preview not available</b><span>{preview.unavailable}</span></div>
          )}
        </figure>
      </div>

      <div className="sm-fw" data-testid="sm-framework"><FrameworkBlock view={{ ...page, framework: s.framework }} hideScore /></div>

      <div className="sm-top">
        <div className="sm-card" data-testid="sm-score">
          <b className="sm-card-title">Compliance Score</b>
          <div className="pg-score-ring"><BigRing percent={hero.percent} label={hero.percentLabel} id="summary" /></div>
          <div className="sm-score-line" data-testid="sm-total-line">Total Weightage: {weightage(s.totalAwarded)} / {weightage(s.totalMax)}</div>
          <div className="sm-score-note" data-testid="sm-counts">{s.assessedRows.length} of {applicable} applicable checkpoints assessed</div>
        </div>
        <SummaryTiles view={{ ...page, tiles: s.tiles }} />
      </div>

      <H title="Applicable checkpoints" note="Weightage is given only for a checkpoint that was assessed" />
      <CheckpointTable rows={rows} seen={seen} total={{ awarded: s.totalAwarded, max: s.totalMax }} testId="sm-checkpoints" />
    </Sheet>
  );
}

/** Page 2: the checkpoints that did not fit on page 1, cookies, the three consent states side by side, the website's privacy elements, the officers' public contact details, the pages scanned, actions and limits, the sign-up link and the disclaimer. */
function Page2({ s, frame, fit, rows, seen }: { s: SummaryView; frame: Frame; fit: number; rows: CheckRow[]; seen: Map<string, number> }) {
  const { page } = s;
  const cc = s.cookieCounts;
  const cookieLines = s.cookieLines.slice(0, cookieRowsAt(fit, s.cookieTotal));
  const cookiesMore = s.cookieTotal - cookieLines.length;
  const untestedChoices = [...(page.consent && !page.consent.acceptTested ? ["Accept"] : []), ...(page.consent && !page.consent.rejectTested ? ["Reject"] : [])];
  return (
    <Sheet n={2} frame={frame} fit={fit} testId="sm-page-2">
      {rows.length > 0 && (
        <>
          <H title="Applicable checkpoints (continued)" />
          <CheckpointTable rows={rows} seen={seen} testId="sm-checkpoints-2" />
        </>
      )}

      <H title="Cookies observed" />
      <div className="sm-strip" data-testid="sm-cookie-counts">
        <span><b>{cc.found}</b> found</span><span><b>{cc.thirdParty}</b> third-party</span><span><b>{cc.beforeConsent}</b> before consent</span>
        <span><b>{cc.afterAccept === null ? "Not Tested" : cc.afterAccept}</b> after Accept</span><span><b>{cc.afterReject === null ? "Not Tested" : cc.afterReject}</b> after Reject</span>
        {cc.byCategory.map((g) => <span key={g.label} className="sm-cat">{g.label}: <b>{g.count}</b></span>)}
      </div>
      {cookieLines.length === 0 ? <p className="pg-muted">No cookies were recorded on the pages scanned.</p> : (
        <table className="pg-table sm-table sm-cookies" data-testid="sm-cookies">
          <thead><tr><th>Cookie</th><th>Domain</th><th>Classification</th><th>Before consent</th><th>After Accept</th><th>After Reject</th></tr></thead>
          <tbody>
            {cookieLines.map((c) => (
              <tr key={`${c.name}|${c.domain}`}><td className="sm-break"><b>{c.name}</b></td><td className="sm-break">{c.domain}</td><td>{c.classification}</td><td>{c.before}</td><td>{c.accept}</td><td>{c.reject}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      {cookiesMore > 0 && <p className="sm-fine" data-testid="sm-cookies-more">+ {cookiesMore} more {cookiesMore === 1 ? "cookie" : "cookies"} observed, listed in the long report.</p>}

      <H title="Consent states compared" />
      {s.consentRows.length === 0 ? <p className="pg-muted">The scan evidence was not loaded, so the consent comparison is not shown.</p> : (
        <table className="pg-table sm-table sm-consent" data-testid="sm-consent">
          <thead><tr><th>Metric</th><th>Before Consent</th><th>After Accept</th><th>After Reject</th></tr></thead>
          <tbody>{s.consentRows.map((r) => <tr key={r.label}><td>{r.label}</td>{r.cells.map((x, i) => <td key={i} className={x === "Not Tested" ? "sm-nt" : undefined}>{x}</td>)}</tr>)}</tbody>
        </table>
      )}
      {untestedChoices.length > 0 && <p className="sm-fine" data-testid="sm-untested">{untestedChoices.join(" and ")} {untestedChoices.length === 1 ? "was" : "were"} not tested (no click was confirmed), so nothing is reported for what the site does after {untestedChoices.length === 1 ? "that choice" : "either choice"}. A cookie held in such a pass is marked “Seen, no choice made”.</p>}
      <div className="sm-strip" data-testid="sm-tech">
        {s.requests !== null && <span><b>{s.requests}</b> third-party requests</span>}
        {s.domains !== null && <span><b>{s.domains}</b> third-party domains</span>}
        {s.knownRequests !== null && <span><b>{s.knownRequests}</b> analytics and marketing requests</span>}
        {s.techRows.map((t) => <span key={t.name} className="sm-cat">{t.name} · {t.type} · {t.requests}</span>)}
        {s.techMore > 0 && <span className="sm-cat">+ {s.techMore} more</span>}
      </div>

      <H title="Website privacy elements" note="publicly detected" />
      {s.elements.length === 0 ? <p className="pg-muted">The scan evidence was not loaded, so the privacy elements are not shown.</p> : (
        <div className="sm-els" data-testid="sm-elements">
          {s.elements.map((e) => <div key={e.title} className="sm-el"><b>{e.title}</b><Pill s={{ label: e.label, tone: e.tone }} />{e.note && <small>{e.note}</small>}</div>)}
        </div>
      )}

      {s.contacts.length > 0 && (
        <div className="sm-contacts" data-testid="sm-contacts">
          {s.contacts.map((c) => (
            <div key={c.title} data-testid="sm-contact"><b>{c.title}</b>{c.lines.map((l) => <span key={l}>{l}</span>)}{c.source && <small>{c.source}</small>}</div>
          ))}
        </div>
      )}

      <H title="Pages scanned" note={s.selectionNotes[0]} />
      {s.selection.length === 0 ? <p className="pg-muted">The page list was not loaded for this scan.</p> : (
        <table className="pg-table sm-table sm-pages" data-testid="sm-pages">
          <thead><tr><th>#</th><th>Page</th><th>Why it was chosen</th><th>Result</th></tr></thead>
          <tbody>
            {s.selection.map((r) => (
              <tr key={r.url} className={r.scanned ? undefined : "sm-unscored"}><td>{r.n}</td><td className="sm-url">{r.url}</td><td>{r.reason}</td><td className="sm-nowrap">{r.status}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="sm-fine sm-rule" data-testid="sm-rule">{SELECTION_RULE}</p>

      <div className="sm-row2 sm-row-notes">
        <div><H title="Recommended actions" /><ul className="sm-list" data-testid="sm-next">{s.nextSteps.map((t) => <li key={t}>{t}</li>)}</ul></div>
        <div><H title="Scope and limitations" /><ul className="sm-list" data-testid="pg-limits">{s.limitations.map((l) => <li key={l}>{l}</li>)}</ul></div>
      </div>

      <div className="sm-cta" data-testid="sm-cta">
        <div className="sm-cta-text"><b>{SIGNUP_TEXT}</b><span>{SIGNUP_SUB}</span></div>
        <a className="sm-cta-link" href={SIGNUP_URL} data-testid="sm-signup">Sign Up</a>
      </div>

      <div className="sm-disc">
        <b>Disclaimer</b>
        <p data-testid="sm-disclaimer">{DISCLAIMER}</p>
      </div>
    </Sheet>
  );
}

/**
 * The two-page report (variant "summary"). Page 1: the scanned site and its first view, framework readiness, the one Compliance Score beside the key metrics, and the applicable
 * checkpoints with their status and the evidence each rests on. Page 2: any checkpoints that did not fit on page 1, the cookies and what each did in each consent state, the three states
 * compared, the website's privacy elements, the officers' public contact details when the website gives them, the pages scanned and why, the actions, the limits, the sign-up link and the
 * disclaimer. Only what the website's public content and the scan support is shown; every figure comes from the report model.
 */
export function SummaryReport({ model }: { model: ReportModel }) {
  const s = buildSummaryReport(model);
  const { page } = s;
  const frame: Frame = { domain: page.domain, scannedAt: page.scannedAt, headerPages: page.headerPages, reportId: page.reportId };
  const doc = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState<[number, number]>([ROOMY, ROOMY]);
  const [moved, setMoved] = useState(0);
  // Each sheet is a fixed A4 page. After every layout the sheet's content is measured against its room; one that does not fit is adjusted one step and measured again, until it fits or the
  // last step is reached. Page 1 first goes from roomy to the normal layout and its first tightening; if it still does not hold its checkpoints, whole checkpoint rows move to the top of
  // page 2 (the text stays whole and readable) before anything is made smaller. Page 2 tightens its layout and then lists fewer cookies. The body stretches to fill the sheet, so content that
  // fits leaves its scroll height equal to its height, and content that does not fit makes the scroll height larger.
  useLayoutEffect(() => {
    const sheets = doc.current?.querySelectorAll("section.pg");
    if (!sheets) return;
    const over = (i: number) => {
      const body = sheets[i]?.querySelector<HTMLElement>(".pg-body");
      return !!body && body.scrollHeight > body.clientHeight + 1;
    };
    const next: [number, number] = [fit[0], fit[1]];
    let nextMoved = moved;
    let changed = false;
    if (over(0)) {
      const canMove = s.assessedRows.length + s.unassessedRows.length - nextMoved > MIN_ON_PAGE_1;
      if (next[0] < 1) { next[0] += 1; changed = true; }
      else if (canMove) { nextMoved += 1; changed = true; }
      else if (next[0] < MAX_FIT) { next[0] += 1; changed = true; }
    }
    if (over(1) && next[1] < MAX_FIT) { next[1] += 1; changed = true; }
    if (changed) { setFit(next); setMoved(nextMoved); }
  });
  const { first, second } = splitCheckpoints(s.assessedRows, s.unassessedRows, moved);
  const seen = new Map<string, number>();
  return (
    <div className="pg-doc" data-testid="summary-report" ref={doc}>
      <SheetPageStyle />
      <Page1 s={s} frame={frame} fit={fit[0]} rows={first} seen={seen} />
      <Page2 s={s} frame={frame} fit={fit[1]} rows={second} seen={seen} />
    </div>
  );
}
