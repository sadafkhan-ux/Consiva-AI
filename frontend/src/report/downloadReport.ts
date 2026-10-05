import type { jsPDF as JsPdf } from "jspdf";
import type { ReportModel } from "./buildReport";
import { NOT_AVAILABLE, type GroupCount } from "./cookieInventory";

// A4 portrait in points. Everything below is laid out against these bounds so the
// output is genuinely ONE page -- text is drawn with jsPDF's own text API rather than
// rasterised from the DOM, so the PDF stays crisp, selectable and small.
const PAGE_W = 595.28;
const PAGE_H = 841.89;
const M = 48; // page margin
const CONTENT_W = PAGE_W - M * 2;
const BOTTOM_LIMIT = PAGE_H - 56; // leave room for the footer

const INK = { r: 24, g: 28, b: 38 };
const MUTED = { r: 108, g: 116, b: 132 };
const RULE = { r: 214, g: 219, b: 228 };

const RISK_COLORS: Record<string, { r: number; g: number; b: number }> = {
  critical: { r: 155, g: 28, b: 40 },
  high: { r: 190, g: 60, b: 40 },
  medium: { r: 178, g: 124, b: 24 },
  low: { r: 62, g: 118, b: 86 },
  info: { r: 82, g: 100, b: 130 },
  none: { r: 62, g: 118, b: 86 },
};

function riskColor(level: string) {
  return RISK_COLORS[level.toLowerCase()] ?? MUTED;
}

/** jsPDF (~400kB) stays in its own chunk so it never delays the first render, but the
 *  fetch starts as soon as this module loads instead of on the first click.
 *
 *  Waiting for the click broke the download after every redeploy: a tab opened before
 *  it still ran the old bundle, which asked for a chunk hash the new build no longer
 *  has, and nginx answered 404. Loaded up front, the library is already in memory by
 *  then. A reload would also have fixed it, but it throws away the scan on screen --
 *  nothing persists it -- which is the very thing being downloaded.
 *
 *  If this early fetch fails (offline, blocked), the click retries it once. */
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

export async function downloadReport(model: ReportModel): Promise<void> {
  const { jsPDF } = await loadJsPdf();
  const doc = new jsPDF({ unit: "pt", format: "a4", compress: true });
  let y = M;

  const setColor = (c: { r: number; g: number; b: number }) => doc.setTextColor(c.r, c.g, c.b);

  /** Draws wrapped text and advances y. Returns false once the page is full, so
   *  callers can stop adding optional sections rather than spilling to a page 2. */
  const writeWrapped = (text: string, size: number, lineGap: number, indent = 0): boolean => {
    doc.setFontSize(size);
    const lines = doc.splitTextToSize(text, CONTENT_W - indent) as string[];
    for (const line of lines) {
      if (y + lineGap > BOTTOM_LIMIT) return false;
      doc.text(line, M + indent, y);
      y += lineGap;
    }
    return true;
  };

  const sectionHeading = (label: string): boolean => {
    if (y + 26 > BOTTOM_LIMIT) return false;
    y += 10;
    doc.setFont("helvetica", "bold");
    doc.setFontSize(9);
    setColor(MUTED);
    doc.text(label.toUpperCase(), M, y);
    y += 5;
    doc.setDrawColor(RULE.r, RULE.g, RULE.b);
    doc.setLineWidth(0.6);
    doc.line(M, y, M + CONTENT_W, y);
    y += 13;
    doc.setFont("helvetica", "normal");
    setColor(INK);
    return true;
  };

  // ---------- header ----------
  doc.setFont("helvetica", "bold");
  doc.setFontSize(17);
  setColor(INK);
  doc.text("Consent Compliance Summary", M, y);
  y += 18;

  doc.setFont("helvetica", "normal");
  doc.setFontSize(10);
  setColor(MUTED);
  doc.text(`${model.siteUrl}`, M, y);
  y += 13;
  doc.setFontSize(8.5);
  doc.text(`Consiva AI · DPDP Act 2023 · generated ${model.generatedAt}`, M, y);
  y += 16;

  // ---------- overall risk banner ----------
  const rc = riskColor(model.overallRisk);
  doc.setFillColor(rc.r, rc.g, rc.b);
  doc.roundedRect(M, y, CONTENT_W, 38, 4, 4, "F");
  doc.setTextColor(255, 255, 255);
  doc.setFont("helvetica", "bold");
  doc.setFontSize(12);
  doc.text(`Overall risk: ${model.overallRisk.toUpperCase()}`, M + 14, y + 16);
  doc.setFont("helvetica", "normal");
  doc.setFontSize(9);
  doc.text(doc.splitTextToSize(model.headline, CONTENT_W - 28)[0] as string, M + 14, y + 29);
  y += 50;

  // Honesty marker: findings still pending review are NOT signed off, and a client
  // reading this PDF must not mistake it for an approved compliance position.
  if (model.provisional) {
    doc.setFont("helvetica", "italic");
    doc.setFontSize(8.5);
    setColor(MUTED);
    y += 2;
    writeWrapped(
      "Provisional — one or more findings are still awaiting human review and may change.",
      8.5, 11,
    );
    doc.setFont("helvetica", "normal");
    y += 2;
  }

  // ---------- scan at a glance ----------
  if (model.counts.length && sectionHeading("Scan at a glance")) {
    const colW = CONTENT_W / model.counts.length;
    for (let i = 0; i < model.counts.length; i++) {
      const x = M + colW * i;
      doc.setFont("helvetica", "bold");
      doc.setFontSize(14);
      setColor(INK);
      doc.text(model.counts[i].value, x, y);
      doc.setFont("helvetica", "normal");
      doc.setFontSize(7.5);
      setColor(MUTED);
      doc.text(model.counts[i].label.toUpperCase(), x, y + 10);
    }
    y += 24;
    setColor(INK);
  }

  // ---------- main problems ----------
  if (model.problems.length && sectionHeading("Main problems found")) {
    for (const p of model.problems) {
      if (y + 24 > BOTTOM_LIMIT) break;
      const pc = riskColor(p.risk);
      doc.setFillColor(pc.r, pc.g, pc.b);
      doc.circle(M + 3, y - 3, 3, "F");
      doc.setFont("helvetica", "bold");
      doc.setFontSize(8);
      setColor(pc);
      doc.text(p.risk.toUpperCase(), M + 12, y);
      doc.setFont("helvetica", "normal");
      setColor(INK);
      y += 11;
      if (!writeWrapped(p.text, 9.5, 12, 12)) break;
      y += 5;
    }
  }

  // ---------- consent & tracker issues ----------
  if (model.consentIssues.length && sectionHeading("Key consent & tracker issues")) {
    for (const issue of model.consentIssues) {
      if (y + 12 > BOTTOM_LIMIT) break;
      doc.text("•", M, y);
      if (!writeWrapped(issue, 9.5, 12, 12)) break;
      y += 2;
    }
  }

  // ---------- recommended actions ----------
  if (model.actions.length && sectionHeading("Recommended actions")) {
    let n = 1;
    for (const a of model.actions) {
      if (y + 12 > BOTTOM_LIMIT) break;
      doc.setFont("helvetica", "bold");
      doc.text(`${n}.`, M, y);
      doc.setFont("helvetica", "normal");
      if (!writeWrapped(a, 9.5, 12, 14)) break;
      y += 2;
      n++;
    }
  }

  // ---------- DPDP references ----------
  if (model.dpdpRefs.length && sectionHeading("DPDP references")) {
    doc.setFontSize(9);
    setColor(MUTED);
    writeWrapped(model.dpdpRefs.join("   ·   "), 9, 12);
    setColor(INK);
  }

  // ---------- footer ----------
  doc.setDrawColor(RULE.r, RULE.g, RULE.b);
  doc.setLineWidth(0.6);
  doc.line(M, PAGE_H - 44, M + CONTENT_W, PAGE_H - 44);
  doc.setFont("helvetica", "normal");
  doc.setFontSize(7.5);
  setColor(MUTED);
  const omitted =
    model.totalFindings > model.shownFindings
      ? ` Showing the ${model.shownFindings} most severe of ${model.totalFindings} findings — see the full report in Consiva for all detail.`
      : "";
  doc.text(
    doc.splitTextToSize(
      `Automated summary generated by the Consiva Consent Agent from a live scan of ${model.siteUrl}.` +
        ` Every legal reference is drawn from the retrieved DPDP source text.${omitted}` +
        " This is not legal advice.",
      CONTENT_W,
    ) as string[],
    M,
    PAGE_H - 33,
  );

  drawCookieInventory(doc, model);

  const host = model.siteUrl.replace(/^https?:\/\//, "").replace(/[^a-zA-Z0-9.-]/g, "-").replace(/-+$/, "");
  const stamp = new Date().toISOString().slice(0, 10);
  doc.save(`consiva-consent-summary-${host || "report"}-${stamp}.pdf`);
}

// ---------- cookie inventory (pages 2+) ----------
//
// The summary above stays one page by design; the inventory is the opposite -- it is
// complete, every cookie and every field, so it flows over as many landscape pages as
// it needs. Same derivation as the Cookies section on screen (cookieInventory.ts).

const L_W = PAGE_H; // landscape A4: the portrait height is the width
const L_H = PAGE_W;
const L_CONTENT_W = L_W - M * 2;
const L_BOTTOM = L_H - 40;

function drawCookieInventory(doc: JsPdf, model: ReportModel): void {
  const inv = model.cookies;
  let y = M;
  let page = 0;

  const setColor = (c: { r: number; g: number; b: number }) => doc.setTextColor(c.r, c.g, c.b);

  const newPage = () => {
    doc.addPage("a4", "landscape");
    page += 1;
    y = M;
    doc.setFont("helvetica", "bold");
    doc.setFontSize(page === 1 ? 15 : 11);
    setColor(INK);
    doc.text(page === 1 ? "Cookie inventory" : "Cookie inventory (continued)", M, y);
    y += page === 1 ? 14 : 10;
    doc.setFont("helvetica", "normal");
    doc.setFontSize(8.5);
    setColor(MUTED);
    doc.text(`${model.siteUrl} · every cookie the scan detected`, M, y);
    y += 16;
    // footer
    doc.setDrawColor(RULE.r, RULE.g, RULE.b);
    doc.setLineWidth(0.6);
    doc.line(M, L_H - 30, M + L_CONTENT_W, L_H - 30);
    doc.setFontSize(7.5);
    doc.text(
      `"${NOT_AVAILABLE}" = not recorded by the scan; no value is inferred. Times are UTC. Cookie inventory page ${page}.`,
      M, L_H - 19,
    );
    setColor(INK);
  };

  const ensure = (space: number) => {
    if (y + space > L_BOTTOM) newPage();
  };

  /** Wrapped text at x within width w; breaks onto a new page line by line. */
  const write = (text: string, x: number, w: number, size: number, gap: number) => {
    doc.setFontSize(size);
    for (const line of doc.splitTextToSize(text, w) as string[]) {
      ensure(gap);
      doc.text(line, x, y);
      y += gap;
    }
  };

  const heading = (label: string) => {
    ensure(30);
    y += 6;
    doc.setFont("helvetica", "bold");
    doc.setFontSize(9);
    setColor(MUTED);
    doc.text(label.toUpperCase(), M, y);
    y += 5;
    doc.setDrawColor(RULE.r, RULE.g, RULE.b);
    doc.setLineWidth(0.6);
    doc.line(M, y, M + L_CONTENT_W, y);
    y += 12;
    doc.setFont("helvetica", "normal");
    setColor(INK);
  };

  const groupLine = (label: string, groups: GroupCount[]) => {
    doc.setFont("helvetica", "bold");
    doc.setFontSize(9);
    ensure(12);
    doc.text(label, M, y);
    doc.setFont("helvetica", "normal");
    const text = groups.length ? groups.map((g) => `${g.label}: ${g.count}`).join("   ·   ") : "None";
    const startY = y;
    write(text, M + 130, L_CONTENT_W - 130, 9, 11.5);
    if (y === startY) y += 11.5;
    y += 3;
  };

  newPage();

  // ---------- totals and groupings ----------
  heading("Summary");
  doc.setFont("helvetica", "bold");
  doc.setFontSize(9);
  doc.text("Total unique cookies", M, y);
  doc.setFont("helvetica", "normal");
  doc.text(String(inv.total), M + 130, y);
  y += 14;
  groupLine("By consent state", inv.byState);
  groupLine("By purpose / category", inv.byCategory);
  groupLine("By domain", inv.byDomain);
  doc.setFontSize(8);
  setColor(MUTED);
  write(
    "A cookie seen in more than one consent state is counted once in the total and once in each state it was seen in.",
    M, L_CONTENT_W, 8, 10,
  );
  setColor(INK);

  if (inv.rows.length === 0) {
    heading("Cookies");
    write("No cookies were detected on this scan.", M, L_CONTENT_W, 9.5, 12);
    return;
  }

  // ---------- one block per cookie ----------
  heading(`Cookies (${inv.rows.length})`);
  const COLS = 4;
  const colW = L_CONTENT_W / COLS;

  inv.rows.forEach((r, i) => {
    ensure(70); // keep a cookie's header with at least its first lines
    doc.setFont("helvetica", "bold");
    doc.setFontSize(10);
    setColor(INK);
    doc.text(`${i + 1}. ${r.name}`, M, y);
    doc.setFont("helvetica", "normal");
    doc.setFontSize(7.5);
    setColor(MUTED);
    doc.text(`Evidence ID ${r.id}`, M + L_CONTENT_W, y, { align: "right" });
    setColor(INK);
    y += 13;

    const fields: [string, string][] = [
      ["Domain", r.domain],
      ["First- / third-party", r.party],
      ["Category / purpose", r.category],
      ["Vendor / service", r.vendor],
      ["Expiry", r.expiry],
      ["Secure", r.secure],
      ["HttpOnly", r.httpOnly],
      ["SameSite", r.sameSite],
    ];
    for (let start = 0; start < fields.length; start += COLS) {
      const rowFields = fields.slice(start, start + COLS);
      // Lay the row out first so it can be kept on one page.
      const wrapped = rowFields.map(([, v]) => doc.setFontSize(8.5).splitTextToSize(v, colW - 10) as string[]);
      const height = 10 + Math.max(...wrapped.map((w) => w.length)) * 10;
      ensure(height + 2);
      rowFields.forEach(([label], c) => {
        const x = M + colW * c;
        doc.setFontSize(7);
        setColor(MUTED);
        doc.text(label.toUpperCase(), x, y);
        doc.setFontSize(8.5);
        setColor(wrapped[c][0] === NOT_AVAILABLE ? MUTED : INK);
        wrapped[c].forEach((line, li) => doc.text(line, x, y + 10 + li * 10));
      });
      setColor(INK);
      y += height + 4;
    }

    doc.setFontSize(7);
    setColor(MUTED);
    ensure(10);
    doc.text("CONSENT STATE · EXACT PAGE · WHEN DETECTED · SOURCE", M, y);
    y += 10;
    setColor(INK);
    for (const s of r.sightings) {
      doc.setFont("helvetica", "bold");
      doc.setFontSize(8.5);
      ensure(11);
      doc.text(s.stateLabel, M, y);
      doc.setFont("helvetica", "normal");
      const lineX = M + 70;
      const lineW = L_CONTENT_W - 70;
      write(`Page: ${s.page}`, lineX, lineW, 8.5, 10);
      if (s.candidatePages.length) {
        setColor(MUTED);
        write(`First appeared while these pages loaded together: ${s.candidatePages.join(", ")}`, lineX, lineW, 8, 10);
        setColor(INK);
      }
      write(`Detected: ${s.detectedAt}`, lineX, lineW, 8.5, 10);
      setColor(MUTED);
      write(`Source: ${s.how}${s.sourceRequest ? ` (${s.sourceRequest})` : ""}`, lineX, lineW, 8, 10);
      setColor(INK);
      y += 3;
    }

    y += 4;
    if (i < inv.rows.length - 1) {
      ensure(8);
      doc.setDrawColor(RULE.r, RULE.g, RULE.b);
      doc.setLineWidth(0.4);
      doc.line(M, y - 4, M + L_CONTENT_W, y - 4);
      y += 6;
    }
  });
}
