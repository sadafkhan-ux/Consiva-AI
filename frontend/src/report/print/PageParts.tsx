import type { ReactNode } from "react";
import { CONSIVA_LOGO_PNG } from "../consivaLogo";
import type { ReportVariant, StatusPillView } from "../pageReports";

// The parts the two fixed-page reports share: the page frame with its header and footer, the score ring, section headings, pills and the small icon set.
// Icons are drawn here, in the report's own colours; they carry no meaning the words beside them do not also say.

export type IconName =
  | "doc" | "cycle" | "nodes" | "bars" | "cookie" | "globe" | "shield" | "check" | "cross" | "eye" | "form" | "sliders" | "calendar" | "page" | "home" | "target"
  | "arrow" | "info" | "clock" | "scale" | "bell" | "book" | "upload" | "key" | "board" | "gavel" | "pencil" | "chart" | "bolt";

const PATHS: Record<IconName, ReactNode> = {
  doc: <><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v4h4M9 12h6M9 16h6" /></>,
  cycle: <><path d="M20 12a8 8 0 1 1-2.5-5.8" /><path d="M20 4v5h-5" /></>,
  nodes: <><circle cx="12" cy="6" r="2.2" /><circle cx="5.5" cy="18" r="2.2" /><circle cx="18.5" cy="18" r="2.2" /><path d="M12 8.2 6.4 16M12 8.2l5.6 7.8M7.7 18h8.6" /></>,
  bars: <><path d="M5 20V12M12 20V5M19 20v-9" /></>,
  cookie: <><path d="M20 12a8 8 0 1 1-8-8 3 3 0 0 0 4 3 3 3 0 0 0 4 5z" /><circle cx="9" cy="10" r="1" /><circle cx="14" cy="15" r="1" /><circle cx="9" cy="15" r="0.8" /></>,
  globe: <><circle cx="12" cy="12" r="8.5" /><path d="M3.5 12h17M12 3.5c3 3 3 14 0 17M12 3.5c-3 3-3 14 0 17" /></>,
  shield: <><path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6z" /><path d="m9 12 2.2 2.2L15.5 10" /></>,
  check: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  cross: <path d="M6 6l12 12M18 6 6 18" />,
  eye: <><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" /><circle cx="12" cy="12" r="2.8" /></>,
  form: <><rect x="5" y="4" width="14" height="16" rx="2" /><path d="M8.5 9h7M8.5 13h7M8.5 17h4" /></>,
  sliders: <><path d="M4 8h9M17 8h3M4 16h3M11 16h9" /><circle cx="15" cy="8" r="2" /><circle cx="9" cy="16" r="2" /></>,
  calendar: <><rect x="4" y="5.5" width="16" height="14" rx="2" /><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4" /></>,
  page: <><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v4h4" /></>,
  home: <><path d="M4 11 12 4l8 7" /><path d="M6 10v10h12V10" /></>,
  target: <><circle cx="12" cy="12" r="8.5" /><circle cx="12" cy="12" r="4.5" /><circle cx="12" cy="12" r="1.2" /></>,
  arrow: <path d="M5 12h14M13 6l6 6-6 6" />,
  info: <><circle cx="12" cy="12" r="8.5" /><path d="M12 11v5.5M12 7.8v.4" /></>,
  clock: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7v5l3 2" /></>,
  scale: <><path d="M12 4v16M6 20h12M5 8h14" /><path d="m5 8-2.5 6a3 3 0 0 0 5 0zM19 8l-2.5 6a3 3 0 0 0 5 0z" /></>,
  bell: <><path d="M6 17V11a6 6 0 0 1 12 0v6l1.5 2h-15z" /><path d="M10 21h4" /></>,
  book: <><path d="M4 5.5C7 4 10 4.5 12 6c2-1.5 5-2 8-.5V19c-3-1.5-6-1-8 .5-2-1.5-5-2-8-.5z" /><path d="M12 6v13.5" /></>,
  upload: <><path d="M12 16V5M7 9l5-5 5 5" /><path d="M5 19h14" /></>,
  key: <><circle cx="8" cy="12" r="3.5" /><path d="M11.5 12H21M17 12v3M20 12v2" /></>,
  board: <><rect x="4" y="5" width="16" height="11" rx="1.5" /><path d="M9 20h6M12 16v4" /></>,
  gavel: <><path d="m13 5 6 6M10 8l6 6M5 19l7-7" /><path d="M8 4l4 4M16 12l4 4" /></>,
  pencil: <><path d="m5 19 1-4L16 5l3 3L9 18z" /><path d="m14 7 3 3" /></>,
  chart: <><path d="M4 19h16" /><path d="m5 15 4-5 3 3 5-7" /></>,
  bolt: <path d="M13 3 5 13.5h6L10 21l9-11h-6z" />,
};

export function Icon({ name, size = 18, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {PATHS[name]}
    </svg>
  );
}

export function Pill({ s, className }: { s: StatusPillView; className?: string }) {
  return <span className={`pg-pill pg-${s.tone}${className ? ` ${className}` : ""}`}>{s.label}</span>;
}

/** A status mark with its words: colour is never the only signal. */
export function Mark({ tone, children }: { tone: "good" | "bad" | "mute" | "warn"; children: ReactNode }) {
  const icon: IconName = tone === "good" ? "check" : tone === "bad" ? "cross" : "info";
  return <span className={`pg-mark pg-${tone}`}><i><Icon name={icon} size={9} /></i>{children}</span>;
}

/** The score ring: the number inside, "/100" under it, one arc from the single score value. */
export function BigRing({ percent, label, id }: { percent: number | null; label: string; id: string }) {
  const R = 54;
  const C = 2 * Math.PI * R;
  const filled = percent === null ? 0 : (Math.min(100, Math.max(0, percent)) / 100) * C;
  const size = percent === null ? 14 : label.length >= 5 ? 30 : 36;
  return (
    <svg className="pg-ring" viewBox="0 0 140 140" role="img" aria-label={percent === null ? "No score" : `Privacy Readiness Score ${label} out of 100`} data-testid={`pg-ring-${id}`}>
      <defs>
        <linearGradient id={`rg-${id}`} x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#2457d6" /><stop offset="0.55" stopColor="#3d7bff" /><stop offset="1" stopColor="#12d6c4" /></linearGradient>
      </defs>
      <circle cx="70" cy="70" r={R} fill="none" stroke="#e6eaf8" strokeWidth="13" />
      {filled > 0 && <circle cx="70" cy="70" r={R} fill="none" stroke={`url(#rg-${id})`} strokeWidth="13" strokeLinecap="round" strokeDasharray={`${filled} ${C}`} transform="rotate(-90 70 70)" />}
      <text x="70" y={percent === null ? 74 : 72} textAnchor="middle" fontSize={size} fontWeight="800" fill="#111b4d" data-testid={`pg-percent-${id}`}>{percent === null ? "No score" : label.replace("%", "")}</text>
      {percent !== null && <text x="70" y="94" textAnchor="middle" fontSize="15" fill="#5d6086">/100</text>}
    </svg>
  );
}

export function Flag() {
  return (
    <svg className="pg-flag" viewBox="0 0 48 48" role="img" aria-label="India">
      <defs><clipPath id="flagclip"><circle cx="24" cy="24" r="22" /></clipPath></defs>
      <g clipPath="url(#flagclip)"><rect width="48" height="16" y="2" fill="#ff9933" /><rect width="48" height="16" y="16" fill="#fff" /><rect width="48" height="16" y="31" fill="#138808" /></g>
      <circle cx="24" cy="24" r="22" fill="none" stroke="#e3e6f4" />
      <circle cx="24" cy="24" r="4.6" fill="none" stroke="#1d3a8a" strokeWidth="1.2" /><circle cx="24" cy="24" r="1" fill="#1d3a8a" />
    </svg>
  );
}

/** The heading of a numbered section: a purple badge, the title, and an optional note or link-like trailer. */
export function SecHead({ n, title, note, aside }: { n: number | string; title: string; note?: string; aside?: ReactNode }) {
  return (
    <div className="pg-sechead">
      <span className="pg-badge">{n}</span>
      <h2>{title}</h2>
      {note && <span className="pg-sec-note">{note}</span>}
      {aside && <span className="pg-sec-aside">{aside}</span>}
    </div>
  );
}

/** Printing a set of fixed sheets uses the sheet's own size with no margin. This is added by the sheets only: a rule in the shared stylesheet would also remove the
 *  margins the print engine gives the flowing pages of the long report, which sit under its page header. */
export function SheetPageStyle() {
  return <style data-sheet-page="">{"@page { size: A4; margin: 0; }"}</style>;
}

export const SHORT_DISCLAIMER = "This report is for informational purposes only and does not constitute legal advice.";

/** One fixed A4 page: header at the top, footer at the bottom, and the page's own content between. */
export function PageFrame({ variant, n, of, domain, scannedAt, reportId, tag = "FREE SCAN REPORT", numbered = true, children }: { variant: ReportVariant; n: number; of: number; domain: string; scannedAt: string; reportId: string; tag?: string; numbered?: boolean; children: ReactNode }) {
  const free = variant === "free";
  return (
    <section className={`pg pg-${variant}`} data-testid={`pg-${variant}-${n}`} data-page={n}>
      <header className="pg-head">
        <span className="pg-logo"><img src={CONSIVA_LOGO_PNG} alt="Consiva.ai" /></span>
        <div className="pg-head-right">
          {free ? (
            <>
              <span className="pg-tag">{tag}</span>
              <div>Scanned on {scannedAt}</div>
              <div>Report ID: {reportId}</div>
            </>
          ) : (
            <>
              <b>Privacy Compliance Report (Full Scan)</b>
              <div>{domain}</div>
              <div>Scanned {scannedAt}</div>
              {n > 1 && <div className="pg-id">Report ID: {reportId}</div>}
            </>
          )}
        </div>
      </header>
      <div className="pg-body">{children}</div>
      <footer className="pg-foot">
        <span className="pg-foot-brand"><i /> Consiva.ai</span>
        <span className="pg-foot-mid">{free ? `Website Privacy Readiness Report | ${domain}` : SHORT_DISCLAIMER}</span>
        <span className="pg-foot-page">{numbered ? `Page ${n} of ${of}` : ""}</span>
      </footer>
    </section>
  );
}
