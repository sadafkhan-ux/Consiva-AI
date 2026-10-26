import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConsentSummarySection } from "./ConsentSummarySection";

describe("the consent banner label", () => {
  it.each([
    ["banner", "DETECTED"],
    ["cmp", "DETECTED"],
    ["none", "NOT DETECTED"],
    ["unknown", "UNKNOWN"],
  ])("%s reads %s: an unknown mechanism is not 'detected'", (mechanism, label) => {
    render(<ConsentSummarySection meta={{ consent_mechanism: mechanism }} />);
    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
  });
});
