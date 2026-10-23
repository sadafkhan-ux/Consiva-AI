import type { ReportModel } from "../buildReport";
import { buildPageReport, FREE_PAGES, type PageReportView } from "../pageReports";
import { AreasBlock, ConsentTable, CtaCard, ElementsGrid, FindingsList, FrameworkBlock, ScoreCard, SummaryTiles, TechCards, UpgradeBand } from "./Blocks";
import { Icon, PageFrame, SecHead, SheetPageStyle, SHORT_DISCLAIMER } from "./PageParts";
import "./pages.css";

type Frame = { variant: "free"; of: number; domain: string; scannedAt: string; reportId: string };

/** The first sheet of the Free report: the hero, the score and its call to action, the scan summary, framework readiness and the privacy areas. The long
 *  report opens with this same sheet as its cover (`numbered` off, since its own pages are counted after it). */
function FirstPage({ model, view, frame, tag, numbered = true }: { model: ReportModel; view: PageReportView; frame: Frame; tag?: string; numbered?: boolean }) {
  const { preview } = view;
  const vis = model.visual;
  const [scanDate, scanTime] = view.scannedAt.split(", ");
  return (
    <PageFrame {...frame} n={1} tag={tag} numbered={numbered}>
        <div className="pg-hero" data-testid="pg-hero">
          <div className="pg-hero-left">
            <div className="pg-hero-domain">{preview.domain}</div>
            <h1>Website Privacy<br />Readiness Report</h1>
            <p>Automated analysis of your website based on India's DPDP Act, 2023 &amp; Rules, global best practices and {vis ? vis.pagesScanned : "the"} scanned {vis && vis.pagesScanned === 1 ? "page" : "pages"}.</p>
            <div className="pg-hero-meta">
              <div><Icon name="calendar" size={18} /><span><b>{scanDate}</b><br />{scanTime}</span></div>
              <div><Icon name="page" size={18} /><span><b>{view.pagesLine}</b><br />pages scanned{vis?.pageLimit ? <><br />(Free scan limit)</> : null}</span></div>
              <div><Icon name="globe" size={18} /><span><b>Homepage</b><br />as scanned</span></div>
            </div>
          </div>
          <figure className="pg-hero-shot" data-testid="pg-hero-preview">
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

        <div className="pg-row2">
          <ScoreCard view={view} id="free" />
          <CtaCard view={view} />
        </div>

        <SecHead n={1} title="Scan Summary" note={`(from ${vis ? vis.pagesScanned : "the"} scanned ${vis && vis.pagesScanned === 1 ? "page" : "pages"})`} />
        <SummaryTiles view={view} />

        <SecHead n={2} title="Framework Readiness" note="(Website observable indicators)" />
        <FrameworkBlock view={view} />

        <SecHead n={3} title="Privacy Areas" note="(Based on applicable checks)" />
        <AreasBlock view={view} />

        <div className="pg-limits" data-testid="pg-limits">
          <b>Scope and limits</b>
          <ul>{view.limits.map((l) => <li key={l}>{l}</li>)}</ul>
        </div>
    </PageFrame>
  );
}

/** The cover of the long report: the Free report's first sheet, with no page number. */
export function ReportCover({ model }: { model: ReportModel }) {
  const view = buildPageReport(model, "free");
  const frame = { variant: "free" as const, of: 1, domain: view.domain, scannedAt: view.scannedAt, reportId: view.reportId };
  return <div className="pg-doc" data-testid="report-cover"><SheetPageStyle /><FirstPage model={model} view={view} frame={frame} tag="PRIVACY COMPLIANCE REPORT" numbered={false} /></div>;
}

/**
 * The Free Scan report: two fixed A4 pages, laid out as the scope document's Free Scan benchmark (section 2.1). Page 1: the first view of the scanned
 * site, the score with its coverage beside it, the scan summary, framework readiness and the privacy areas. Page 2: consent behaviour, technologies,
 * privacy elements, top findings, the limits of the scan and the footer call to action. Every figure is read from the report model.
 */
export function FreeReport({ model }: { model: ReportModel }) {
  const view = buildPageReport(model, "free");
  const frame = { variant: "free" as const, of: FREE_PAGES, domain: view.domain, scannedAt: view.scannedAt, reportId: view.reportId };
  return (
    <div className="pg-doc" data-testid="free-report">
      <SheetPageStyle />
      <FirstPage model={model} view={view} frame={frame} />

      <PageFrame {...frame} n={2}>
        <SecHead n={4} title="Consent Behaviour" note="What was observed on the scanned pages before consent, after Accept and after Reject." />
        <ConsentTable view={view} />
        {model.visual && <p className="pg-callout" data-testid="pg-takeaway"><b>Key takeaway.</b> {model.visual.consent.takeaway}</p>}

        <SecHead n={5} title="Tracking Technologies Detected" note={`(${view.techCards.length} shown)`} />
        <TechCards view={view} />
        {view.techMore > 0 && <p className="pg-muted pg-more">+ {view.techMore} more technologies are listed in the Full Scan report.</p>}

        <SecHead n={6} title="Privacy Elements on the Site" note="(Website observable)" />
        <ElementsGrid view={view} />

        <SecHead n={7} title="Top Findings" />
        <FindingsList view={view} />
        {view.findingsMore > 0 && <p className="pg-muted pg-more">+ {view.findingsMore} more {view.findingsMore === 1 ? "finding" : "findings"} in the Full Scan report.</p>}

        <UpgradeBand view={view} full={false} />
        <p className="pg-disclaimer" data-testid="pg-disclaimer"><b>Important:</b> {SHORT_DISCLAIMER} It is an automated, point-in-time assessment of publicly accessible website signals within the stated scan scope, and is not an audit, a certification or a guarantee of compliance with the DPDP Act, 2023, the DPDP Rules, 2025, GDPR/ePrivacy, CCPA/CPRA or any other law. A passing score applies only to checks tested within the stated scope and does not establish full organisational compliance. For a detailed assessment, engage a qualified privacy professional.</p>
      </PageFrame>
    </div>
  );
}
