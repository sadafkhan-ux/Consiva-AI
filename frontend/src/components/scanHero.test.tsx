import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DpdpScoreResponse, ScanScreenshot } from "../api/types";
import type { ConsentScanState } from "../hooks/useConsentScan";
import fixtures from "../test/dpdpScoreFixtures.json";
import { buildHeroView } from "../report/hero";
import { buildScoreView } from "../report/dpdpScore";
import { ScanHero } from "./ScanHero";

const F = fixtures as unknown as Record<string, DpdpScoreResponse>;
const score = buildScoreView(F.mixed);
const B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/";
const shot = (over: Partial<ScanScreenshot> = {}): ScanScreenshot => ({
  scan_id: "11111111-1111-1111-1111-111111111111", domain: "swaransoft.com", status: "ok", available: true, message: null, page_url: "https://swaransoft.com/",
  mime: "image/jpeg", width: 1280, height: 720, captured_at: "2026-10-22T16:43:00Z", data: B64, ...over,
});
const none = (message: string, status = "blocked"): ScanScreenshot => shot({ status, available: false, data: null, mime: null, width: null, height: null, message });

describe("the hero on the dashboard", () => {
  it("shows the domain directly above the homepage picture, and the picture itself", () => {
    render(<ScanHero score={score} siteUrl="https://swaransoft.com/" screenshot={shot()} />);
    const figure = screen.getByTestId("hero-preview");
    const children = Array.from(figure.children).map((c) => c.getAttribute("data-testid"));
    expect(children.indexOf("hero-domain")).toBeLessThan(children.indexOf("hero-screenshot"));           // the domain comes first, above the picture
    expect(screen.getByTestId("hero-domain")).toHaveTextContent("swaransoft.com");
    const img = screen.getByTestId("hero-screenshot") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe(`data:image/jpeg;base64,${B64}`);
    expect(img.alt).toBe("Homepage of swaransoft.com as the scanner saw it");
    expect(img).toHaveAttribute("width", "1280");
    expect(screen.getByTestId("hero-caption")).toHaveTextContent("Homepage as scanned · 22 Oct 2026, 16:43 UTC");
  });

  it("puts the score ring first, in the same row as the picture", () => {
    render(<ScanHero score={score} siteUrl="https://swaransoft.com/" screenshot={shot()} />);
    const hero = screen.getByTestId("scan-hero");
    const order = Array.from(hero.children).map((c) => c.getAttribute("data-testid") ?? c.className);
    expect(order[0]).toBe("hero-score");                                  // the score is the first thing: top left
    expect(order[order.length - 1]).toBe("hero-preview");                  // the picture is the last: top right
    expect(screen.getByTestId("hero-percent")).toHaveTextContent("56.5%");
    expect(within(screen.getByTestId("hero-score")).getByText("Compliance Score")).toBeInTheDocument();
  });

  it("reads exactly the words the PDF reads: band, headline and coverage", () => {
    render(<ScanHero score={score} siteUrl="https://swaransoft.com/" screenshot={shot()} />);
    const h = buildHeroView(score);
    expect(screen.getByTestId("hero-band")).toHaveTextContent(h.band);
    expect(screen.getByTestId("hero-headline")).toHaveTextContent(h.headline);
    expect(screen.getByTestId("hero-coverage")).toHaveTextContent(h.coverageLine ?? "");
  });

  it("names the domain once, and never repeats the address of a homepage", () => {
    const { container } = render(<ScanHero score={score} siteUrl="https://swaransoft.com/" screenshot={shot()} />);
    expect((container.textContent ?? "").split("swaransoft.com").length - 1).toBe(1);
    expect(screen.queryByTestId("hero-address")).not.toBeInTheDocument();
  });

  it("shows the address once, under the picture, when the scan was of a deeper page", () => {
    render(<ScanHero score={score} siteUrl="https://swaransoft.com/pricing?plan=pro" screenshot={shot()} />);
    expect(screen.getByTestId("hero-address")).toHaveTextContent("https://swaransoft.com/pricing?plan=pro");
  });

  it.each([
    ["blocked", "The site did not show its homepage to the scanner (it answered with an error or a security check), so no preview is shown."],
    ["timeout", "The homepage preview could not be taken in time."],
    ["not_captured", "No homepage preview was taken for this scan: it was scanned before previews were added."],
  ])("%s: says so in a clean panel, with no picture, and still names the domain", (status, message) => {
    render(<ScanHero score={score} siteUrl="https://swaransoft.com/" screenshot={none(message, status)} />);
    expect(screen.queryByTestId("hero-screenshot")).not.toBeInTheDocument();
    expect(screen.getByTestId("hero-preview-missing")).toHaveTextContent("Homepage preview not available");
    expect(screen.getByTestId("hero-preview-missing")).toHaveTextContent(message);
    expect(screen.getByTestId("hero-domain")).toHaveTextContent("swaransoft.com");
    expect(screen.getByTestId("hero-percent")).toHaveTextContent("56.5%");        // the score is untouched
  });

  it("uses a plain default when the preview could not be loaded at all", () => {
    render(<ScanHero score={score} siteUrl="https://swaransoft.com/" screenshot={null} />);
    expect(screen.getByTestId("hero-preview-missing")).toHaveTextContent("A homepage preview is not available for this scan.");
  });

  it("says there is no score, and shows no number, when the score is withheld or missing", () => {
    const { unmount } = render(<ScanHero score={null} siteUrl="https://swaransoft.com/" screenshot={shot()} />);
    expect(screen.getByTestId("hero-percent")).toHaveTextContent("No score");
    expect(screen.queryByTestId("hero-ring-arc")).not.toBeInTheDocument();
    unmount();
    render(<ScanHero score={buildScoreView(F.insufficient)} siteUrl="https://swaransoft.com/" screenshot={shot()} />);
    expect(screen.getByTestId("hero-percent")).not.toHaveTextContent("%");
  });

  it("draws the arc to the score: the full ring at 100%", () => {
    render(<ScanHero score={buildScoreView(F.thin)} siteUrl="https://x.test/" screenshot={null} />);
    const arc = screen.getByTestId("hero-ring-arc");
    const [filled, total] = (arc.getAttribute("stroke-dasharray") ?? "").split(" ").map(Number);
    expect(filled / total).toBeCloseTo(1, 2);
  });

  it("refuses a picture that belongs to a different scan, and says no preview is available", () => {
    render(<ScanHero score={score} siteUrl="https://swaransoft.com/" screenshot={shot()} scanId="22222222-2222-2222-2222-222222222222" />);
    expect(screen.queryByTestId("hero-screenshot")).not.toBeInTheDocument();
    expect(screen.getByTestId("hero-preview-missing")).toBeInTheDocument();
  });

  it("shows the picture when it belongs to the scan being shown", () => {
    render(<ScanHero score={score} siteUrl="https://swaransoft.com/" screenshot={shot()} scanId="11111111-1111-1111-1111-111111111111" />);
    expect(screen.getByTestId("hero-screenshot")).toBeInTheDocument();
  });

  it("spells the domain as the scan was entered, www included", () => {
    render(<ScanHero score={score} siteUrl="https://www.swaransoft.com/" screenshot={shot()} />);
    expect(screen.getByTestId("hero-domain")).toHaveTextContent("www.swaransoft.com");
  });

  it("is absent before there is a scanned address", () => {
    const { container } = render(<ScanHero score={score} siteUrl="" screenshot={shot()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("never prints a path, an error or a code in the hero", () => {
    render(<ScanHero score={score} siteUrl="https://swaransoft.com/" screenshot={none("A preview of the homepage could not be taken for this scan.", "failed")} />);
    expect(screen.getByTestId("scan-hero").textContent).not.toMatch(/Traceback|Exception|C:\\|\/home\/|capture_|playwright/i);
  });
});

// ── on the page, with the hook's state ────────────────────────────────────────────────────────────────────────────────────────
let current: Partial<ConsentScanState> = {};
vi.mock("../hooks/useConsentScan", async (orig) => {
  const real = await orig<typeof import("../hooks/useConsentScan")>();
  const base = { phase: "idle", scanId: null, scan: null, stages: [], websiteScanMeta: null, findings: [], audit: [], evidence: null, dpdpScore: null, policyAnalysis: null,
    screenshot: null, error: null, failedStage: null, startedAt: null, completedAt: null, durationMs: null };
  return { ...real, useConsentScan: () => ({ state: { ...base, ...current }, run: vi.fn(), open: vi.fn(), afterReviewDecision: vi.fn() }) };
});
vi.mock("../api/client", () => ({ api: { listScans: vi.fn().mockResolvedValue([]) } }));

import { ConsentAgentView } from "../ConsentAgentView";

describe("the page", () => {
  const done = (over: Partial<ConsentScanState> = {}) => ({
    phase: "completed", scanId: "s1", findings: [], startedAt: 1, completedAt: 2, dpdpScore: F.mixed,
    scan: { id: "s1", url: "https://swaransoft.com/", domain: "swaransoft.com", status: "completed", error: null, evidence_counts: { pages: 5, cookies: 0, trackers: 0, third_party_services: 0, policies: 0, forms: 0 } },
    ...over,
  }) as Partial<ConsentScanState>;
  beforeEach(() => { current = {}; });

  it("does not show another scan's picture on this scan's page", () => {
    current = done({ screenshot: shot({ scan_id: "99999999-9999-9999-9999-999999999999" }) });   // scanId s1 is not that scan
    render(<ConsentAgentView />);
    expect(screen.queryByTestId("hero-screenshot")).not.toBeInTheDocument();
    expect(screen.getByTestId("hero-preview-missing")).toBeInTheDocument();
  });

  it("shows the hero with the scan's own homepage picture once the scan has its results", () => {
    current = done({ screenshot: shot(), scanId: "11111111-1111-1111-1111-111111111111" });   // the picture belongs to this scan
    render(<ConsentAgentView />);
    expect(screen.getByTestId("hero-screenshot")).toBeInTheDocument();
    expect(screen.getByTestId("hero-domain")).toHaveTextContent("swaransoft.com");
  });

  it("shows the plain panel for a scan whose preview is missing, and the rest of the page is unchanged", () => {
    current = done({ screenshot: none("A preview of the homepage could not be taken for this scan.", "failed") });
    render(<ConsentAgentView />);
    expect(screen.getByTestId("hero-preview-missing")).toBeInTheDocument();
    expect(screen.getByTestId("dpdp-score")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Download Report \(PDF\)/ })).toBeEnabled();
  });

  it("shows no hero before any scan", () => {
    render(<ConsentAgentView />);
    expect(screen.queryByTestId("scan-hero")).not.toBeInTheDocument();
  });
});

describe("the figure inside the ring", () => {
  const withPercent = (percent: number) => {
    const s = JSON.parse(JSON.stringify(F.mixed)) as DpdpScoreResponse;
    s.readiness.score_percent = percent;
    return buildScoreView(s);
  };
  const text = () => screen.getByTestId("hero-percent");

  it.each([[100, "100%", 22], [64, "64%", 22], [45.5, "45.5%", 18], [76.2, "76.2%", 18]])("%s shows %s at size %i, the figure the server sent", (value, label, size) => {
    render(<ScanHero score={withPercent(value)} siteUrl="https://x.test/" screenshot={undefined} />);
    expect(text().textContent).toBe(label);
    expect(text().getAttribute("font-size")).toBe(String(size));
    expect(text().getAttribute("text-anchor")).toBe("middle");
    expect(text().getAttribute("x")).toBe("55");                                              // the ring's centre, across
  });

  it("puts the baseline half a capital's height below the ring's centre, at every size, so the figure is centred down as well", () => {
    for (const value of [100, 45.5]) {
      const { unmount } = render(<ScanHero score={withPercent(value)} siteUrl="https://x.test/" screenshot={undefined} />);
      const size = Number(text().getAttribute("font-size"));
      expect(Number(text().getAttribute("y"))).toBeCloseTo(55 + size * 0.36, 5);
      unmount();
    }
  });

  it("keeps the widest figure inside the coloured ring with room to spare", () => {
    // Measured in Chromium: "45.5%" in the bold face is 3.09 times its font size wide (67.9 ring units at size 22, which touched the ring's edge).
    // The ring's inner diameter is 2 x (44 - 4.5) = 79 units. At size 18 the same figure is about 55.6 units wide: more than 10 units clear on each side.
    const widest = 3.09 * 18;
    expect(widest).toBeLessThan(79 - 20);
  });

  it("says No score in the same ring, centred, when the score is withheld", () => {
    const s = JSON.parse(JSON.stringify(F.insufficient)) as DpdpScoreResponse;
    render(<ScanHero score={buildScoreView(s)} siteUrl="https://x.test/" screenshot={undefined} />);
    expect(text().textContent).toBe("No score");
    expect(text().getAttribute("font-size")).toBe("13");
    expect(Number(text().getAttribute("y"))).toBeCloseTo(55 + 13 * 0.36, 5);
    expect(screen.queryByTestId("hero-ring-arc")).not.toBeInTheDocument();
  });
});
