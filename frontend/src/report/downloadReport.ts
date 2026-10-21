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
const SCORE_BANNER = { r: 38, g: 50, b: 74 };
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

  // ---------- DPDP Compliance Score banner (replaces the old overall-risk banner) ----------
  // The same headline and coverage wording as the dashboard (report/dpdpScore.ts). A neutral colour: the score is not a risk level.
  doc.setFillColor(SCORE_BANNER.r, SCORE_BANNER.g, SCORE_BANNER.b);
  doc.roundedRect(M, y, CONTENT_W, 38, 4, 4, "F");
  doc.setTextColor(255, 255, 255);
  doc.setFont("helvetica", "bold");
  doc.setFontSize(12);
  doc.text(
    model.dpdpScore
      ? `DPDP Compliance Score (proposed methodology): ${model.dpdpScore.headline}`
      : "DPDP Compliance Score: not available for this report",
    M + 14, y + 16,
  );
  doc.setFont("helvetica", "normal");
  doc.setFontSize(9);
  doc.text(
    doc.splitTextToSize(
      model.dpdpScore ? `Assessment coverage (separate from the score): ${model.dpdpScore.coverageHeadline}. ${model.dpdpScore.coverageLine}` : model.headline,
      CONTENT_W - 28,
    )[0] as string,
    M + 14, y + 29,
  );
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

  // What was tested, so a quiet report is not read as a clean one.
  doc.setFont("helvetica", "normal");
  doc.setFontSize(8.5);
  setColor(MUTED);
  writeWrapped(model.scope, 8.5, 11);
  if (model.dpdpScore) {
    y += 2;
    writeWrapped(`${model.dpdpScore.scopeLines[0]} ${model.dpdpScore.scanTimeLine}.`, 8.5, 11);
    writeWrapped(`${model.dpdpScore.versionsLine}. ${model.dpdpScore.basisLine} Method, weights, evidence and remediation follow on the next page.`, 8.5, 11);
    if (model.dpdpScore.caution) writeWrapped(model.dpdpScore.caution, 8.5, 11);
  }
  y += 4;
  setColor(INK);

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

  drawDpdpScore(doc, model);
  drawCookieInventory(doc, model);

  const host = model.siteUrl.replace(/^https?:\/\//, "").replace(/[^a-zA-Z0-9.-]/g, "-").replace(/-+$/, "");
  const stamp = new Date().toISOString().slice(0, 10);
  doc.save(`consiva-consent-summary-${host || "report"}-${stamp}.pdf`);
}

// ---------- DPDP Compliance Score (after the summary page) ----------
//
// Every figure and sentence comes from report/dpdpScore.ts, the same view the dashboard renders, so the two cannot differ.
// It flows over as many portrait pages as it needs, and says "Insufficient Evidence" instead of a number when the score is
// withheld.

const AMBER = { r: 178, g: 112, b: 20 };
const AMBER_FILL = { r: 253, g: 246, b: 232 };
const AMBER_RULE = { r: 233, g: 205, b: 150 };

function drawDpdpScore(doc: JsPdf, model: ReportModel): void {
  const v = model.dpdpScore;
  if (!v) return;

  let y = M;
  let page = 0;
  const setColor = (c: { r: number; g: number; b: number }) => doc.setTextColor(c.r, c.g, c.b);

  const footer = () => {
    doc.setDrawColor(RULE.r, RULE.g, RULE.b);
    doc.setLineWidth(0.6);
    doc.line(M, PAGE_H - 44, M + CONTENT_W, PAGE_H - 44);
    doc.setFont("helvetica", "normal");
    doc.setFontSize(7.5);
    setColor(MUTED);
    doc.text(
      doc.splitTextToSize(`DPDP Compliance Score, ${model.siteUrl}. ${v.methodologyLine}. Proposed methodology, not legal advice. Page ${page}.`, CONTENT_W) as string[],
      M, PAGE_H - 33,
    );
    setColor(INK);
  };
  const newPage = () => {
    doc.addPage("a4", "portrait");
    page += 1;
    y = M;
    doc.setFont("helvetica", "bold");
    doc.setFontSize(page === 1 ? 15 : 11);
    setColor(INK);
    doc.text(page === 1 ? "DPDP Compliance Score" : "DPDP Compliance Score (continued)", M, y);
    y += page === 1 ? 16 : 12;
    doc.setFont("helvetica", "normal");
    doc.setFontSize(8.5);
    setColor(MUTED);
    doc.text(`${model.siteUrl} · proposed methodology`, M, y);
    y += 16;
    setColor(INK);
    footer();
  };
  const ensure = (space: number) => {
    if (y + space > BOTTOM_LIMIT) newPage();
  };
  const write = (text: string, x: number, w: number, size: number, gap: number) => {
    doc.setFontSize(size);
    for (const line of doc.splitTextToSize(text, w) as string[]) {
      ensure(gap);
      doc.text(line, x, y);
      y += gap;
    }
  };
  const heading = (label: string) => {
    ensure(34);
    y += 6;
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
  };
  const rows = (items: typeof v.assessed) => {
    for (const r of items) {
      ensure(40);
      doc.setFont("helvetica", "bold");
      doc.setFontSize(9.5);
      const titleLines = doc.splitTextToSize(`${r.id}  ${r.title}`, CONTENT_W - 110) as string[];
      doc.text(r.status, M + CONTENT_W, y, { align: "right" });
      for (const line of titleLines) {
        ensure(12);
        doc.text(line, M, y);
        y += 12;
      }
      doc.setFont("helvetica", "normal");
      setColor(MUTED);
      write(`${r.provisions} · weight ${r.weight} · ${r.points} · ${r.inForce}`, M + 8, CONTENT_W - 8, 8, 10);
      setColor(INK);
      if (r.timingNote) {
        setColor(AMBER);
        write(r.timingNote, M + 8, CONTENT_W - 8, 8, 10);
        setColor(INK);
      }
      for (const o of r.observations) write(o, M + 8, CONTENT_W - 8, 8.5, 10.5);
      setColor(MUTED);
      write(`${r.checkType}. Checks: ${r.description}`, M + 8, CONTENT_W - 8, 7.5, 9.5);
      write(`Applies when: ${r.applicability}`, M + 8, CONTENT_W - 8, 7.5, 9.5);
      write(`Assessment criteria: ${r.criteria}`, M + 8, CONTENT_W - 8, 7.5, 9.5);
      write(`Evidence expected: ${r.expectedEvidence}`, M + 8, CONTENT_W - 8, 7.5, 9.5);
      if (r.pages.length) write(`Pages: ${r.pages.join(", ")}`, M + 8, CONTENT_W - 8, 7.5, 9.5);
      if (r.sufficiency) write(r.sufficiency, M + 8, CONTENT_W - 8, 7.5, 9.5);
      if (r.limitation) write(r.limitation, M + 8, CONTENT_W - 8, 7.5, 9.5);
      write(`Confidence: ${r.confidence}`, M + 8, CONTENT_W - 8, 7.5, 9.5);
      write(`Scoring rationale: ${r.scoringRationale}`, M + 8, CONTENT_W - 8, 7.5, 9.5);
      setColor(INK);
      if (r.note) {
        setColor(MUTED);
        write(r.note, M + 8, CONTENT_W - 8, 8, 10);
        setColor(INK);
      }
      if (r.rules.length || r.evidenceIds.length) {
        setColor(MUTED);
        const ev = r.evidenceIds.length ? `Evidence: ${r.evidenceIds.slice(0, 6).join(", ")}${r.evidenceIds.length > 6 ? ` and ${r.evidenceIds.length - 6} more` : ""}.` : "";
        write(`${r.rules.length ? `Related rules: ${r.rules.join(", ")}. ` : ""}${ev}`, M + 8, CONTENT_W - 8, 7.5, 9.5);
        setColor(INK);
      }
      y += 5;
    }
  };

  newPage();
  doc.setFont("helvetica", "bold");
  doc.setFontSize(26);
  setColor(INK);
  doc.text(v.headline, M, y + 8);
  y += 22;
  doc.setFont("helvetica", "normal");
  write(v.coverageLine, M, CONTENT_W, 9, 12);
  setColor(MUTED);
  for (const l of v.scopeLines) write(l, M, CONTENT_W, 8.5, 11);
  write(v.scanTimeLine, M, CONTENT_W, 8.5, 11);
  write(v.versionsLine, M, CONTENT_W, 8.5, 11);
  setColor(INK);
  setColor(AMBER);
  write(v.natureNote, M, CONTENT_W, 8.5, 11);
  if (v.caution) write(v.caution, M, CONTENT_W, 8.5, 11);
  setColor(INK);
  write(v.basisLine, M, CONTENT_W, 9, 12);
  write(v.inForceLine, M, CONTENT_W, 9, 12);
  write(v.formulaLine, M, CONTENT_W, 8.5, 11);
  write(v.methodology.versionLine, M, CONTENT_W, 8.5, 11);
  write(v.methodologyLine, M, CONTENT_W, 8.5, 11);
  if (v.methodology.custom) {
    setColor(AMBER);
    write(v.methodology.weightsNote, M, CONTENT_W, 8.5, 11);
    setColor(INK);
  }
  if (v.withheldReason) {
    setColor(RISK_COLORS.medium);
    write(`A score is not shown because the evidence is not sufficient: ${v.withheldReason} Missing or untested evidence is never counted as compliance.`, M, CONTENT_W, 9, 12);
    setColor(INK);
  }
  if (v.unableLine) write(v.unableLine, M, CONTENT_W, 9, 12);

  heading("What the scan found");
  write(v.breakdownLine, M, CONTENT_W, 9, 12);
  write(v.statusCountsLine, M, CONTENT_W, 9, 12);
  write(v.checkTypesLine, M, CONTENT_W, 8.5, 11);
  ensure(24);
  doc.setFont("helvetica", "bold");
  doc.setFontSize(9);
  doc.text("Technical signals, counted apart", M, y);
  doc.setFont("helvetica", "normal");
  y += 12;
  for (const l of v.evidenceLines) write(`• ${l}`, M + 4, CONTENT_W - 4, 8.5, 10.5);
  setColor(MUTED);
  write(v.evidenceNote, M, CONTENT_W, 8, 10.5);
  setColor(INK);

  // ---- the four finding groups, in order. Needs Human Review is the amber "Logic Required / Human Review" box. ----
  write(v.groupCountsLine, M, CONTENT_W, 9, 12);
  for (const line of v.upcomingLines) {
    setColor(MUTED);
    write(line, M, CONTENT_W, 8.5, 11);
    setColor(INK);
  }
  const entry = (f: (typeof v.groups)[number]["entries"][number], amber: boolean) => {
    ensure(54);
    const top = y;
    doc.setFont("helvetica", "bold");
    write(`${f.id.startsWith("DPDP-") ? `${f.id}  ` : ""}${f.title}`, M + 10, CONTENT_W - 12, 9.5, 11.5);
    doc.setFont("helvetica", "normal");
    setColor(amber ? AMBER : MUTED);
    write(`${f.outcome} · ${f.severity}${f.timingNote ? ` · ${f.timingNote}` : ""}`, M + 10, CONTENT_W - 12, 8, 10);
    write(f.severityNote, M + 10, CONTENT_W - 12, 7.5, 9.5);
    setColor(INK);
    if (f.legal) write(`Legal reference: ${f.legal}`, M + 10, CONTENT_W - 12, 8.5, 10.5);
    write(`Affected page: ${f.pages.length ? f.pages.join(", ") : "none recorded"} · Consent state: ${f.consentState}`, M + 10, CONTENT_W - 12, 8.5, 10.5);
    if (f.observed.length) {
      doc.setFont("helvetica", "bold");
      write("What was observed", M + 10, CONTENT_W - 12, 8, 10);
      doc.setFont("helvetica", "normal");
      for (const o of f.observed) write(`• ${o}`, M + 14, CONTENT_W - 16, 8.5, 10.5);
    }
    if (f.rules.length || f.evidenceIds.length) {
      setColor(MUTED);
      const ids = f.evidenceIds.length ? `Evidence: ${f.evidenceIds.slice(0, 6).join(", ")}${f.evidenceIds.length > 6 ? ` and ${f.evidenceIds.length - 6} more` : ""}.` : "";
      write(`${f.rules.length ? `Related rules: ${f.rules.join(", ")}. ` : ""}${ids}`, M + 10, CONTENT_W - 12, 7.5, 9.5);
      setColor(INK);
    }
    if (f.caveat) {
      setColor(MUTED);
      write(f.caveat, M + 10, CONTENT_W - 12, 7.5, 9.5);
      setColor(INK);
    }
    if (f.businessImpact) write(`Business impact: ${f.businessImpact}`, M + 10, CONTENT_W - 12, 8.5, 10.5);
    if (f.explanation) write(`Why this group: ${f.explanation}`, M + 10, CONTENT_W - 12, 8.5, 10.5);
    if (f.whatToReview.length) write(`What must be reviewed: ${f.whatToReview.join(" ")}`, M + 10, CONTENT_W - 12, 8.5, 10.5);
    if (f.missing.length) {
      doc.setFont("helvetica", "bold");
      write("What is missing", M + 10, CONTENT_W - 12, 8, 10);
      doc.setFont("helvetica", "normal");
      for (const m of f.missing) write(`• ${m}`, M + 14, CONTENT_W - 16, 8.5, 10.5);
    }
    if (f.remediation) write(`Recommended remediation: ${f.remediation}`, M + 10, CONTENT_W - 12, 8.5, 10.5);
    if (f.howToVerify) write(`How to verify: ${f.howToVerify}`, M + 10, CONTENT_W - 12, 8.5, 10.5);
    if (f.related) {
      setColor(MUTED);
      write(`Related: ${f.related}`, M + 10, CONTENT_W - 12, 7.5, 9.5);
      setColor(INK);
    }
    const c = amber ? AMBER_RULE : RULE;
    doc.setDrawColor(c.r, c.g, c.b);
    doc.setLineWidth(2);
    doc.line(M + 2, top - 9, M + 2, Math.max(top - 9, y - 6));
    y += 6;
  };
  for (const g of v.groups) {
    const amber = g.key === "needs_review";
    ensure(amber ? 60 : 40);
    y += 8;
    if (amber) {
      // The "Logic Required / Human Review" box: a shaded band, the statement, then an amber bar down each item.
      doc.setFillColor(AMBER_FILL.r, AMBER_FILL.g, AMBER_FILL.b);
      doc.rect(M - 6, y - 12, CONTENT_W + 12, 20, "F");
      doc.setFillColor(AMBER.r, AMBER.g, AMBER.b);
      doc.rect(M - 6, y - 12, 4, 20, "F");
      doc.setFont("helvetica", "bold");
      doc.setFontSize(10.5);
      setColor(AMBER);
      doc.text(`${v.reviewTitle.toUpperCase()}: ${g.title.toUpperCase()} (${g.count})`, M + 4, y + 2);
      setColor(INK);
      doc.setFont("helvetica", "normal");
      y += 20;
      write(v.reviewStatement, M, CONTENT_W, 8.5, 11);
    } else {
      doc.setFont("helvetica", "bold");
      doc.setFontSize(10.5);
      doc.text(`${g.title} (${g.count})`, M, y);
      doc.setFont("helvetica", "normal");
      y += 13;
    }
    setColor(MUTED);
    write(`${g.intro} ${g.note}`, M, CONTENT_W, 8, 10.5);
    setColor(INK);
    if (g.entries.length === 0) write("None.", M, CONTENT_W, 8.5, 11);
    for (const f of g.entries) entry(f, amber);
  }

  heading("Why this result");
  for (const l of v.explanationLines) write(`• ${l}`, M + 4, CONTENT_W - 4, 8.5, 10.5);

  heading("By category");
  for (const c of v.categories) {
    ensure(14);
    doc.setFontSize(9);
    doc.setFont("helvetica", "bold");
    doc.text(c.name, M, y);
    doc.setFont("helvetica", "normal");
    doc.text(c.score, M + 190, y);
    doc.text(c.coverage, M + 270, y);
    doc.text(c.weights, M + 360, y);
    y += 13;
  }

  heading(`Assessed requirements (${v.assessed.length})`);
  rows(v.assessed);
  if (v.unableToVerify.length) {
    heading(`Unable to verify (${v.unableToVerify.length}): not in the score`);
    rows(v.unableToVerify);
  }
  if (v.excluded.length) {
    heading(`Excluded from the score (${v.excluded.length})`);
    rows(v.excluded);
  }
  heading(`Prioritized remediation (${v.remediationPlan.length})`);
  setColor(MUTED);
  write(v.remediationNote, M, CONTENT_W, 8, 10.5);
  setColor(INK);
  if (v.remediationPlan.length === 0) write("No remediation actions: no check failed with sufficient evidence.", M, CONTENT_W, 8.5, 11);
  for (const p of v.remediationPlan) {
    ensure(40);
    doc.setFont("helvetica", "bold");
    write(`${p.rank}. ${p.title} · ${p.priority}`, M, CONTENT_W, 9.5, 11.5);
    doc.setFont("helvetica", "normal");
    setColor(MUTED);
    write(`${p.outcome} · ${p.timing} · ${p.priorityReason}`, M + 10, CONTENT_W - 10, 7.5, 9.5);
    setColor(INK);
    write(`Action: ${p.action}`, M + 10, CONTENT_W - 10, 8.5, 10.5);
    write(`How to verify: ${p.howToVerify}`, M + 10, CONTENT_W - 10, 8.5, 10.5);
    setColor(MUTED);
    write(p.legal, M + 10, CONTENT_W - 10, 7.5, 9.5);
    setColor(INK);
    y += 4;
  }

  heading(v.methodology.title);
  if (!v.methodology.custom) write(v.methodology.weightsNote, M, CONTENT_W, 8.5, 10.5);
  for (const l of v.methodology.rules) write(`• ${l}`, M + 4, CONTENT_W - 4, 8.5, 10.5);
  ensure(24);
  y += 4;
  doc.setFont("helvetica", "bold");
  doc.setFontSize(9);
  doc.text("Category weights", M, y);
  doc.setFont("helvetica", "normal");
  y += 12;
  for (const c of v.methodology.categories) {
    write(`${c.name}: weight ${c.weight} (${c.share} of the applicable weight). ${c.rationale}`, M + 4, CONTENT_W - 4, 8.5, 10.5);
  }
  ensure(24);
  y += 4;
  doc.setFont("helvetica", "bold");
  doc.setFontSize(9);
  doc.text("Requirement weights", M, y);
  doc.setFont("helvetica", "normal");
  y += 12;
  for (const w of v.methodology.weights) {
    write(`${w.id}  ${w.title} · ${w.type} · weight ${w.weight} · ${w.applies}`, M + 4, CONTENT_W - 4, 8.5, 10.5);
    setColor(MUTED);
    write(`Why this weight: ${w.rationale}`, M + 12, CONTENT_W - 12, 7.5, 9.5);
    write(`Applies when: ${w.applicability}`, M + 12, CONTENT_W - 12, 7.5, 9.5);
    setColor(INK);
  }
  ensure(24);
  y += 4;
  doc.setFont("helvetica", "bold");
  doc.setFontSize(9);
  doc.text("Legal mapping", M, y);
  doc.setFont("helvetica", "normal");
  y += 12;
  write(v.versionsLine, M + 4, CONTENT_W - 4, 8.5, 10.5);
  for (const l of v.legalSources) write(`• ${l}`, M + 4, CONTENT_W - 4, 8.5, 10.5);
  ensure(24);
  y += 4;
  doc.setFont("helvetica", "bold");
  doc.setFontSize(9);
  doc.text("Duplicate rules", M, y);
  doc.setFont("helvetica", "normal");
  y += 12;
  for (const l of v.methodology.dedup) write(`• ${l}`, M + 4, CONTENT_W - 4, 8.5, 10.5);

  heading("Not assessed by this score");
  for (const o of v.outOfScope) write(o, M, CONTENT_W, 8.5, 11);
  y += 4;
  setColor(MUTED);
  write(v.disclaimer, M, CONTENT_W, 8, 10.5);
  setColor(INK);
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
