import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DpdpScoreResponse, SectorAssessment } from "../api/types";
import fixtures from "../test/dpdpScoreFixtures.json";
import { DpdpScoreSection, SectorSection } from "./DpdpScoreSection";

const mixed = (fixtures as unknown as Record<string, DpdpScoreResponse>).mixed;
const SECTOR: SectorAssessment = {
  sector: "ecommerce", label: "E-commerce", confidence: "high", summary: "This looks like an e-commerce website (high confidence), from 9 signal(s) in the scan.",
  signals: [{ sector: "ecommerce", source: "path", match: "checkout", seen_in: "/checkout", points: 3 }], also_possible: ["Finance and insurance"],
  notes: [{ id: "ecommerce-erasure", title: "Erasure after inactivity for large e-commerce entities", provisions: ["DPDP Rules r.8(1), r.8(2), Third Schedule item 1"],
    text: "An e-commerce entity with at least two crore registered users in India must erase personal data three years after last use.", observed: ["Payment services seen: Razorpay."],
    action: "Declare whether the entity meets the Third Schedule threshold (DPDP-X8)." }],
  declarations: [{ flag: "erasure_class", requirement_id: "DPDP-X8", label: "E-commerce entity with at least two crore registered users in India", why: "A website scan cannot count registered users.", declared: null }],
  limitations: ["The sector is inferred, so it is an indication, not a finding."],
};

describe("the sector and applicability card", () => {
  it("shows the sector, its confidence, the provisions, what was observed, and what to declare", () => {
    render(<SectorSection sector={SECTOR} />);
    expect(screen.getByTestId("sector-summary")).toHaveTextContent("E-commerce");
    expect(screen.getByTestId("sector-summary")).toHaveTextContent("high confidence");
    expect(screen.getByTestId("sector-summary")).toHaveTextContent("Also possible: Finance and insurance");
    expect(screen.getByTestId("sector-signals")).toHaveTextContent('"checkout" (path)');
    expect(screen.getByTestId("sector-note")).toHaveTextContent("Third Schedule item 1");
    expect(screen.getByTestId("sector-note")).toHaveTextContent("Observed: Payment services seen: Razorpay.");
    expect(screen.getByTestId("sector-declarations")).toHaveTextContent("DPDP-X8");
    expect(screen.getByTestId("sector-declarations")).toHaveTextContent("not declared");
    expect(screen.getByTestId("sector-limits")).toHaveTextContent("indication, not a finding");
  });

  it("is on the score card when the server sent one, and absent for an older server", () => {
    const { unmount } = render(<DpdpScoreSection score={{ ...mixed, sector: SECTOR }} />);
    expect(screen.getByTestId("sector-assessment")).toBeInTheDocument();
    unmount();
    render(<DpdpScoreSection score={mixed} />);
    expect(screen.queryByTestId("sector-assessment")).not.toBeInTheDocument();
  });

  it("says plainly when the sector was declared by the organisation, and what the scan made of the site", () => {
    render(<SectorSection sector={{ ...SECTOR, sector: "government_vendor", label: "Government vendor", confidence: "declared", declared: true, detected_sector: "it", agrees_with_scan: false,
      summary: "Declared by the organisation as government vendor; the scan's own reading was it and software (medium confidence).", also_possible: ["IT and software (including SaaS)"] }} />);
    const summary = screen.getByTestId("sector-summary");
    expect(summary).toHaveTextContent("Government vendor");
    expect(summary).toHaveTextContent("(declared)");
    expect(summary).not.toHaveTextContent("declared confidence");
    expect(summary).toHaveTextContent("the scan's own reading was it and software");
  });

  it("does not change the score shown", () => {
    const a = render(<DpdpScoreSection score={{ ...mixed, sector: SECTOR }} />);
    const withSector = screen.getByTestId("dpdp-headline").textContent;
    a.unmount();
    render(<DpdpScoreSection score={mixed} />);
    expect(screen.getByTestId("dpdp-headline").textContent).toBe(withSector);
  });
});
