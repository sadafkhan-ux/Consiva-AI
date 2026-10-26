import type { ReportModel } from "../buildReport";
import { buildPageReport, FULL_PAGES, type SeriesPoint } from "../pageReports";
import { CONSIVA_UPDATES, OMITTED_DUTIES_NOTE } from "../reportScope";
import { AreasBlock, ConsentTable, CtaCard, ElementsGrid, FindingsList, FrameworkBlock, ScoreCard, SummaryTiles, UpgradeBand } from "./Blocks";
import { Icon, PageFrame, Pill, SecHead, SheetPageStyle, type IconName } from "./PageParts";
import { ScanTransparency } from "./Transparency";
import "./pages.css";

// The Full Scan report: eight fixed A4 pages, laid out as the scope document's Full Scan benchmark (section 2.2), as a readable report rather than a
// collage. The long evidence appendix (every cookie, request, form, policy and finding) follows these pages in the printed report.

const TYPE_TONE: Record<string, string> = { Marketing: "#2457d6", Analytics: "#7a4dff", Functional: "#0d9aa8" };

/** The chart: one area per kind of request across the three consent states, on one scale. A state the scan did not test is marked, not drawn as zero. */
function RequestsChart({ series }: { series: SeriesPoint[] }) {
  const W = 640, H = 270, L = 34, R = 14, T = 12, B = 30;
  const max = Math.max(1, ...series.map((s) => s.analytics + s.functional + s.other));
  const step = max <= 5 ? 1 : max <= 10 ? 2 : max <= 25 ? 5 : max <= 50 ? 10 : max <= 100 ? 20 : Math.pow(10, Math.floor(Math.log10(max)));
  const top = Math.ceil(max / step) * step;
  const x = (i: number) => L + (i * (W - L - R)) / (series.length - 1);
  const y = (v: number) => T + (1 - v / top) * (H - T - B);
  const kinds = [
    { key: "analytics" as const, name: "Analytics & Marketing", colour: "#7a4dff" },
    { key: "functional" as const, name: "Functional", colour: "#2f7bff" },
    { key: "other" as const, name: "Other", colour: "#9aa0bd" },
  ];
  const ticks: number[] = [];
  for (let t = 0; t <= top; t += step) ticks.push(t);
  const tested = series.map((s, i) => ({ s, i })).filter(({ s }) => s.tested);
  return (
    <figure className="pg-chart" data-testid="pg-chart">
      <ul className="pg-legend pg-legend-chart">{kinds.map((k) => <li key={k.key}><i style={{ background: k.colour }} />{k.name}</li>)}</ul>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Third-party requests by consent state">
        {ticks.map((t) => <g key={t}><line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="#e6eaf8" /><text x={L - 6} y={y(t) + 3} textAnchor="end" fontSize="9" fill="#5d6086">{t}</text></g>)}
        {kinds.map((k) => {
          const pts = tested.map(({ s, i }) => `${x(i)},${y(s[k.key])}`);
          if (pts.length < 1) return null;
          const area = tested.length > 1 ? `M${x(tested[0].i)},${y(0)} L${pts.join(" L")} L${x(tested[tested.length - 1].i)},${y(0)} Z` : "";
          return <g key={k.key}>{area && <path d={area} fill={k.colour} opacity="0.12" />}<polyline points={pts.join(" ")} fill="none" stroke={k.colour} strokeWidth="2" />{tested.map(({ s, i }) => <circle key={i} cx={x(i)} cy={y(s[k.key])} r="3" fill={k.colour} />)}</g>;
        })}
        {series.map((s, i) => <text key={s.state} x={x(i)} y={H - 10} textAnchor={i === 0 ? "start" : i === series.length - 1 ? "end" : "middle"} fontSize="10" fill="#111b4d">{s.state}{s.tested ? "" : " (not tested)"}</text>)}
      </svg>
    </figure>
  );
}

const CARD_ICONS: IconName[] = ["scale", "key", "board", "doc", "gavel"];

export function FullReport({ model }: { model: ReportModel }) {
  const view = buildPageReport(model, "full");
  const frame = { variant: "full" as const, of: FULL_PAGES, domain: view.domain, scannedAt: view.scannedAt, headerPages: view.headerPages, reportId: view.reportId };
  const { preview } = view;
  const vis = model.visual;
  const [scanDate, scanTime] = view.scannedAt.split(", ");
  return (
    <div className="pg-doc" data-testid="full-report">
      <SheetPageStyle />
      <PageFrame {...frame} n={1}>
        <div className="pg-cover" data-testid="pg-hero">
          <div>
            <div className="pg-cover-domain">{preview.domain}</div>
            <h1>Privacy<br />Compliance<br />Report</h1>
            <p>Evidence from a live scan before consent, after Accept and after Reject, based on India's DPDP Act, 2023 &amp; Rules and global best practices.</p>
            <div className="pg-hero-meta pg-hero-meta-light">
              <div><Icon name="calendar" size={18} /><span><b>{scanDate}</b><br />{scanTime}</span></div>
              <div><Icon name="page" size={18} /><span><b>{view.pagesLine}</b><br />pages scanned{vis?.pageLimit ? <><br />(limit {vis.pageLimit})</> : null}</span></div>
            </div>
          </div>
          <figure className="pg-hero-shot pg-hero-shot-full" data-testid="pg-hero-preview">
            {preview.src ? (
              <>
                <div className="pg-browser"><i /><i /><i /><span>{preview.address ?? `https://${preview.domain}`}</span></div>
                <img src={preview.src} alt={`Homepage of ${preview.domain} as the scanner saw it`} data-testid="pg-hero-screenshot" />
                <figcaption>First view of {preview.domain} as scanned on {view.scannedAt}</figcaption>
              </>
            ) : (
              <div className="pg-shot-missing" data-testid="pg-hero-missing"><b>Homepage preview not available</b><span>{preview.unavailable}</span></div>
            )}
          </figure>
        </div>
        <ScoreCard view={view} id="full" />
        <SecHead n={1} title="Quick Summary" />
        <SummaryTiles view={view} />
        {model.analysisNote && <p className="pg-callout" data-testid="pg-analysis-note">{model.analysisNote}</p>}
        <div className="pg-row2 pg-row2-even"><CtaCard view={view} /><div className="pg-limits pg-limits-flat" data-testid="pg-limits"><b>Scope and limits</b><ul>{view.limits.map((l) => <li key={l}>{l}</li>)}</ul></div></div>
      </PageFrame>

      <PageFrame {...frame} n={2}>
        <SecHead n={2} title="Framework Readiness" />
        <FrameworkBlock view={view} />
        <div className="pg-info"><Icon name="info" size={18} /><p>{CONSIVA_UPDATES.actStatus}</p></div>
        <SecHead n={3} title="Privacy Areas" note="Each bar is the share of weighted points earned. Coverage shows how much was actually tested." />
        <AreasBlock view={view} />
        <SecHead n={4} title="Top Findings" aside={view.findingsMore > 0 ? `+ ${view.findingsMore} more in the Appendix` : undefined} />
        <FindingsList view={view} />
        {view.groupCounts && <p className="pg-muted pg-more" data-testid="pg-group-counts">{view.groupCounts}</p>}
        {view.upcoming.length > 0 && (
          <>
            <h3 className="pg-h3">When the remaining duties start <span>(readiness, not breach)</span></h3>
            <ul className="pg-dates" data-testid="pg-upcoming">{view.upcoming.map((u) => <li key={u}><Icon name="clock" size={14} />{u}</li>)}</ul>
          </>
        )}
      </PageFrame>

      <PageFrame {...frame} n={3}>
        <SecHead n={5} title="Consent Behaviour" note="What was observed on the scanned pages before consent, after Accept and after Reject." />
        <ConsentTable view={view} />
        {vis && <p className="pg-callout" data-testid="pg-takeaway"><b>Key takeaway.</b> {vis.consent.takeaway}</p>}
        <SecHead n="5a" title="Scan transparency" note="What was scanned, what was tested, and what could not be verified." />
        <ScanTransparency view={model.transparency} compact />
      </PageFrame>

      <PageFrame {...frame} n={4}>
        <SecHead n={6} title="Tracking Technologies Detected" note="Technologies identified from network requests, scripts and cookies on the scanned pages." />
        <div className="pg-stat4">
          {[[view.tiles[3]?.value, "Analytics & marketing requests"], [view.tiles[2]?.value, "Total third-party requests"], [view.tiles[4]?.value, "Cookies set"], [view.tiles[5]?.value, "Third-party domains"]].map(([val, label]) => (
            <div key={label}><b>{val ?? "—"}</b><span>{label}</span></div>
          ))}
        </div>
        <h3 className="pg-h3">Top technologies <span>({Math.min(view.techRows.length, 8)} shown)</span></h3>
        <table className="pg-table" data-testid="pg-tech-table">
          <thead><tr><th>Technology</th><th>Type</th><th>Requests</th><th>Seen in</th><th>Category</th></tr></thead>
          <tbody>
            {view.techRows.length === 0 && <tr><td colSpan={5}>No third-party technologies were recorded on the pages scanned.</td></tr>}
            {view.techRows.map((t) => <tr key={t.name}><td><b>{t.name}</b></td><td style={{ color: TYPE_TONE[t.type] ?? "#555a85" }}>{t.type}</td><td>{t.requests}</td><td>{t.seen}</td><td>{t.category}</td></tr>)}
          </tbody>
        </table>
        {view.techRowsMore > 0 && <p className="pg-muted pg-more">+ {view.techRowsMore} more: every request is listed in the Appendix (Third-party requests and scripts).</p>}
        <SecHead n={7} title="Third-party Domains" note={`(${view.domains.shown.length + view.domains.others} found)`} />
        <div className="pg-chips" data-testid="pg-domains">
          {view.domains.shown.length === 0 && <span className="pg-muted">No third-party domain was recorded.</span>}
          {view.domains.shown.map((d) => <span key={d}>{d}</span>)}
          {view.domains.others > 0 && <span>others ({view.domains.others})</span>}
        </div>
        <SecHead n={8} title="Requests over Time" note="(across consent states)" />
        <RequestsChart series={view.series} />
        <p className="pg-muted">Requests are counted per consent state, so one request seen in two states is counted in both. Totals above count each request once.</p>
      </PageFrame>

      <PageFrame {...frame} n={5}>
        <SecHead n={9} title="Privacy Elements on the Site" note="Key privacy-related elements found on the scanned pages." />
        <ElementsGrid view={view} />
        <h3 className="pg-h3">Policies found <span>({view.policies.length})</span></h3>
        <table className="pg-table" data-testid="pg-policies">
          <thead><tr><th>Type</th><th>URL</th></tr></thead>
          <tbody>{view.policies.length === 0 ? <tr><td colSpan={2}>No privacy, cookie or terms page was found in the scanned scope.</td></tr> : view.policies.slice(0, 6).map((p) => <tr key={p.url}><td><b>{p.type}</b></td><td>{p.url}</td></tr>)}</tbody>
        </table>
        <h3 className="pg-h3">Forms found <span>({view.forms.length})</span></h3>
        <table className="pg-table" data-testid="pg-forms">
          <thead><tr><th>Form</th><th>Fields</th></tr></thead>
          <tbody>{view.forms.length === 0 ? <tr><td colSpan={2}>No forms were found in the scanned scope.</td></tr> : view.forms.slice(0, 4).map((f) => <tr key={f.title}><td><b>{f.title}</b></td><td>{f.fields}</td></tr>)}</tbody>
        </table>
        {view.policies.length > 6 && <p className="pg-muted pg-more">+ {view.policies.length - 6} more policy pages in the Appendix (Policies found).</p>}
        {view.forms.length > 4 && <p className="pg-muted pg-more">+ {view.forms.length - 4} more forms, every field listed, in the Appendix (Forms found).</p>}
        <div className="pg-limits" data-testid="pg-limits"><b>Scope and limits</b><ul>{view.limits.map((l) => <li key={l}>{l}</li>)}</ul></div>
        <div className="pg-info"><Icon name="info" size={18} /><p>A report is based on {vis ? vis.pagesScanned : "the"} scanned {vis && vis.pagesScanned === 1 ? "page" : "pages"}. Behaviour on additional pages, subdomains or login-gated areas may differ. "Not found" means not found in the scanned scope; it does not mean the item does not exist.</p></div>
      </PageFrame>

      <PageFrame {...frame} n={6}>
        <SecHead n={10} title="Every Check Behind the Score" note={`All checks, their weight, status and evidence. ${view.framework.counts.met + view.framework.counts.partly + view.framework.counts.notMet} of ${view.framework.counts.met + view.framework.counts.partly + view.framework.counts.notMet + view.framework.counts.unverified} applicable checks tested from outside.`} />
        <ul className="pg-legend pg-legend-status">
          {([["Pass", "good"], ["Partially met", "warn"], ["Not met", "bad"], ["Could not verify", "mute"], ["Not applicable", "mute"]] as const).map(([s, t]) => <li key={s}><Pill s={{ label: s, tone: t }} /></li>)}
        </ul>
        <table className="pg-table pg-checks" data-testid="pg-checks">
          <thead><tr><th>ID</th><th>What is checked</th><th>Weight</th><th>Status</th><th>Evidence</th></tr></thead>
          <tbody>
            {view.checks.length === 0 && <tr><td colSpan={5}>The score was not loaded, so no check is listed.</td></tr>}
            {view.checks.map((c) => <tr key={c.id}><td><b>{c.id}</b></td><td>{c.title}</td><td>{c.weight}</td><td><Pill s={c.status} /></td><td>{c.evidence}</td></tr>)}
          </tbody>
        </table>
        {view.checksMore > 0 && <p className="pg-muted pg-more">+ {view.checksMore} more {view.checksMore === 1 ? "check" : "checks"} in the Appendix (Every check behind the score).</p>}
        <p className="pg-muted pg-more">{OMITTED_DUTIES_NOTE}</p>
        <p className="pg-muted pg-more">Unknown results (could not verify, test failed, not assessed) are never counted as passed. A zero means a completed test observed zero.</p>
      </PageFrame>

      <PageFrame {...frame} n={7}>
        <SecHead n={11} title="Prioritised Fixes" note="Fix these items to improve your score and strengthen your privacy posture." />
        {view.fixes.length === 0 && <p className="pg-muted">No fix is listed: no check failed with enough evidence.</p>}
        <div className="pg-fixes" data-testid="pg-fixes">
          {view.fixes.map((f) => (
            <div className="pg-fix" key={f.rank}>
              <span className="pg-num pg-n0">{f.rank}</span>
              <div><b>{f.title}</b><p>{f.action}</p></div>
              <Pill s={{ label: f.priority, tone: f.tone }} />
            </div>
          ))}
        </div>
        <UpgradeBand view={view} full />
      </PageFrame>

      <PageFrame {...frame} n={8}>
        <SecHead n={12} title="DPDP Act and Rules" note="(Context for this report)" />
        <div className="pg-cards5" data-testid="pg-legal">
          {CONSIVA_UPDATES.cards.map((c, i) => (
            <div key={c.title}><Icon name={CARD_ICONS[i] ?? "doc"} size={22} /><b>{c.title}</b><span>{c.status}</span><span className="pg-muted">{c.when}</span></div>
          ))}
        </div>
        <div className="pg-info"><Icon name="info" size={18} /><p>{CONSIVA_UPDATES.scope}</p></div>
        <SecHead n={13} title="What's Next" />
        <div className="pg-cards4">
          {([["upload", "Upgrade to Full Scan", "Scan all pages, get detailed findings and legal mapping."], ["bell", "Continuous Monitoring", "Alerts for new trackers and consent changes."], ["book", "Regulatory Guidance", "DPDP, GDPR and CCPA guidance."], ["doc", "Evidence & Exports", "Download audit-ready reports."]] as [IconName, string, string][]).map(([ic, t, d]) => (
            <div key={t}><Icon name={ic} size={22} /><b>{t}</b><span className="pg-muted">{d}</span></div>
          ))}
        </div>
        <div className="pg-reportinfo" data-testid="pg-reportinfo">
          <b>Report Information</b>
          <dl>
            <dt>Domain</dt><dd>{view.domain}</dd>
            <dt>Scan time</dt><dd>{view.scannedAt}</dd>
            <dt>Pages scanned</dt><dd>{vis ? `${vis.pagesScanned} of ${view.pagesLine.includes("of") ? view.pagesLine.split(" of ")[1] : vis.pagesScanned}${vis.pageLimit ? ` (limit ${vis.pageLimit})` : ""}` : "Not recorded"}</dd>
            <dt>Consent states tested</dt><dd>{vis ? `${vis.statesTested} (Before, ${vis.consent.acceptTested ? "After Accept" : "After Accept not tested"}, ${vis.consent.rejectTested ? "After Reject" : "After Reject not tested"})` : "Not recorded"}</dd>
            <dt>Report ID</dt><dd>{view.reportId}</dd>
            <dt>Scoring and rule versions</dt><dd>{model.dpdpScore ? model.dpdpScore.versionsLine : "Not recorded"}</dd>
          </dl>
        </div>
        <p className="pg-disclaimer" data-testid="pg-disclaimer"><b>Important:</b> This report is an automated, point-in-time assessment of publicly accessible website signals within the stated scan scope. It is provided for informational and preliminary readiness purposes only; it is not legal advice, a legal opinion, an audit, a certification, or a guarantee of compliance with the Digital Personal Data Protection Act, 2023, the DPDP Rules, 2025, GDPR/ePrivacy, CCPA/CPRA or any other law. A passing score applies only to checks tested within the stated scope and does not establish full organisational compliance.</p>
      </PageFrame>
    </div>
  );
}
