import type { ReportModel } from "./buildReport";
import type { ElementStatus } from "./reportVisual";
import { pagesCoverageLine } from "./scanPages";
import { CONSIVA_LOGO_HEIGHT, CONSIVA_LOGO_PNG, CONSIVA_LOGO_WIDTH } from "./consivaLogo";
import { buildHeroView, buildPreviewView } from "./hero";
import { formatRef, formatRefShort, formatRefs } from "./legalRefs";
import { buildPolicyView, POLICY_BASIS_LABEL, POLICY_STATUS_LABEL } from "./policyAnalysis";
import { isReportCategory, OMITTED_DUTIES_NOTE, REPORT_DISCLAIMER } from "./reportScope";
import { REQUEST_UNIT_NOTE } from "./stateCounts";

// The report reads in order. Numbered sections 1 to 10: the score, scan summary, readiness, privacy areas and top findings (page 1); the consent
// comparison, the technologies seen, the privacy elements and the table of every check behind the score (page 2, flowing onto the next page
// when the table needs it); the pages the scan visited and a review of the scan, ending with the call to action. Then the Evidence appendix,
// lettered A onward, which holds the complete record: the sector reading, what the policies say (with exact quotations), the consent banner,
// every form with every field, every cookie, every third-party request, the policies and services, and every compliance finding. Nothing is
// cut: a block grows to fit its text and flows onto the next page. A contents on page 1 and an index at the start of the appendix give the
// page of each section. Text and shapes are drawn with jsPDF's own API (crisp and selectable). Every figure is read from the scan; a value the
// scan does not have is shown as "Not assessed", "Not tested" or "Could not verify", never filled in. Legal references are spelt one way
// (report/legalRefs.ts).
const PAGE_W = 595.28;
const PAGE_H = 841.89;
const M = 36;
const CW = PAGE_W - M * 2;
const FOOTER_Y = PAGE_H - 46;
/** The report is three pages, so the pages table lists at most this many. A scan is limited to 5; an older one can hold more. */
const MAX_LISTED_PAGES = 8;

/** Where the call-to-action buttons point. Placeholders until the product team supplies the real destinations. */
export const CTA_URLS = {
  improve: "https://consiva.ai",
  plan: "https://consiva.ai",
  platform: "https://consiva.ai",
};

/** The header strip: the logo badge's own colour, so the logo sits in it without a visible edge. It runs the full width of the page,
 *  below the brand's gradient line, and the logo and every header word are drawn on it in white. */
const HEADER_BG: Rgb = [3, 7, 15];
const HEADER_TOP = 5;
const HEADER_H = 52;
/** The logo is drawn at this height; its width follows the artwork's own proportions. */
const LOGO_H = 34;
const LOGO_W = (LOGO_H * CONSIVA_LOGO_WIDTH) / CONSIVA_LOGO_HEIGHT;

type Rgb = [number, number, number];
const INK: Rgb = [22, 22, 58];
const MUTED: Rgb = [93, 96, 134];
const RULE: Rgb = [227, 230, 244];
const TINT: Rgb = [244, 246, 255];
const WHITE: Rgb = [255, 255, 255];
const BLUE: Rgb = [47, 107, 255];
const PURPLE: Rgb = [122, 77, 255];
const CYAN: Rgb = [18, 194, 233];
const DEEP: Rgb = [75, 59, 214];

type Tone = "good" | "warn" | "mute" | "bad";
const TONES: Record<Tone, { bg: Rgb; fg: Rgb }> = {
  good: { bg: [227, 246, 238], fg: [13, 122, 85] },
  warn: { bg: [255, 241, 214], fg: [143, 86, 0] },
  mute: { bg: [236, 238, 248], fg: [85, 90, 133] },
  bad: { bg: [253, 228, 231], fg: [168, 40, 58] },
};

const ELEMENT_PILL: Record<ElementStatus, { label: string; tone: Tone }> = {
  found: { label: "Found", tone: "good" },
  not_found: { label: "Not found in scanned scope", tone: "warn" },
  unverified: { label: "Could not verify", tone: "mute" },
  not_assessed: { label: "Not assessed", tone: "mute" },
};

// jsPDF (~400kB) stays in its own chunk so it never delays the first render, but the fetch starts as soon as this module loads
// instead of on the first click. Waiting for the click broke the download after every redeploy: a tab opened before it still ran
// the old bundle, which asked for a chunk hash the new build no longer has, and nginx answered 404. If this early fetch fails
// (offline, blocked), the click retries it once.
let jsPdfModule = import("jspdf");
jsPdfModule.catch(() => {
  // Handled at click time; this only stops an "unhandled rejection" in the console.
});

async function loadJsPdf() {
  try {
    return await jsPdfModule;
  } catch {
    jsPdfModule = import("jspdf");
    return jsPdfModule;
  }
}

// The score bands live with the hero's words (report/hero.ts), which the dashboard reads too.
export { scoreBand } from "./hero";

const mix = (a: Rgb, b: Rgb, t: number): Rgb => [0, 1, 2].map((i) => Math.round(a[i] + (b[i] - a[i]) * t)) as Rgb;
/** The brand gradient: blue, then purple, then cyan, at position t from 0 to 1. */
export function gradientAt(t: number): Rgb {
  const c = Math.min(1, Math.max(0, t));
  return c < 0.55 ? mix(BLUE, PURPLE, c / 0.55) : mix(PURPLE, CYAN, (c - 0.55) / 0.45);
}

// Review items that are purely about legal timing or which legal duties apply stay in the dashboard, not in this report.
const LEGAL_ONLY_REVIEW = new Set(["not-yet-in-force", "entity-applicability"]);
const stripRuleCodes = (t: string) => t.replace(/\s*\(rule R-\d+\)/g, "");
const stripId = (t: string) => t.replace(/^DPDP-\w+\s+/, "");

interface FindingRow { title: string; detail: string; label: string; tone: Tone }

export async function downloadReport(model: ReportModel): Promise<void> {
  const { jsPDF } = await loadJsPdf();
  const doc = new jsPDF({ unit: "pt", format: "a4", compress: true });
  const v = model.dpdpScore;
  const vis = model.visual;

  const fill = (c: Rgb) => doc.setFillColor(c[0], c[1], c[2]);
  const stroke = (c: Rgb) => doc.setDrawColor(c[0], c[1], c[2]);
  const ink = (c: Rgb) => doc.setTextColor(c[0], c[1], c[2]);

  /** Wrapped text capped at `maxLines` (the last line is cut with "…"). Returns the y after the last line drawn. */
  const text = (
    s: string, x: number, y: number,
    o: { size?: number; bold?: boolean; color?: Rgb; width?: number; lines?: number; lead?: number; align?: "left" | "center" | "right" } = {},
  ): number => {
    const size = o.size ?? 9;
    const lead = o.lead ?? size * 1.3;
    doc.setFont("helvetica", o.bold ? "bold" : "normal");
    doc.setFontSize(size);
    ink(o.color ?? INK);
    let lines = o.width ? (doc.splitTextToSize(s, o.width) as string[]) : [s];
    if (o.lines && lines.length > o.lines) {
      lines = lines.slice(0, o.lines);
      lines[lines.length - 1] = `${lines[lines.length - 1].replace(/[\s,.;:]+$/, "")}…`;
    }
    for (const line of lines) {
      doc.text(line, x, y, { align: o.align ?? "left" });
      y += lead;
    }
    return y;
  };

  /** Wraps by words, and breaks a word (a URL, a long name) by characters when it alone is wider than the column. Nothing is dropped or cut. */
  const wrapCell = (s: string, width: number, size: number, bold = false): string[] => {
    doc.setFont("helvetica", bold ? "bold" : "normal");
    doc.setFontSize(size);
    const out: string[] = [];
    let line = "";
    const push = () => { if (line) out.push(line); line = ""; };
    for (const word of String(s).split(/\s+/).filter(Boolean)) {
      if (doc.getTextWidth(word) > width) {
        push();
        for (const ch of word) {
          if (line && doc.getTextWidth(line + ch) > width) push();
          line += ch;
        }
        continue;
      }
      const next = line ? `${line} ${word}` : word;
      if (doc.getTextWidth(next) > width) { push(); line = word; } else line = next;
    }
    push();
    return out.length ? out : [""];
  };

  const box = (x: number, y: number, w: number, h: number, o: { bg?: Rgb; border?: Rgb; r?: number } = {}) => {
    if (o.bg) fill(o.bg);
    if (o.border) { stroke(o.border); doc.setLineWidth(0.7); }
    doc.roundedRect(x, y, w, h, o.r ?? 7, o.r ?? 7, o.bg && o.border ? "FD" : o.bg ? "F" : "S");
  };

  /** A coloured label. Returns its width. */
  const pill = (label: string, x: number, y: number, tone: Tone, size = 7): number => {
    doc.setFont("helvetica", "bold");
    doc.setFontSize(size);
    const w = doc.getTextWidth(label) + 12;
    fill(TONES[tone].bg);
    doc.roundedRect(x, y, w, size + 7, (size + 7) / 2, (size + 7) / 2, "F");
    ink(TONES[tone].fg);
    doc.text(label, x + 6, y + size + 1.6);
    return w;
  };

  /** Pill sized to fit inside `maxW`: falls back to a shorter label when the long one would overflow. */
  const fitPill = (label: string, short: string, x: number, y: number, tone: Tone, maxW: number, size = 7) => {
    doc.setFont("helvetica", "bold");
    doc.setFontSize(size);
    pill(doc.getTextWidth(label) + 12 <= maxW ? label : short, x, y, tone, size);
  };

  const gradientBar = (x: number, y: number, w: number, h: number) => {
    const steps = 80;
    for (let i = 0; i < steps; i++) {
      fill(gradientAt(i / (steps - 1)));
      doc.rect(x + (w * i) / steps, y, w / steps + 0.6, h, "F");
    }
  };

  /** A ring filled to `percent`, in the brand gradient. */
  const ring = (cx: number, cy: number, r: number, percent: number | null, width: number) => {
    stroke([229, 232, 246]);
    doc.setLineWidth(width);
    doc.circle(cx, cy, r, "S");
    if (percent === null || percent <= 0) return;
    const sweep = (Math.min(100, percent) / 100) * Math.PI * 2;
    const steps = Math.max(2, Math.ceil(sweep * 28));
    doc.setLineCap("round");
    doc.setLineWidth(width);
    for (let i = 0; i < steps; i++) {
      const a0 = -Math.PI / 2 + (sweep * i) / steps;
      const a1 = -Math.PI / 2 + (sweep * (i + 1)) / steps + 0.02;
      stroke(gradientAt(i / (steps - 1)));
      doc.line(cx + r * Math.cos(a0), cy + r * Math.sin(a0), cx + r * Math.cos(a1), cy + r * Math.sin(a1));
    }
    doc.setLineCap("butt");
  };

  /** Every section heading in the report, in order, with the page it landed on: the contents on page 1 is built from this. */
  const outline: { label: string; page: number }[] = [];
  let sectionNo = 0;
  /** A numbered section heading: the number in a purple badge, the title in small capitals, and a rule. Returns the y below it. */
  const section = (label: string, y: number): number => {
    sectionNo += 1;
    outline.push({ label: `${sectionNo}  ${label}`, page: doc.getNumberOfPages() });
    fill(PURPLE);
    doc.circle(M + 6.5, y - 3.2, 6.5, "F");
    text(String(sectionNo), M + 6.5, y - 0.7, { size: 7, bold: true, color: WHITE, align: "center" });
    doc.setFont("helvetica", "bold");
    doc.setFontSize(7.5);
    ink(MUTED);
    doc.text(label.toUpperCase(), M + 19, y, { charSpace: 0.8 });
    const w = doc.getTextWidth(label.toUpperCase()) + label.length * 0.8;
    stroke(RULE);
    doc.setLineWidth(0.6);
    doc.line(M + 19 + w + 8, y - 2.5, M + CW, y - 2.5);
    return y + 9;
  };

  const logo = (x: number, y: number) => {
    doc.addImage(CONSIVA_LOGO_PNG, "PNG", x, y, LOGO_W, LOGO_H);
  };

  const pageTop = (page: number) => {
    gradientBar(0, 0, PAGE_W, HEADER_TOP);
    fill(HEADER_BG);
    doc.rect(0, HEADER_TOP, PAGE_W, HEADER_H, "F");
    // The logo is vertically centred in the strip, with the same space above and below it.
    logo(M, HEADER_TOP + (HEADER_H - LOGO_H) / 2);
    text(model.siteUrl.replace(/^https?:\/\//, "").replace(/\/$/, ""), PAGE_W - M, 28, { size: 9, bold: true, color: WHITE, align: "right" });
    const sub = page === 1
      ? `Scanned ${v ? v.scanTime : model.generatedAt}${model.scanPages ? ` · ${model.scanPages.coverageLine}` : vis ? ` · ${pagesCoverageLine(vis.pagesScanned, vis.pagesFound)}` : ""}`
      : `Scanned ${v ? v.scanTime : model.generatedAt}`;
    text(sub, PAGE_W - M, 40, { size: 7.5, color: WHITE, align: "right" });
  };

  const footer = (_page: number) => {
    stroke(RULE);
    doc.setLineWidth(0.6);
    doc.line(M, FOOTER_Y, M + CW, FOOTER_Y);
    const note = REPORT_DISCLAIMER;   // the same few lines on every page of every report
    text(note, M, FOOTER_Y + 10, { size: 6.5, color: MUTED, width: CW - 44, lead: 8.2 });
  };

  const button = (label: string, x: number, y: number, w: number, h: number, url: string, style: "solid" | "outline" | "light") => {
    if (style === "solid") {
      fill(PURPLE);
      doc.roundedRect(x, y, w, h, h / 2, h / 2, "F");
    } else if (style === "light") {
      fill(WHITE);
      doc.roundedRect(x, y, w, h, h / 2, h / 2, "F");
    } else {
      stroke(WHITE);
      doc.setLineWidth(1.1);
      doc.roundedRect(x, y, w, h, h / 2, h / 2, "S");
    }
    text(label, x + w / 2, y + h / 2 + 3.2, { size: 8.5, bold: true, color: style === "light" ? DEEP : WHITE, align: "center" });
    doc.link(x, y, w, h, { url });
  };

  const pillWidth = (label: string, size = 7) => {
    doc.setFont("helvetica", "bold");
    doc.setFontSize(size);
    return doc.getTextWidth(label) + 12;
  };

  /**
   * A top-to-bottom writer for blocks of text: a paragraph, an item with labels, a quotation, a table. It keeps its own position (`w.y`), starts a
   * new page (header and footer included) when the next block will not fit, and cuts nothing: a block grows to fit its text. The numbered sections
   * after the table of checks and the appendix both write with it.
   */
  const ORDER_COUNTS = ["Compliant", "Gap", "Not Found", "Cannot Verify", "Requires document review"] as const;
  const makeWriter = (startY: number) => {
    const w = { y: startY };
      const room = (h: number, redraw?: () => void) => {
        if (w.y + h <= FOOTER_Y - 14) return;
        footer(doc.getNumberOfPages());
        doc.addPage("a4", "portrait");
        pageTop(doc.getNumberOfPages());
        w.y = 74;
        redraw?.();
      };
      /** A paragraph of wrapped text across the full width. */
      const para = (s: string, o: { size?: number; bold?: boolean; color?: Rgb; indent?: number; lead?: number } = {}) => {
        const size = o.size ?? 8;
        const lead = o.lead ?? size * 1.4;
        const lines = wrapCell(s, CW - (o.indent ?? 0), size, o.bold);
        for (const ln of lines) {
          room(lead);
          text(ln, M + (o.indent ?? 0), w.y + size, { size, bold: o.bold, color: o.color });
          w.y += lead;
        }
      };

      /** An item's title with coloured labels at the right of its first line. */
      const entry = (title: string, pills: { label: string; tone: Tone }[], o: { indent?: number; size?: number } = {}) => {
        const indent = o.indent ?? 0;
        const size = o.size ?? 8.4;
        const lead = size * 1.4;
        const widths = pills.map((p) => pillWidth(p.label, 6.4));
        const pillsW = widths.reduce((a, b) => a + b + 4, 0);
        const lines = wrapCell(title, CW - indent - pillsW - 10, size, true);
        room(lines.length * lead + 8);
        lines.forEach((ln, i) => text(ln, M + indent, w.y + size + i * lead, { size, bold: true }));
        let px = M + CW - pillsW + 4;
        pills.forEach((p, i) => {
          pill(p.label, px, w.y + 0.5, p.tone, 6.4);
          px += widths[i] + 4;
        });
        w.y += lines.length * lead + 3;
      };

      /** A passage quoted from the site, set apart: tinted block, accent bar, the words in full, then where they came from. */
      const quoteBlock = (quote: string, source: string | null, indent = 12) => {
        const size = 7.5;
        const lead = 9.8;
        const lines = wrapCell(`“${quote}”`, CW - indent - 22, size);
        const src = source ? wrapCell(source, CW - indent - 22, 6.6) : [];
        const h = lines.length * lead + src.length * 8.4 + 10;
        room(h + 3);
        fill(TINT);
        doc.roundedRect(M + indent, w.y, CW - indent, h, 3, 3, "F");
        fill(PURPLE);
        doc.rect(M + indent, w.y, 2.6, h, "F");
        lines.forEach((ln, i) => text(ln, M + indent + 10, w.y + 7 + size + i * lead - 5, { size, color: INK }));
        src.forEach((ln, i) => text(ln, M + indent + 10, w.y + 5 + lines.length * lead + 7 + i * 8.4, { size: 6.6, color: DEEP }));
        w.y += h + 4;
      };

      /** A table whose cells wrap in full, with banded rows; the header repeats on each page. */
      const table = (cols: { head: string; w: number; bold?: boolean }[], rows: (string | string[])[][], size = 7.4) => {
        const lead = size * 1.3;
        const head = () => {
          fill(TINT);
          doc.rect(M, w.y, CW, 17, "F");
          let hx = M;
          cols.forEach((c) => { text(c.head.toUpperCase(), hx + 5, w.y + 11.5, { size: 6, bold: true, color: MUTED }); hx += c.w; });
          w.y += 17;
        };
        room(36);
        head();
        rows.forEach((row, ri) => {
          const cells = row.map((c, i) => (Array.isArray(c) ? c : [c]).flatMap((part) => wrapCell(part, cols[i].w - 10, size, cols[i].bold)));
          const h = Math.max(...cells.map((c) => c.length)) * lead + 8;
          room(h, head);
          if (ri % 2 === 1) {
            fill([249, 250, 255]);
            doc.rect(M, w.y, CW, h, "F");
          }
          let x = M;
          cells.forEach((lines, i) => {
            lines.forEach((ln, k) => text(ln, x + 5, w.y + 5 + size + k * lead, { size, bold: cols[i].bold }));
            x += cols[i].w;
          });
          stroke(RULE);
          doc.setLineWidth(0.4);
          doc.line(M, w.y + h, M + CW, w.y + h);
          w.y += h;
        });
        w.y += 4;
      };

    return { w, room, para, entry, quoteBlock, table };
  };


  const assessed = v ? v.assessed : [];
  const scoreValue = v ? v.scoreValue : null;   // the one authoritative number: the ring, the text and every other page use it

  // ============================== page 1 ==============================
  pageTop(1);
  text("Privacy Compliance Report", M, 74, { size: 20, bold: true });
  text("Evidence from a live scan of the main domain: before consent, after Accept and after Reject.", M, 88, { size: 8.5, color: MUTED });

  // --- hero: the score at the top left, what it means beside it, and at the top right the scanned site's domain above its homepage as the scanner saw it ---
  const hero = buildHeroView(v);
  const preview = buildPreviewView(model.screenshot, model.siteUrl, model.scanId);
  const heroY = 100;
  const RING_R = 36;
  const SW = 176;                                  // the picture's width
  const SH_MAX = 100;                              // and the tallest it may be: compact, never the loudest thing on the page
  const shotX = M + CW - 14 - SW;
  const hx = M + 126;
  const hw = shotX - 14 - hx;

  // The right-hand column is measured first, so the hero is exactly as tall as what it holds.
  const domainSizes = [9.5, 9, 8.5, 8, 7.5, 7, 6.5, 6];
  const domainSize = domainSizes.find((s) => wrapCell(preview.domain, SW, s, true).length === 1) ?? 6;
  const domainLines = wrapCell(preview.domain, SW, domainSize, true);
  const aspect = preview.width / preview.height;
  const imgH = preview.src ? Math.min(SH_MAX, SW / aspect) : Math.round(SW * 9 / 16);
  const imgW = preview.src ? Math.min(SW, imgH * aspect) : SW;
  const domainTop = heroY + 12;
  const imgTop = domainTop + domainLines.length * (domainSize + 2.5) + 5;
  const captionLines = wrapCell(preview.caption, SW, 6.8);
  const addressLines = preview.address ? wrapCell(preview.address, SW, 6.6) : [];
  const rightBottom = imgTop + imgH + 4 + captionLines.length * 8.6 + addressLines.length * 8.2;

  // The middle column: the reading of the score, its coverage, and the call to action.
  const headLines = wrapCell(hero.headline, hw, 10, true);
  const headTop = heroY + 44;
  const covLines = hero.coverageLine ? wrapCell(hero.coverageLine, hw, 7.4) : [];
  const covTop = headTop + headLines.length * 12.6 + 6;
  const buttonTop = covTop + covLines.length * 9.2 + 8;
  const midBottom = buttonTop + 22;
  const ringBottom = heroY + 62 + RING_R + 4 + 3 + 16;

  const heroH = Math.round(Math.max(rightBottom, midBottom, ringBottom) - heroY + 14);
  box(M, heroY, CW, heroH, { bg: [243, 245, 255], border: RULE, r: 12 });

  // left: the ring, thick black border, the percentage inside it, the label directly beneath it
  const ringX = M + 62;
  const ringY = heroY + 62;
  ring(ringX, ringY, RING_R, scoreValue, 8);
  stroke(HEADER_BG);
  doc.setLineWidth(3);
  doc.circle(ringX, ringY, RING_R + 4 + 1.5, "S");
  if (hero.scored) {
    // centred in the ring both ways: the baseline sits half a cap height below the centre
    text(hero.percentLabel, ringX, ringY + 6.2, { size: 17, bold: true, align: "center" });
  } else {
    text(hero.percentLabel, ringX, ringY + 4, { size: 10, bold: true, color: MUTED, align: "center" });
  }
  text(hero.scoreLabel, ringX, ringY + RING_R + 4 + 3 + 11, { size: 8.5, bold: true, color: MUTED, align: "center" });

  // middle
  pill(hero.band, hx, heroY + 14, hero.tone, 7.5);
  headLines.forEach((ln, k) => text(ln, hx, headTop + k * 12.6, { size: 10, bold: true }));
  covLines.forEach((ln, k) => text(ln, hx, covTop + k * 9.2, { size: 7.4, color: MUTED }));
  button("Improve My Score with Consiva AI", hx, buttonTop, 176, 22, CTA_URLS.improve, "solid");

  // right: the domain above the homepage picture; the same picture the dashboard shows, or a plain panel that says why there is none
  domainLines.forEach((ln, k) => text(ln, shotX, domainTop + domainSize + k * (domainSize + 2.5), { size: domainSize, bold: true }));
  let drawn = false;
  if (preview.src) {
    try {
      const ix = shotX + (SW - imgW) / 2;
      doc.addImage(preview.src, preview.src.startsWith("data:image/png") ? "PNG" : "JPEG", ix, imgTop, imgW, imgH);
      stroke(RULE);
      doc.setLineWidth(0.8);
      doc.rect(ix, imgTop, imgW, imgH, "S");
      drawn = true;
    } catch {
      drawn = false;   // a picture the PDF cannot place is treated as no picture: the report still builds
    }
  }
  if (!drawn) {
    fill([236, 238, 248]);
    stroke(RULE);
    doc.setLineWidth(0.8);
    doc.roundedRect(shotX, imgTop, SW, imgH, 6, 6, "FD");
    text("Homepage preview not available", shotX + SW / 2, imgTop + imgH / 2 - 8, { size: 8.4, bold: true, color: MUTED, align: "center" });
    wrapCell(preview.unavailable ?? "A homepage preview is not available for this scan.", SW - 24, 6.9).forEach((ln, k) =>
      text(ln, shotX + SW / 2, imgTop + imgH / 2 + 4 + k * 8.4, { size: 6.9, color: MUTED, align: "center" }));
  }
  captionLines.forEach((ln, k) => text(ln, shotX, imgTop + imgH + 11 + k * 8.6, { size: 6.8, color: MUTED }));
  addressLines.forEach((ln, k) => text(ln, shotX, imgTop + imgH + 11 + captionLines.length * 8.6 + k * 8.2, { size: 6.6, color: MUTED }));

  // When the analysis step did not complete the report says so, beside the score, so the score is not read as the result of a finished analysis.
  let afterHero = heroY + heroH + 22;
  if (model.analysisNote) {
    afterHero = text(model.analysisNote, M, heroY + heroH + 12, { size: 7.2, bold: true, color: TONES.warn.fg, width: CW, lead: 9, lines: 2 }) + 8;
  }

  // --- scan summary ---
  let y = section("Scan summary", afterHero);
  const tiles: { value: string; label: string }[] = vis
    ? [
        { value: vis.pagesFound !== null && vis.pagesFound >= vis.pagesScanned ? `${vis.pagesScanned} of ${vis.pagesFound}` : String(vis.pagesScanned),
          label: vis.pageLimit ? `Pages scanned (limit ${vis.pageLimit})` : "Pages scanned" },
        { value: String(vis.statesTested), label: "Consent states tested" },
        { value: String(vis.requests), label: "Third-party requests" },
        { value: String(vis.knownRequests), label: "Analytics and marketing requests" },
        { value: String(vis.cookies), label: "Cookies set" },
        { value: String(vis.domains), label: "Third-party domains" },
      ]
    : model.counts.slice(0, 6).map((c) => ({ value: c.value, label: c.label }));
  const gap = 6;
  const tw = (CW - gap * (tiles.length - 1)) / Math.max(1, tiles.length);
  // Each tile grows to fit its label, so no label is cut.
  const tileLabels = tiles.map((t) => wrapCell(t.label, tw - 14, 6.8));
  const tileH = Math.max(50, 34 + Math.max(...tileLabels.map((l) => l.length)) * 8 + 6);
  tiles.forEach((t, i) => {
    const x = M + i * (tw + gap);
    box(x, y, tw, tileH, { bg: TINT, r: 8 });
    text(t.value, x + 8, y + 22, { size: 17, bold: true });
    tileLabels[i].forEach((ln, k) => text(ln, x + 8, y + 34 + k * 8, { size: 6.8, color: MUTED }));
  });
  y += tileH + 16;

  // --- readiness: the India DPDP Act result. No other framework is assessed, so none is shown. ---
  y = section("Framework readiness", y);
  const fh = 62;
  const counts = (k: string) => assessed.filter((r) => r.statusKey === k).length;
  box(M, y, CW, fh, { bg: v && scoreValue !== null ? WHITE : [247, 248, 252], border: RULE, r: 9 });
  text("India DPDP Act", M + 12, y + 16, { size: 9, bold: true });
  // The figure is as large as the room beside the counts allows: "Insufficient Evidence" is much wider than "56.5%".
  const readinessText = v ? v.headline : "Not available";
  let readinessSize = 20;
  doc.setFont("helvetica", "bold");
  doc.setFontSize(readinessSize);
  while (readinessSize > 11 && doc.getTextWidth(readinessText) > 170) { readinessSize -= 1; doc.setFontSize(readinessSize); }
  text(readinessText, M + 12, y + 44, { size: readinessSize, bold: true });
  text(
    v ? `${counts("met")} met · ${counts("partially_met")} partly met · ${counts("not_met")} not met\n${v.unableToVerify.length} could not be verified · ${v.excluded.length} not applicable` : "The score was not loaded.",
    M + 200, y + 28, { size: 7.4, color: MUTED, width: CW - 215, lead: 10, lines: 2 },
  );
  y += fh + 16;

  // --- privacy areas ---
  if (v) {
    y = section("Privacy areas", y);
    y = text("Each bar is the share of the weighted points earned in that area. Coverage is the share of the area's checks that a scan could test.", M, y + 1, { size: 6.8, color: MUTED, width: CW, lead: 8.2 }) + 3;
    const cats = v.categories.filter((c) => isReportCategory(c.name)).slice(0, 4);
    const aw = (CW - gap * (cats.length - 1)) / Math.max(1, cats.length);
    const ah = 66;
    cats.forEach((c, i) => {
      const x = M + i * (aw + gap);
      const pctValue = c.score === "Not assessed" ? null : parseFloat(c.score);
      box(x, y, aw, ah, { border: RULE, r: 9 });
      text(c.name, x + 9, y + 14, { size: 8, bold: true, width: aw - 14, lines: 1 });
      text(pctValue === null ? "Not assessed" : c.score, x + 9, y + 35, { size: pctValue === null ? 10 : 17, bold: true, color: pctValue === null ? MUTED : INK });
      if (pctValue !== null) {
        fill([233, 235, 247]);
        doc.roundedRect(x + 9, y + 42, aw - 18, 4.5, 2.2, 2.2, "F");
        const bar = Math.max(4, ((aw - 18) * pctValue) / 100);
        for (let k = 0; k < 24; k++) {
          fill(gradientAt(k / 23));
          doc.rect(x + 9 + (bar * k) / 24, y + 42, bar / 24 + 0.4, 4.5, "F");
        }
      }
      text(pctValue === null ? "Cannot be tested from outside" : c.coverage.replace("coverage", "Coverage"), x + 9, y + 58, { size: 6.8, color: MUTED, width: aw - 14, lines: 1 });
    });
    y += ah + 16;
  }

  // --- top findings ---
  y = section("Top findings", y);
  const rows: FindingRow[] = [];
  if (v) {
    const detailOf = (e: { observed: string[]; explanation: string; missing: string[] }, unable: boolean) =>
      stripRuleCodes(unable ? (e.missing[0] ?? e.explanation) : (e.observed[0] ?? e.explanation));
    const group = (k: string) => v.groups.find((g) => g.key === k)?.entries ?? [];
    for (const e of group("confirmed")) rows.push({ title: stripId(e.title), detail: detailOf(e, false), label: e.outcome, tone: "bad" });
    for (const e of group("future_readiness")) rows.push({ title: stripId(e.title), detail: detailOf(e, false), label: e.outcome, tone: "warn" });
    for (const e of group("needs_review").filter((x) => !LEGAL_ONLY_REVIEW.has(x.id))) rows.push({ title: stripId(e.title), detail: detailOf(e, false), label: "Needs review", tone: "warn" });
    const business = new Set(v.unableToVerify.filter((r) => r.checkType === "Business check").map((r) => r.id));
    for (const e of group("unable_to_verify").filter((x) => !business.has(x.id))) rows.push({ title: stripId(e.title), detail: detailOf(e, true), label: "Could not verify", tone: "mute" });
  } else {
    for (const p of model.problems) rows.push({ title: p.text, detail: "", label: p.risk, tone: "warn" });
  }
  const shown = rows.slice(0, 3);
  if (shown.length === 0) {
    text("No gaps were found in the checks we could test.", M, y + 12, { size: 9 });
  }
  let fy = y;
  let drawnCards = 0;
  for (const [i, f] of shown.entries()) {
    const titleL = wrapCell(f.title, CW - 170, 8.8, true);
    const detailL = wrapCell(f.detail, CW - 54, 7.3);   // under the title, so it can use the card's whole width
    // The detail starts below the status label at the card's right, so a long line can use the full width without running under it.
    const detailTop = Math.max(31, 15 + titleL.length * 10.4);
    const h = Math.max(38, detailTop + detailL.length * 8.6 + 5);
    if (fy + h > FOOTER_Y - 14) break;   // not enough room left on the page: this one and the rest are in the Appendix, and the pointer below counts them
    drawnCards += 1;
    box(M, fy, CW, h, { border: RULE, r: 9 });
    fill(f.tone === "bad" ? [209, 59, 74] : f.tone === "warn" ? [199, 122, 6] : [138, 142, 181]);
    doc.circle(M + 17, fy + 19, 8, "F");
    text(String(i + 1), M + 17, fy + 22.2, { size: 9, bold: true, color: WHITE, align: "center" });
    titleL.forEach((ln, k) => text(ln, M + 34, fy + 14 + k * 10.4, { size: 8.8, bold: true }));
    detailL.forEach((ln, k) => text(ln, M + 34, fy + detailTop + k * 8.6, { size: 7.3, color: MUTED }));
    const pw = pillWidth(f.label);
    pill(f.label, M + CW - pw - 10, fy + 12, f.tone, 7);
    fy += h + 6;
  }
  let contentsTop = fy + 6;
  if (rows.length > drawnCards) {
    text(`+ ${rows.length - drawnCards} more in the Appendix (Compliance findings in full).`, M, fy + 6, { size: 7.5, color: MUTED });
    contentsTop = fy + 20;
  }
  footer(1);

  // ============================== page 2 ==============================
  doc.addPage("a4", "portrait");
  pageTop(2);
  y = section("Consent behaviour", 74);
  if (vis) {
    const c = vis.consent;
    const colW = [CW - 3 * 118, 118, 118, 118];
    const heads = ["What was observed", "Before consent", "After Accept", "After Reject"];
    fill(TINT);
    doc.rect(M, y, CW, 20, "F");
    let cx = M;
    heads.forEach((h, i) => {
      text(h.toUpperCase(), i === 0 ? cx + 8 : cx + colW[i] / 2, y + 13, { size: 6.8, bold: true, color: MUTED, align: i === 0 ? "left" : "center" });
      cx += colW[i];
    });
    y += 20;
    const num = (n: number, tested: boolean) => (tested ? String(n) : "Not tested");
    const tableRows: { label: string; cells: [string, string, string]; bold: boolean }[] = [
      { label: "Cookies set", cells: [num(c.cookies[0], true), num(c.cookies[1], c.acceptTested), num(c.cookies[2], c.rejectTested)], bold: true },
      { label: "Analytics and marketing requests", cells: [num(c.known[0], true), num(c.known[1], c.acceptTested), num(c.known[2], c.rejectTested)], bold: true },
      { label: "All third-party requests", cells: [num(c.all[0], true), num(c.all[1], c.acceptTested), num(c.all[2], c.rejectTested)], bold: true },
      { label: "Banner action", cells: c.banner, bold: false },
    ];
    for (const r of tableRows) {
      text(r.label, M + 8, y + 15, { size: 8.5 });
      let x = M + colW[0];
      r.cells.forEach((cell, i) => {
        const isNum = /^\d+$/.test(cell);
        text(cell, x + colW[i + 1] / 2, y + 15, { size: isNum ? 12 : 8, bold: r.bold && isNum, color: isNum || !r.bold ? INK : MUTED, align: "center", width: colW[i + 1] - 8, lines: 1 });
        x += colW[i + 1];
      });
      stroke(RULE);
      doc.setLineWidth(0.5);
      doc.line(M, y + 24, M + CW, y + 24);
      y += 24;
    }
    text(REQUEST_UNIT_NOTE, M, y + 10, { size: 6.6, color: MUTED, width: CW, lines: 2, lead: 8 });
    y += 20;
    // key takeaway
    y += 2;
    box(M, y, CW, 50, { bg: [238, 243, 255], r: 6 });
    fill(PURPLE);
    doc.rect(M, y, 4, 50, "F");
    text("KEY TAKEAWAY", M + 14, y + 13, { size: 7, bold: true, color: DEEP });
    text(c.takeaway, M + 14, y + 25, { size: 8.3, width: CW - 28, lines: 3, lead: 10.4 });
    y += 50 + 18;

    // technologies
    y = section("Tracking technologies detected", y);
    const tcol = [CW - 70 - 90 - 140, 90, 70, 140];
    fill(TINT);
    doc.rect(M, y, CW, 18, "F");
    let tx = M;
    ["Technology", "Type", "Requests", "Seen"].forEach((h, i) => {
      text(h.toUpperCase(), tx + 8, y + 12, { size: 6.8, bold: true, color: MUTED });
      tx += tcol[i];
    });
    y += 18;
    if (vis.technologies.rows.length === 0) {
      text("No third-party technologies were recorded on the pages scanned.", M + 8, y + 14, { size: 8.5 });
      y += 22;
    }
    for (const t of vis.technologies.rows) {
      const cells = [t.name, t.type, String(t.requests), t.seen].map((c, i) => wrapCell(c, tcol[i] - 14, 8.2, i === 0));
      const rowH = Math.max(18, Math.max(...cells.map((c) => c.length)) * 10 + 8);
      let x = M;
      cells.forEach((lines, i) => {
        lines.forEach((ln, k) => text(ln, x + 8, y + 12 + k * 10, { size: 8.2, bold: i === 0 }));
        x += tcol[i];
      });
      stroke(RULE);
      doc.setLineWidth(0.5);
      doc.line(M, y + rowH, M + CW, y + rowH);
      y += rowH;
    }
    if (vis.technologies.more > 0) {
      text(`+ ${vis.technologies.more} more: every third-party request is listed in the Appendix (Third-party requests and scripts).`, M + 8, y + 11, { size: 7.5, color: MUTED });
      y += 14;
    }
    y += 14;

    // elements
    y = section("Privacy elements on the site", y);
    const cols = 4;
    const ew = (CW - gap * (cols - 1)) / cols;
    const noteLines = vis.elements.slice(0, 12).map((e) => wrapCell(e.note, ew - 14, 6.6));
    const eh = Math.max(52, 43 + Math.max(1, ...noteLines.map((l) => l.length)) * 8 + 4);
    vis.elements.slice(0, 12).forEach((e, i) => {
      const x = M + (i % cols) * (ew + gap);
      const ey = y + Math.floor(i / cols) * (eh + gap);
      box(x, ey, ew, eh, { border: RULE, r: 8 });
      text(e.title, x + 8, ey + 13, { size: 7.8, bold: true, width: ew - 14, lines: 1 });
      const p = ELEMENT_PILL[e.status];
      fitPill(p.label, e.status === "not_found" ? "Not found" : p.label, x + 8, ey + 19, p.tone, ew - 14, 6.6);
      noteLines[i].forEach((ln, k) => text(ln, x + 8, ey + 43 + k * 8, { size: 6.6, color: MUTED }));
    });
    y += Math.ceil(Math.min(12, vis.elements.length) / cols) * (eh + gap) + 8;
  } else {
    text("The scan evidence could not be loaded, so the consent comparison, technologies and privacy elements are not shown.", M, y + 14, { size: 9, color: MUTED, width: CW });
    y += 40;
  }

  // --- every check behind the score, compactly. The full record of each check (legal basis, limits, evidence expected) is on the dashboard. ---
  if (v) {
    const checks = v.allChecks.filter((c) => isReportCategory(c.category));
    const listed = checks.filter((c) => c.statusKey !== "not_applicable");
    const notApplicable = checks.filter((c) => c.statusKey === "not_applicable");
    const contributionOf = (r: (typeof checks)[number]) => {
      if (r.statusKey === "unable_to_verify") return "Not scored";
      const lost = /^([\d.]+) of [\d.]+ points lost/.exec(r.penalty);
      return `${r.points}${lost && parseFloat(lost[1]) > 0 ? ` · penalty ${lost[1]}` : ""}`;
    };
    if (y + 80 > FOOTER_Y - 14) {
      footer(doc.getNumberOfPages());
      doc.addPage("a4", "portrait");
      pageTop(doc.getNumberOfPages());
      y = 74;
    }
    y = section("Every check behind the score", y + 8);
    const cols = [30, 196, 118, 34, 68, 77];
    const headRow = () => {
      fill(TINT);
      doc.rect(M, y, CW, 16, "F");
      let hx = M;
      ["ID", "What is checked, and the evidence", "Requirement", "Weight", "Status", "Contribution"].forEach((h, i) => {
        text(h.toUpperCase(), i === 3 ? hx + cols[i] - 6 : hx + 6, y + 11, { size: 5.9, bold: true, color: MUTED, align: i === 3 ? "right" : "left" });
        hx += cols[i];
      });
      y += 16;
    };
    /** Starts a new page when the next block does not fit. The table's header is repeated only where more table rows follow. */
    const checkRoom = (h: number, repeatHeader = true) => {
      if (y + h <= FOOTER_Y - 14) return;
      footer(doc.getNumberOfPages());
      doc.addPage("a4", "portrait");
      pageTop(doc.getNumberOfPages());
      y = 74;
      if (repeatHeader) headRow();
    };
    headRow();
    const tone = (t: string): Tone => (t === "pass" ? "good" : t === "fail" ? "bad" : t === "na" ? "mute" : "warn");
    let band = 0;
    for (const r of listed) {
      // Every line of the check, its evidence, its legal basis and its contribution is printed: the row grows to fit its text.
      const titleL = wrapCell(r.title, cols[1] - 10, 7, true);
      const evL = wrapCell(stripRuleCodes(r.evidenceLine), cols[1] - 10, 6.1);
      const refL = wrapCell(formatRefShort(r.provisions), cols[2] - 10, 6.1);
      const conL = wrapCell(contributionOf(r), cols[5] - 8, 6.4);
      const rowH = Math.max(21, 6 + titleL.length * 8.2 + evL.length * 7.2 + 4, refL.length * 7.2 + 10, conL.length * 7.4 + 10);
      checkRoom(rowH);
      if (r.tone === "fail") {
        fill([253, 236, 238]);
        doc.rect(M, y, CW, rowH, "F");
      } else if (band % 2 === 1) {
        fill([249, 250, 255]);
        doc.rect(M, y, CW, rowH, "F");
      }
      band += 1;
      let x = M;
      text(r.id.replace("DPDP-", ""), x + 6, y + 13, { size: 7, bold: true });
      x += cols[0];
      titleL.forEach((ln, k) => text(ln, x + 6, y + 9 + k * 8.2, { size: 7, bold: true }));
      evL.forEach((ln, k) => text(ln, x + 6, y + 9 + titleL.length * 8.2 + k * 7.2, { size: 6.1, color: MUTED }));
      x += cols[1];
      refL.forEach((ln, k) => text(ln, x + 6, y + 9 + k * 7.2, { size: 6.1, color: MUTED }));
      x += cols[2];
      text(String(r.weight), x + cols[3] - 6, y + 13, { size: 7, align: "right" });
      x += cols[3];
      pill(r.verdict, x + 4, y + 5, tone(r.tone), 6.2);
      x += cols[4];
      conL.forEach((ln, k) => text(ln, x + 6, y + 9 + k * 7.4, { size: 6.4 }));
      stroke(RULE);
      doc.setLineWidth(0.4);
      doc.line(M, y + rowH, M + CW, y + rowH);
      y += rowH;
    }
    if (notApplicable.length) {
      checkRoom(21);
      const idsL = wrapCell(notApplicable.map((r) => r.id.replace("DPDP-", "")).join(", "), cols[0] + cols[1] - 10, 7, true);
      const rowH = Math.max(21, idsL.length * 8.2 + 10);
      idsL.forEach((ln, k) => text(ln, M + 6, y + 13 + k * 8.2, { size: 7, bold: true }));
      text("Not applicable: left out unless declared.", M + cols[0] + cols[1] + 6, y + 13, { size: 6.1, color: MUTED, width: cols[2] + cols[3] - 12 });
      pill("N/A", M + cols[0] + cols[1] + cols[2] + cols[3] + 4, y + 5, "mute", 6.2);
      y += rowH;
      stroke(RULE);
      doc.line(M, y, M + CW, y);
    }
    y += 4;
    for (const ln of wrapCell(OMITTED_DUTIES_NOTE, CW, 6.8)) {
      checkRoom(9, false);
      text(ln, M, y + 6, { size: 6.8, color: MUTED });
      y += 8.2;
    }
  }
  // ============================== what to fix first, and the dedicated sections ==============================
  // Flows on from the table of every check, onto as many pages as it needs. The wording of each section is worked out once (report/fixes via
  // dpdpScore and policyAnalysis, report/insights) and is the same as the dashboard's.
  {
    const MW = makeWriter(y);
    const cur2 = MW.w;
    const heading = (title: string) => {
      MW.room(84);
      cur2.y = section(title, cur2.y + 18) + 2;
    };

    // --- prioritised fixes ---
    heading("Prioritised fixes");
    const policyView = buildPolicyView(model.policyAnalysis);
    const scoreFixes = v ? v.remediationPlan : [];
    const policyFixes = policyView?.fixes ?? [];
    MW.para("In the order to do them. Where a fix changes the score, its impact is the score's own arithmetic: that one check fixed on its own and nothing else changed. Where it changes the policy, the impact is the policy's completeness. Each fix says why it matters and where the law says so.", { size: 7.6, color: MUTED });
    if (!v && !policyView) {
      MW.para("The score and the policy analysis were not available for this report, so no fixes can be listed.", { size: 8.5 });
    }
    if (v) {
      cur2.y += 4;
      MW.para("Fixes that change the score", { size: 9.2, bold: true, color: DEEP });
      if (scoreFixes.length === 0) MW.para("No fix is listed for the score: no check failed with enough evidence.", { size: 8, indent: 6 });
      for (const f of scoreFixes) {
        MW.room(60);
        MW.entry(`${f.rank}. ${f.title}`, [{ label: f.priority, tone: f.timing === "Current" ? "bad" : "warn" }, { label: f.outcome, tone: "mute" }], { indent: 6, size: 8.2 });
        MW.para(`Action: ${f.action}`, { size: 7.6, indent: 12 });
        if (f.scoreLine) MW.para(`Impact on the score: ${f.scoreLine}`, { size: 7.6, bold: true, indent: 12 });
        if (f.impact) MW.para(`Why it matters: ${f.impact}`, { size: 7.6, indent: 12 });
        MW.para(`How to verify: ${f.howToVerify}`, { size: 7.4, indent: 12 });
        MW.para(formatRef(f.legal), { size: 7, color: MUTED, indent: 12 });
        cur2.y += 4;
      }
    }
    if (policyView) {
      cur2.y += 4;
      MW.para("Fixes to the privacy policy's wording", { size: 9.2, bold: true, color: DEEP });
      if (!policyView.available) MW.para(`${policyView.reason ?? "The policy analysis is not available for this scan."}`, { size: 8, indent: 6 });
      else if (policyFixes.length === 0) MW.para("The policy covers every point that applies to this site.", { size: 8, indent: 6 });
      for (const f of policyFixes) {
        MW.room(60);
        MW.entry(`${f.rank}. ${f.row.title}`, [{ label: f.kind, tone: f.kind === "Required" ? "warn" : "mute" }, { label: POLICY_STATUS_LABEL[f.row.status] ?? f.row.status, tone: f.row.status === "partial" ? "warn" : "bad" }], { indent: 6, size: 8.2 });
        MW.para(`Action: ${f.row.fix}`, { size: 7.6, indent: 12 });
        if (f.impactLine) MW.para(`Impact on the policy: ${f.impactLine}`, { size: 7.6, bold: true, indent: 12 });
        MW.para(`Why it matters: ${f.why}`, { size: 7.4, indent: 12 });
        cur2.y += 4;
      }
    }
    MW.para("What each rests on: the score fixes rest on what the scan measured. The policy fixes rest on the policy's own text, where a point not found is not a finding that a duty is unmet.", { size: 7, color: MUTED });

    // --- the dedicated sections: grievance officer, DPO, Indian-language support, children's data and age gate, security signals ---
    for (const s of model.insights) {
      heading(s.title);
      const tone: Tone = s.tone === "good" ? "good" : s.tone === "warn" ? "warn" : "mute";
      MW.room(30);
      const wStatus = pillWidth(s.status, 7);
      pill(s.status, M, cur2.y, tone, 7);
      pill(s.knowledge, M + wStatus + 6, cur2.y, "mute", 7);
      cur2.y += 20;
      MW.para(s.summary, { size: 8 });
      if (s.facts.length) {
        MW.table([{ head: "What the scan looked at", w: 200, bold: true }, { head: "What it found", w: CW - 200 }], s.facts.map((f) => [f.label, f.value]), 7.4);
      }
      if (s.quote) MW.quoteBlock(s.quote.text, s.quote.source);
      MW.para(s.legal, { size: 7, color: MUTED });
      for (const n of s.notes) MW.para(n, { size: 7, color: MUTED });
      cur2.y += 4;
    }

    // --- the five-page readiness review ---
    {
      const rv = model.readiness;
      heading("Five-page DPDP readiness review");
      MW.para(rv.timing, { size: 7.4, color: MUTED });
      if (!rv.available) {
        MW.para(rv.reason ?? "The review is not available for this scan.", { size: 8.5 });
      } else {
        MW.para("A. The five pages", { size: 9.2, bold: true, color: DEEP });
        MW.table(
          [{ head: "#", w: 18 }, { head: "Page", w: 112, bold: true }, { head: "URL", w: 168 }, { head: "Why selected", w: CW - 298 }],
          rv.pages.map((p) => [String(p.n), p.role, p.url ? (p.scanned ? p.url : `${p.url} (named, not loaded in this scan)`) : "NOT FOUND", p.why]), 7,
        );
        MW.para("B. Findings, one row per check", { size: 9.2, bold: true, color: DEEP });
        MW.para(ORDER_COUNTS.map((k) => `${rv.counts[k]} ${k}`).join("  ·  "), { size: 7.6, bold: true });
        cur2.y += 2;
        MW.table(
          [{ head: "Page", w: 50, bold: true }, { head: "Section", w: 62 }, { head: "Check", w: 112 }, { head: "Status", w: 52, bold: true }, { head: "Evidence", w: 150 }, { head: "Fix", w: CW - 426 }],
          rv.rows.map((r) => [r.page, r.sections, r.check, [r.status, `(${r.evidenceKind})`], r.evidence, r.fix]), 6.4,
        );
        MW.para("C. Summary", { size: 9.2, bold: true, color: DEEP });
        MW.para(`Readiness score: ${rv.scoreLine}`, { size: 8, bold: true });
        MW.para("Top gaps, ranked by risk", { size: 8.2, bold: true });
        if (rv.topGaps.length === 0) MW.para("No gap or missing item was found in the checks the scan could assess.", { size: 7.8, indent: 6 });
        for (const g of rv.topGaps) {
          MW.room(36);
          MW.entry(`${g.rank}. ${g.row.check}`, [{ label: `${g.riskLabel} risk`, tone: g.riskLabel === "High" ? "bad" : g.riskLabel === "Medium" ? "warn" : "mute" }], { indent: 6, size: 7.8 });
          MW.para(`${g.row.page}: ${g.row.status}. Fix: ${g.row.fix}`, { size: 7.2, indent: 12 });
        }
        MW.para(`Highest-risk page: ${rv.riskiest ? `${rv.riskiest.n}. ${rv.riskiest.page}. ${rv.riskiest.why}` : "None: no page carries a gap on what the scan could assess."}`, { size: 8, bold: true });
        for (const n of rv.notes) MW.para(n, { size: 7, color: MUTED });
        MW.para(rv.closing, { size: 7.6, bold: true });
      }
      cur2.y += 4;
    }
    y = cur2.y;
  }

  // ============================== website pages scanned ==============================
  // Which pages this scan visited, from the stored page records of the same scan run as the score, and a short review of how it went.
  {
    // It continues on the page the table of checks ended on when there is room for a useful start, and begins a new page when there is not.
    let py: number;
    if (y + 190 <= FOOTER_Y - 14) {
      py = section("Website Pages Scanned", y + 16);
    } else {
      footer(doc.getNumberOfPages());
      doc.addPage("a4", "portrait");
      pageTop(doc.getNumberOfPages());
      py = section("Website Pages Scanned", 74);
    }
    const sp = model.scanPages;
    const siteHost = model.siteUrl.replace(/^https?:\/\//, "").replace(/\/$/, "");
    py = text(
      `Scan run ${model.scanId ? model.scanId.slice(0, 8) : "not recorded"} of ${siteHost}. Listed as the scan recorded them: the homepage first, then the pages that loaded, then any attempt that did not.`,
      M, py + 8, { size: 8, color: MUTED, width: CW, lead: 10 },
    ) + 4;
    if (sp) py = text(sp.coverageLine, M, py + 12, { size: 12, bold: true }) + 2;

    const room = (h: number) => {
      if (py + h <= FOOTER_Y - 14) return;
      footer(doc.getNumberOfPages());
      doc.addPage("a4", "portrait");
      pageTop(doc.getNumberOfPages());
      py = 74;
    };
    /** A URL has no spaces to wrap at, so it is broken by width, character by character. */
    const wrapUrl = (url: string, width: number, size: number): string[] => {
      doc.setFont("helvetica", "normal");
      doc.setFontSize(size);
      const lines: string[] = [];
      let line = "";
      for (const ch of url) {
        if (line && doc.getTextWidth(line + ch) > width) {
          lines.push(line);
          line = "";
        }
        line += ch;
      }
      if (line) lines.push(line);
      return lines.length ? lines : [""];
    };

    if (!sp || sp.rows.length === 0) {
      py = text(
        sp
          ? "The scan recorded no pages."
          : "The list of pages could not be loaded for this report, so the pages are not shown. The scan itself is unaffected.",
        M, py + 14, { size: 9, color: MUTED, width: CW },
      );
    } else {
      const cols = [28, 122, 273, 100];
      const size = 7.8;
      const lead = 9.6;
      fill(TINT);
      doc.rect(M, py, CW, 18, "F");
      let hx = M;
      ["No.", "Page type", "Page URL", "Scan status"].forEach((h, i) => {
        text(h.toUpperCase(), hx + 6, py + 12, { size: 6.4, bold: true, color: MUTED });
        hx += cols[i];
      });
      py += 18;
      for (const r of sp.rows.slice(0, MAX_LISTED_PAGES)) {
        const urlLines = wrapUrl(r.url, cols[2] - 12, size);
        doc.setFont("helvetica", "normal");
        doc.setFontSize(size);
        const typeLines = doc.splitTextToSize(r.pageType, cols[1] - 12) as string[];
        const statusLines = r.scanned ? [""] : (doc.splitTextToSize(r.status, cols[3] - 10) as string[]);
        const lines = Math.max(urlLines.length, typeLines.length, statusLines.length, 1);
        const h = Math.max(24, lines * lead + 10);
        room(h);
        let x = M;
        text(String(r.no), x + 6, py + 14, { size, bold: true });
        x += cols[0];
        typeLines.forEach((ln, i) => text(ln, x + 6, py + 14 + i * lead, { size, bold: r.no === 1 }));
        x += cols[1];
        urlLines.forEach((ln, i) => text(ln, x + 6, py + 14 + i * lead, { size, color: DEEP }));
        if (/^https?:\/\//i.test(r.url)) doc.link(x + 6, py + 5, cols[2] - 12, urlLines.length * lead + 2, { url: r.url });
        x += cols[2];
        if (r.scanned) {
          pill(r.status.replace(/^Scanned /, "Scanned "), x + 6, py + 5, "good", 6.8);
        } else {
          statusLines.forEach((ln, i) => text(ln, x + 6, py + 14 + i * lead, { size: 7.4, bold: true, color: TONES.bad.fg }));
        }
        stroke(RULE);
        doc.setLineWidth(0.4);
        doc.line(M, py + h, M + CW, py + h);
        py += h;
      }
      py += 6;
      py = text(
        `${Math.min(sp.rows.length, MAX_LISTED_PAGES)} ${Math.min(sp.rows.length, MAX_LISTED_PAGES) === 1 ? "page" : "pages"} listed${sp.pageLimit ? `, of a limit of ${sp.pageLimit}` : ""}` +
        `${sp.rows.length > MAX_LISTED_PAGES ? `; ${sp.rows.length - MAX_LISTED_PAGES} more recorded by an earlier, larger scan are not listed` : ""}. Select a URL to open it.`,
        M, py + 8, { size: 7.4, color: MUTED },
      );
    }

    if (sp) {
      room(60);
      py = section("Scan Review", py + 10);
      py += 4;
      for (const item of sp.review) {
        doc.setFont("helvetica", "normal");
        doc.setFontSize(8);
        const lines = doc.splitTextToSize(`${item.label}: ${item.text}`, CW - 20) as string[];
        room(lines.length * 10 + 6);
        lines.forEach((ln, i) => {
          if (i === 0) {
            doc.setFont("helvetica", "bold");
            doc.setFontSize(8);
            ink(INK);
            doc.text(`${item.label}:`, M + 8, py + 8);
            const lw = doc.getTextWidth(`${item.label}: `);
            doc.setFont("helvetica", "normal");
            ink(INK);
            doc.text(ln.slice(item.label.length + 2), M + 8 + lw, py + 8);
          } else {
            doc.setFont("helvetica", "normal");
            ink(INK);
            doc.text(ln, M + 8, py + 8);
          }
          py += 10;
        });
        py += 5;
      }
    }
    footer(doc.getNumberOfPages());
  }

  // ============================== evidence appendix ==============================
  // The complete record of what the scan found, with nothing cut short: the sector reading, what the policies say, the consent banner, every
  // form with every field, every cookie, every third-party request, the policies and services, and each compliance finding with the evidence
  // behind it. It follows the summary pages and runs as long as the evidence does. Sections are lettered in the order they appear, and
  // every legal reference is spelt the same way (report/legalRefs.ts).
  if (model.evidence) {
    const ev = model.evidence;
    const W = makeWriter(74);
    const cur = W.w;
    const { room: aroom, para, entry, quoteBlock, table } = W;
    const STATE_NAME: Record<string, string> = { pre_consent: "Before consent", post_accept: "After Accept", post_reject: "After Reject" };
    const states = (s: string[]) => (s.length ? s.map((x) => STATE_NAME[x] ?? x).join(", ") : "Not recorded");
    const yesNo = (b: boolean | null | undefined) => (b === null || b === undefined ? "Not recorded" : b ? "Yes" : "No");
    const bare = (u: string | null | undefined) => (u ? u.replace(/^https?:\/\//, "") : "Not recorded");

    /** A section header: a tinted bar with the section's letter, its title and (optionally) a count. Listed in the contents. */
    let letterNo = 0;
    const asection = (title: string, count?: string) => {
      aroom(58);
      const letter = String.fromCharCode(65 + letterNo++);
      const y0 = cur.y + 14;
      outline.push({ label: `Appendix ${letter}  ${title}`, page: doc.getNumberOfPages() });
      box(M, y0, CW, 24, { bg: TINT, r: 6 });
      fill(PURPLE);
      doc.circle(M + 15, y0 + 12, 8, "F");
      text(letter, M + 15, y0 + 15, { size: 8.5, bold: true, color: WHITE, align: "center" });
      text(title, M + 31, y0 + 15.8, { size: 10.5, bold: true });
      if (count) pill(count, M + CW - pillWidth(count, 7) - 8, y0 + 6, "mute", 7);
      cur.y = y0 + 24 + 9;
    };

    const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

    doc.addPage("a4", "portrait");
    pageTop(doc.getNumberOfPages());
    text("Evidence appendix", M, 76, { size: 20, bold: true });
    cur.y = 86;
    para("Everything the scan recorded for this site, in full: no entry is shortened or left out. The summary pages draw on this record.", { size: 8.3, color: MUTED });
    // An index of the sections below. Room is kept for it now and it is filled in at the end, when every section's page is known.
    const plannedSections = (v?.sector ? 1 : 0) + (buildPolicyView(model.policyAnalysis) ? 1 : 0) + 6 + (v ? 1 : 0);
    const indexH = plannedSections * 10.5 + 32;
    cur.y += 8;
    const indexPage = doc.getNumberOfPages();
    const indexTop = cur.y;
    cur.y += indexH;

    // --- sector and applicability ---
    const sec = v?.sector ?? null;
    if (sec) {
      asection("Sector and applicability", sec.confidence === "declared" ? "Declared" : `${sec.confidence} confidence`);
      para(`${sec.label}. ${sec.summary}${sec.also_possible.length ? ` Also possible: ${sec.also_possible.join(", ")}.` : ""}`, { size: 8.6, bold: true });
      if (sec.signals.length) para(`Seen in the scan: ${sec.signals.map((s) => `"${s.match}" (${s.source})`).join(", ")}.`, { size: 7.4, color: MUTED });
      for (const n of sec.notes) {
        cur.y += 5;
        entry(n.title, []);
        para(formatRefs(n.provisions), { size: 7, color: MUTED });
        para(n.text, { size: 7.9 });
        for (const o of n.observed) para(`Observed: ${o}`, { size: 7.6, indent: 10 });
        para(`What to check: ${n.action}`, { size: 7.6, bold: true, indent: 10 });
      }
      if (sec.declarations.length) {
        cur.y += 5;
        para("Worth declaring (these requirements stay out of the score until declared):", { size: 8, bold: true });
        for (const d of sec.declarations) {
          para(`${d.requirement_id}: ${d.label}. ${d.declared === null ? "Not declared" : d.declared ? "Declared applicable" : "Declared not applicable"}. ${d.why}`, { size: 7.6, indent: 10 });
        }
      }
      cur.y += 2;
      para(sec.limitations.join(" "), { size: 7, color: MUTED });
    }

    // --- privacy policy content analysis: what the policies say, with exact quotations and the provisions each point rests on ---
    const policy = buildPolicyView(model.policyAnalysis);
    if (policy) {
      asection("Privacy policy content analysis", policy.available ? policy.headline.replace("Policy completeness ", "") : "Not available");
      if (!policy.available) {
        para(`${policy.headline}. ${policy.reason ?? ""}`, { size: 8.5, bold: true });
        for (const n of policy.notes) para(n, { size: 7.4, color: MUTED });
      } else {
        para(policy.headline, { size: 10.5, bold: true });
        para(`${policy.summaryLine}.`, { size: 7.8, color: MUTED });
        cur.y += 4;
        para("Checklist", { size: 8.8, bold: true });
        table(
          [{ head: "Point the policy should cover", w: CW - 262, bold: true }, { head: "Status", w: 100 }, { head: "Basis", w: 162 }],
          policy.checklist.map((c) => [c.title, c.status, c.basis]), 7.2,
        );
        for (const g of policy.groups) {
          aroom(70);   // a group heading is never left alone at the foot of a page
          cur.y += 6;
          para(g.name, { size: 9.2, bold: true, color: DEEP });
          for (const r of g.rows) {
            aroom(34);
            entry(r.title, [
              { label: POLICY_STATUS_LABEL[r.status] ?? r.status, tone: r.status === "verified" ? "good" : r.status === "not_applicable" ? "mute" : r.status === "partial" ? "warn" : "bad" },
              { label: POLICY_BASIS_LABEL[r.basis] ?? r.basis, tone: "mute" },
            ], { indent: 6, size: 8 });
            para(r.provisions.join(" · "), { size: 7, color: MUTED, indent: 12 });
            if (r.quote) quoteBlock(r.quote, r.source_url);
            para(r.detail, { size: 7.4, indent: 12 });
            cur.y += 4;
          }
        }
      }
      if (policy.documents.length) para(`Read: ${policy.documents.join(" · ")}`, { size: 7, color: MUTED });
      para(policy.limitations.join(" "), { size: 7, color: MUTED });
    }

    // --- consent banner ---
    asection("Consent banner", ev.consent_signals.length === 0 ? "None detected" : plural(ev.consent_signals.length, "mechanism"));
    if (ev.consent_signals.length === 0) {
      para("No consent mechanism was detected on the pages scanned.", { size: 8.5 });
    }
    const label = (k: string) => k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
    const show = (val: unknown): string => {
      if (val === null || val === undefined || val === "") return "Not recorded";
      if (typeof val === "boolean") return val ? "Yes" : "No";
      if (Array.isArray(val)) return val.length ? val.map(String).join(", ") : "None";
      if (typeof val === "object") return JSON.stringify(val);
      return String(val);
    };
    ev.consent_signals.forEach((s, i) => {
      if (ev.consent_signals.length > 1) para(`Consent mechanism ${i + 1}`, { size: 8.4, bold: true });
      table(
        [{ head: "Detail", w: 190, bold: true }, { head: "Recorded", w: CW - 190 }],
        [
          ["Mechanism", s.mechanism_type], ["Consent tool (vendor)", s.cmp_vendor ?? "Not identified"],
          ["Reject-all option", yesNo(s.has_reject_all)], ["Granular choices", yesNo(s.has_granular_choices)],
          ...Object.entries(s.evidence ?? {}).map(([k, val]) => [label(k), show(val)]),
        ],
      );
    });

    // --- forms ---
    asection("Forms found", plural(ev.forms.length, "form"));
    if (ev.forms.length === 0) para("No forms were found on the pages scanned.", { size: 8.5 });
    ev.forms.forEach((f, i) => {
      aroom(56);
      entry(`Form ${i + 1}${f.purpose_guess ? `: ${label(f.purpose_guess)}` : ""} — ${plural(f.fields.length, "field")}${f.selector ? ` (${f.selector})` : ""}`, []);
      table(
        [{ head: "Field name", w: CW - 230, bold: true }, { head: "Type", w: 130 }, { head: "Required", w: 100 }],
        f.fields.length ? f.fields.map((fl) => [fl.name || "(unnamed)", fl.field_type || "Not recorded", fl.required ? "Yes" : "No"]) : [["No fields were recorded", "", ""]],
      );
      cur.y += 2;
    });

    // --- cookies: the inventory, grouped by what the cookies are for ---
    const inv = model.cookies;
    asection("Cookie inventory", plural(inv.total, "cookie"));
    if (inv.total === 0) para("No cookies were recorded.", { size: 8.5 });
    else {
      // the counts, from the same rows as the table below
      const tiles: [string, string][] = [
        ["Cookies found", String(inv.summary.found)], ["Third-party", String(inv.summary.thirdParty)], ["Set before consent", String(inv.summary.beforeConsent)],
        ["Long-lived (over a year)", String(inv.summary.longLived)], ["Seen after Reject", inv.summary.afterReject === null ? "Not tested" : String(inv.summary.afterReject)],
      ];
      aroom(52);
      const tw = (CW - 6 * 4) / 5;
      tiles.forEach(([label, value], i) => {
        const x = M + i * (tw + 6);
        box(x, cur.y, tw, 40, { bg: TINT, r: 6 });
        text(value, x + 8, cur.y + 19, { size: value.length > 6 ? 11 : 16, bold: true });
        text(label, x + 8, cur.y + 32, { size: 6.4, color: MUTED, width: tw - 12 });
      });
      cur.y += 48;
      para("Long-lived means the cookie expires more than a year after it was set. The Act and Rules set no limit on a cookie's lifetime, so this is information, not a finding. A purpose is given only for a recognised cookie, in its provider's own words; for any other the scan says what it could establish and that the purpose could not be identified from the name. “Not tested” means no choice was made in that pass, so the cookie's absence there says nothing.", { size: 7, color: MUTED });
      const iso = (s: string) => /^\d{4}-\d{2}-\d{2}/.exec(s)?.[0] ?? null;
      for (const g of inv.groups) {
        aroom(70);
        cur.y += 4;
        para(`${g.title} (${g.rows.length})`, { size: 9.2, bold: true, color: DEEP });
        para(g.note, { size: 7, color: MUTED });
        table(
          [{ head: "Cookie", w: 128, bold: true }, { head: "Provider", w: 60 }, { head: "Party", w: 44 }, { head: "Lifetime", w: 62 }, { head: "Purpose", w: 137 }, { head: "Before", w: 30 }, { head: "Accept", w: 31 }, { head: "Reject", w: 31 }],
          g.rows.map((r) => {
            const pages = [...new Set(r.sightings.map((s) => s.page).filter((p) => p !== "Not available").map((p) => p.replace(/^https?:\/\//, "")))];
            const flags = `Secure: ${r.secure === "Not available" ? "n/a" : r.secure} · HttpOnly: ${r.httpOnly === "Not available" ? "n/a" : r.httpOnly} · SameSite: ${r.sameSite === "Not available" ? "n/a" : r.sameSite}`;
            const expires = iso(r.expiry);
            return [
              [r.name, r.domain, ...pages.map((p) => `On ${p}`)],
              r.provider,
              r.party === "Not available" ? "Not recorded" : r.party,
              [r.lifetime + (r.longLived ? " (long-lived)" : ""), ...(expires ? [`expires ${expires}`] : []), flags],
              r.purpose + (r.purposeSource === "catalogue" ? "" : r.purposeSource === "category" ? " (from its classification only)" : ""),
              r.behaviour.before, r.behaviour.accept, r.behaviour.reject,
            ];
          }),
          6.6,
        );
      }
    }

    // --- third-party requests ---
    const trackerRank = (t: { category: string | null }) => (t.category === "marketing" ? 0 : t.category === "analytics" ? 1 : t.category ? 3 : 2);
    const trackers = [...ev.trackers].sort((a, b) => trackerRank(a) - trackerRank(b));
    asection("Third-party requests and scripts", plural(trackers.length, "request"));
    if (trackers.length === 0) para("No third-party requests were recorded.", { size: 8.5 });
    else {
      para("Listed with marketing first, then analytics, then everything else. A request shows the browser contacted a service; it does not show that a tracker ran or what it sent.", { size: 7.2, color: MUTED });
      table(
        [{ head: "Vendor", w: 88, bold: true }, { head: "Category", w: 62 }, { head: "Seen", w: 84 }, { head: "Request URL", w: CW - 88 - 62 - 84 - 100 }, { head: "On page", w: 100 }],
        trackers.map((t) => [t.vendor ?? "Unclassified", t.category ?? "unclassified", states(t.consent_states), t.script_src, bare(t.page_url)]),
        6.9,
      );
    }

    // --- policies and services ---
    asection("Policies found", plural(ev.policies.length, "policy", "policies"));
    if (ev.policies.length === 0) para("No privacy, cookie or terms page was found on the pages scanned.", { size: 8.5 });
    else table([{ head: "Type", w: 130, bold: true }, { head: "URL", w: CW - 130 }], ev.policies.map((p) => [label(p.policy_type), p.url]));
    asection("Third-party services", plural(ev.third_party_services.length, "service"));
    if (ev.third_party_services.length === 0) para("No third-party service was identified.", { size: 8.5 });
    else table([{ head: "Service", w: 150, bold: true }, { head: "Category", w: 100 }, { head: "Domains", w: CW - 250 }], ev.third_party_services.map((s) => [s.service_name, s.category ?? "unclassified", s.domains.join(", ") || "Not recorded"]));

    // --- compliance findings ---
    if (v) {
      const hidden = new Set(v.allChecks.filter((c) => !isReportCategory(c.category)).map((c) => c.id));
      const groups = v.groups.map((g) => ({ ...g, entries: g.entries.filter((e) => !hidden.has(e.id)) })).filter((g) => g.entries.length);
      asection("Compliance findings in full", plural(groups.reduce((n, g) => n + g.entries.length, 0), "finding"));
      if (groups.length === 0) para("No findings were recorded for the checks that could be tested.", { size: 8.5 });
      const outcomeTone = (key: string): Tone => (key === "confirmed" ? "bad" : key === "unable_to_verify" ? "mute" : "warn");
      for (const g of groups) {
        aroom(80);   // a group heading is never left alone at the foot of a page
        cur.y += 4;
        para(`${g.title} (${g.entries.length})`, { size: 9.2, bold: true, color: DEEP });
        for (const e of g.entries) {
          aroom(44);
          const pills: { label: string; tone: Tone }[] = [{ label: e.outcome, tone: outcomeTone(g.key) }];
          if (e.severity && !/not rated/.test(e.severity)) pills.push({ label: e.severity.replace(/^Severity:\s*/, ""), tone: "warn" });
          entry(stripId(e.title), pills, { indent: 6, size: 8.2 });
          if (e.legal) para(`Legal basis: ${formatRef(e.legal)}`, { size: 7, color: MUTED, indent: 12 });
          if (e.timingNote) para(e.timingNote, { size: 7, color: MUTED, indent: 12 });
          for (const o of e.observed) para(`Observed: ${stripRuleCodes(o)}`, { size: 7.4, indent: 12 });
          if (e.pages.length) para(`Pages: ${e.pages.join(", ")}`, { size: 7, color: MUTED, indent: 12 });
          if (e.explanation) para(stripRuleCodes(e.explanation), { size: 7.4, indent: 12 });
          for (const m of e.missing) para(`Missing: ${m}`, { size: 7.4, indent: 12 });
          if (e.remediation) para(`Remediation: ${e.remediation}`, { size: 7.4, indent: 12 });
          cur.y += 5;
        }
      }
    }
    footer(doc.getNumberOfPages());

    // The appendix index, now that every section's page is known.
    const appendixEntries = outline.filter((o) => o.label.startsWith("Appendix "));
    doc.setPage(indexPage);
    box(M, indexTop, CW, indexH, { bg: TINT, border: RULE, r: 8 });
    text("IN THIS APPENDIX", M + 12, indexTop + 15, { size: 7.5, bold: true, color: MUTED });
    appendixEntries.forEach((o, i) => {
      const cy = indexTop + 30 + i * 10.5;
      text(o.label, M + 12, cy, { size: 7.8 });
      text(`p. ${o.page}`, M + CW - 12, cy, { size: 7.8, bold: true, color: DEEP, align: "right" });
    });
  }

  // Contents on page 1, in the room under the top findings, from the sections as they were actually drawn (so the page numbers are true).
  // Two columns when they fit; three, a little tighter, when the findings above leave less room.
  if (outline.length > 0) {
    const layout = [{ cols: 2, lead: 10 }, { cols: 3, lead: 9 }].map((l) => ({ ...l, rows: Math.ceil(outline.length / l.cols) }))
      .find((l) => contentsTop + l.rows * l.lead + 30 <= FOOTER_Y - 12);
    if (layout) {
      const boxH = layout.rows * layout.lead + 30;
      doc.setPage(1);
      box(M, contentsTop, CW, boxH, { bg: TINT, border: RULE, r: 8 });
      text("CONTENTS", M + 12, contentsTop + 15, { size: 7.5, bold: true, color: MUTED });
      const gap = 14;
      const colW = (CW - 24 - gap * (layout.cols - 1)) / layout.cols;
      outline.forEach((o, i) => {
        const cx = M + 12 + Math.floor(i / layout.rows) * (colW + gap);
        const cy = contentsTop + 29 + (i % layout.rows) * layout.lead;
        const lines = wrapCell(o.label, colW - 30, 7.2);
        text(lines.length > 1 ? `${lines[0].replace(/\s+\S*$/, "")}` : lines[0], cx, cy, { size: 7.2 });
        text(`p. ${o.page}`, cx + colW, cy, { size: 7.2, bold: true, color: DEEP, align: "right" });
      });
    } else if (contentsTop + 30 <= FOOTER_Y - 12) {
      // Too little room for the full list: one line of the main parts and where they start. The appendix has its own full index.
      const find = (starts: string) => outline.find((o) => o.label.replace(/^\d+\s{2}/, "").startsWith(starts));
      const wanted: [string, { label: string; page: number } | undefined][] = [
        ["Summary", find("Scan summary")], ["Consent behaviour", find("Consent behaviour")], ["Every check", find("Every check")],
        ["Fixes", find("Prioritised fixes")], ["Pages scanned", find("Website Pages")], ["Evidence appendix", outline.find((o) => o.label.startsWith("Appendix A"))],
      ];
      const parts = wanted.flatMap(([name, e]) => (e ? [[name, e] as const] : []));
      doc.setPage(1);
      box(M, contentsTop, CW, 24, { bg: TINT, border: RULE, r: 8 });
      text("CONTENTS", M + 12, contentsTop + 15, { size: 7.5, bold: true, color: MUTED });
      // How wide the strip is at a given type size; it must end inside the box.
      const widthAt = (list: typeof parts, size: number) => {
        doc.setFont("helvetica", "normal");
        doc.setFontSize(size);
        return list.reduce((sum, [name, e]) => sum + doc.getTextWidth(name) + 4 + doc.getTextWidth(`p. ${e.page}`) + 14, 0);
      };
      const room = CW - 76 - 14;
      let shown = [...parts];
      let size = 7.4;
      for (const smaller of [7.4, 7, 6.6, 6.2]) { size = smaller; if (widthAt(shown, smaller) <= room) break; }
      // Still too wide at the smallest size: drop the parts the reader can find fastest by scrolling, never the first or the appendix.
      for (const drop of ["Every check", "Consent behaviour", "Pages scanned"]) {
        if (widthAt(shown, size) <= room) break;
        shown = shown.filter(([name]) => name !== drop);
      }
      let cx = M + 76;
      for (const [name, e] of shown) {
        text(name, cx, contentsTop + 15, { size });
        const w = doc.getTextWidth(name);
        text(`p. ${e.page}`, cx + w + 4, contentsTop + 15, { size, bold: true, color: DEEP });
        cx += w + 4 + doc.getTextWidth(`p. ${e.page}`) + 14;
      }
    }
  }

  // Page numbers are stamped last, when the total is known.
  const totalPages = doc.getNumberOfPages();
  for (let p = 1; p <= totalPages; p++) {
    doc.setPage(p);
    text(`Page ${p} of ${totalPages}`, PAGE_W - M, FOOTER_Y + 11, { size: 7, color: MUTED, align: "right" });
  }

  const host = model.siteUrl.replace(/^https?:\/\//, "").replace(/[^a-zA-Z0-9.-]/g, "-").replace(/-+$/, "");
  const stamp = new Date().toISOString().slice(0, 10);
  doc.save(`consiva-privacy-report-${host || "report"}-${stamp}.pdf`);
}
