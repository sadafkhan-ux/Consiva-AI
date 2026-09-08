import type { ReportModel } from "./buildReport";

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

/** jsPDF is imported dynamically so its ~150kB never enters the initial bundle -- the
 *  library is only fetched the first time someone actually asks for the PDF, which
 *  keeps the cost of this feature at zero for every scan that isn't downloaded. */
export async function downloadReport(model: ReportModel): Promise<void> {
  const { jsPDF } = await import("jspdf");
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

  const host = model.siteUrl.replace(/^https?:\/\//, "").replace(/[^a-zA-Z0-9.-]/g, "-").replace(/-+$/, "");
  const stamp = new Date().toISOString().slice(0, 10);
  doc.save(`consiva-consent-summary-${host || "report"}-${stamp}.pdf`);
}
