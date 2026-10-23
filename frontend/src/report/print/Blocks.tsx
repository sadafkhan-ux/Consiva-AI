import type { ReactNode } from "react";
import { findingPill, type PageReportView } from "../pageReports";
import type { ElementStatus } from "../reportVisual";
import { BigRing, Flag, Icon, Mark, Pill, type IconName } from "./PageParts";

// The blocks both fixed-page reports are built from. Each one only lays out values already worked out in report/pageReports.ts.

export function ScoreCard({ view, id }: { view: PageReportView; id: string }) {
  const { hero } = view;
  return (
    <div className="pg-scorecard" data-testid={`pg-score-${id}`}>
      <div className="pg-score-ring">
        <BigRing percent={hero.percent} label={hero.percentLabel} id={id} />
        <div className="pg-score-name">Privacy Readiness Score</div>
      </div>
      <div className="pg-score-text">
        <span className={`pg-chip pg-${hero.tone}`}><Icon name="check" size={11} /> {view.coveragePct !== null && view.coveragePct < 50 && hero.percent !== null ? `${hero.band} · limited coverage` : hero.band}</span>
        <p>{hero.headline} {hero.percent !== null && "Read the score with the coverage."}</p>
        <div className="pg-cov">
          <div className="pg-cov-head"><b>Coverage</b> <span>{view.coverageLabel}</span></div>
          <div className="pg-bar"><i style={{ width: `${view.coveragePct ?? 0}%` }} /></div>
          {view.testedLine && <div className="pg-cov-note">{view.testedLine.replace("tested from outside.", "tested from outside.")}</div>}
        </div>
      </div>
    </div>
  );
}

export function CtaCard({ view }: { view: PageReportView }) {
  const { cta } = view;
  return (
    <div className="pg-ctacard" data-testid="pg-cta" data-cta={cta.key}>
      <div className="pg-cta-head"><span className="pg-cta-icon"><Icon name="target" size={20} /></span><b>{cta.heading}</b></div>
      <p>{cta.body}</p>
      <a className="pg-btn" href={cta.href}>{cta.label} <Icon name="arrow" size={12} /></a>
      <div className="pg-cta-note">{cta.note}</div>
    </div>
  );
}

const TILE_ICONS: IconName[] = ["doc", "cycle", "nodes", "bars", "cookie", "globe"];

export function SummaryTiles({ view }: { view: PageReportView }) {
  return (
    <div className="pg-tiles">
      {view.tiles.map((t, i) => (
        <div className="pg-tile" key={t.label}>
          <Icon name={TILE_ICONS[i] ?? "doc"} size={20} className="pg-tile-icon" />
          <b>{t.value}</b>
          <span>{t.label}</span>
        </div>
      ))}
    </div>
  );
}

export function FrameworkBlock({ view, withNote }: { view: PageReportView; withNote?: ReactNode }) {
  const f = view.framework;
  const colour: Record<string, string> = { met: "#21c07a", partly: "#f5c431", notMet: "#e5484d", unverified: "#d6dcee", na: "#b5bbd6" };
  const legendText: Record<string, string> = { met: "met", partly: "partly met", notMet: "not met", unverified: "could not be verified", na: "not applicable" };
  return (
    <div className="pg-framework" data-testid="pg-framework">
      <div className="pg-fw-left"><Flag /><div><b>{f.name}</b><div className="pg-fw-score" data-testid="pg-framework-score">{f.scoreLabel}</div></div></div>
      <div className="pg-fw-right">
        <div className="pg-segbar" role="img" aria-label={f.segments.map((s) => `${s.count} ${legendText[s.key]}`).join(", ")}>
          {f.segments.filter((s) => s.count > 0).map((s) => <i key={s.key} style={{ width: `${s.pct}%`, background: colour[s.key] }} />)}
        </div>
        <ul className="pg-legend">
          {f.segments.map((s) => <li key={s.key}><i style={{ background: colour[s.key] }} />{s.count} {legendText[s.key]}</li>)}
        </ul>
      </div>
      {withNote}
    </div>
  );
}

export function AreasBlock({ view }: { view: PageReportView }) {
  if (view.areas.length === 0) return <p className="pg-muted">The score was not loaded, so no privacy area is shown.</p>;
  const icons: IconName[] = ["shield", "doc", "form", "globe"];
  return (
    <div className="pg-areas">
      {view.areas.map((a, i) => (
        <div className="pg-area" key={a.name}>
          <div className="pg-area-head"><span className="pg-area-icon"><Icon name={icons[i] ?? "doc"} size={20} /></span><div><b>{a.name}</b><div className="pg-area-score">{a.scoreLabel}</div></div></div>
          {a.pct !== null && <div className="pg-bar pg-bar-lg"><i style={{ width: `${Math.max(2, Math.min(100, a.pct))}%` }} /></div>}
          <div className="pg-cov-note">{a.coverageLabel}</div>
        </div>
      ))}
    </div>
  );
}

const cell = (s: string) => (/^\d+$/.test(s) ? <b className="pg-count">{s}</b> : s === "Clicked" ? <Mark tone="good">Clicked</Mark> : s === "Banner found" ? <span className="pg-eye"><Icon name="eye" size={14} /> {s}</span> : <span className="pg-muted-cell">{s}</span>);

export function ConsentTable({ view }: { view: PageReportView }) {
  const c = view.consent;
  if (!c) return <p className="pg-muted">The scan evidence was not loaded, so the consent comparison is not shown.</p>;
  const num = (n: number, tested: boolean) => (tested ? String(n) : "Not tested");
  const rows: [string, string[]][] = [
    ["Cookies set", [num(c.cookies[0], true), num(c.cookies[1], c.acceptTested), num(c.cookies[2], c.rejectTested)]],
    ["Analytics and marketing requests", [num(c.known[0], true), num(c.known[1], c.acceptTested), num(c.known[2], c.rejectTested)]],
    ["All third-party requests", [num(c.all[0], true), num(c.all[1], c.acceptTested), num(c.all[2], c.rejectTested)]],
    ["Banner action", [c.banner[0], c.banner[1], c.banner[2]]],
  ];
  return (
    <table className="pg-table pg-consent" data-testid="pg-consent">
      <thead><tr><th>Metric</th><th>Before Consent</th><th>After Accept</th><th>After Reject</th></tr></thead>
      <tbody>{rows.map(([m, vals]) => <tr key={m}><td>{m}</td>{vals.map((x, i) => <td key={i}>{cell(x)}</td>)}</tr>)}</tbody>
    </table>
  );
}

const TYPE_COLOUR: Record<string, string> = { Marketing: "#2457d6", Analytics: "#7a4dff", Functional: "#0d9aa8", Unclassified: "#8a8eb5" };
const initial = (name: string) => name.replace(/^(www\.)/, "").charAt(0).toUpperCase();

export function TechCards({ view }: { view: PageReportView }) {
  if (view.techCards.length === 0) return <p className="pg-muted">No third-party technologies were recorded on the pages scanned.</p>;
  return (
    <div className="pg-techcards">
      {view.techCards.map((t) => (
        <div className="pg-techcard" key={t.name}>
          <span className="pg-techlogo" style={{ background: TYPE_COLOUR[t.type] ?? "#8a8eb5" }}>{initial(t.name)}</span>
          <b>{t.name}</b>
          <span className="pg-typepill" style={{ color: TYPE_COLOUR[t.type] ?? "#555a85" }}>{t.type}</span>
          <span className="pg-muted">{t.requests} {t.requests === 1 ? "request" : "requests"}</span>
        </div>
      ))}
    </div>
  );
}

const ELEMENT_ICON: Record<string, IconName> = {
  "Privacy policy": "doc", "Terms of use": "doc", "Cookie policy": "cookie", "Consent banner": "shield", "Accept option": "check", "Reject option": "cross", Forms: "form", "Per-purpose choices": "sliders",
};
const ELEMENT_PILL: Record<ElementStatus, { label: string; tone: "good" | "warn" | "mute" | "bad" }> = {
  found: { label: "Found", tone: "good" }, not_found: { label: "Not found", tone: "bad" }, unverified: { label: "Could not verify", tone: "mute" }, not_assessed: { label: "Not assessed", tone: "mute" },
};

export function ElementsGrid({ view }: { view: PageReportView }) {
  if (view.elements.length === 0) return <p className="pg-muted">The scan evidence was not loaded, so the privacy elements are not shown.</p>;
  return (
    <div className="pg-elements">
      {view.elements.slice(0, 8).map((e) => {
        const p = ELEMENT_PILL[e.status];
        return (
          <div className="pg-element" key={e.title}>
            <Icon name={ELEMENT_ICON[e.title] ?? "doc"} size={22} className="pg-el-icon" />
            <div>
              <b>{e.title}</b>
              <Mark tone={p.tone}>{p.label}</Mark>
              <span className="pg-el-note">{e.status === "not_found" ? "in scanned scope" : ""}{e.status === "not_found" && e.note && !/scanned scope/.test(e.note) ? ` · ${e.note}` : ""}{e.status !== "not_found" ? e.note : ""}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function FindingsList({ view }: { view: PageReportView }) {
  if (view.findings.length === 0) return <p className="pg-muted">No gaps were found in the checks we could test.</p>;
  return (
    <div className="pg-findings" data-testid="pg-findings">
      {view.findings.map((f, i) => (
        <div className="pg-finding" key={i}>
          <span className={`pg-num pg-n${i}`}>{i + 1}</span>
          <div><b>{f.title}</b>{f.detail && <div className="pg-muted">{f.detail}</div>}</div>
          <Pill s={findingPill(f)} />
        </div>
      ))}
    </div>
  );
}

export function UpgradeBand({ view, full }: { view: PageReportView; full: boolean }) {
  return (
    <div className={`pg-band${full ? " pg-band-dark" : ""}`} data-testid="pg-band">
      <div className="pg-band-main">
        <b>{full ? "Turn insights into continuous compliance with Consiva AI" : "Get a Full Scan with Consiva AI"}</b>
        <p>{full ? "Set up monitoring, assign fixes and verify them as the site changes." : "Scan all your pages, get detailed technical findings, legal mapping to DPDP Act, 2023 & Rules, and expert recommendations."}</p>
        <div className="pg-band-btns">
          <a className="pg-btn pg-btn-on" href={view.cta.href}>{full ? view.cta.label : "Upgrade to Full Scan"} <Icon name="arrow" size={12} /></a>
          <a className="pg-btn pg-btn-ghost" href={view.cta.href}>Talk to an Expert</a>
        </div>
      </div>
      {!full && (
        <ul className="pg-band-list">
          {["Scan all website pages", "Detailed DPDP Act & Rules mapping", "Full list of cookies, trackers & third parties", "Actionable fixes and monitoring"].map((t) => <li key={t}><Mark tone="good">{t}</Mark></li>)}
        </ul>
      )}
    </div>
  );
}
