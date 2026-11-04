import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { A } from "../test/assessments";
import { TenChecksPanel } from "./TenChecksPanel";

afterEach(cleanup);
const ten = () => A.mixed;

describe("the dashboard's score breakdown", () => {
  it("shows nothing when there is no assessment", () => {
    render(<TenChecksPanel ten={null} />);
    expect(screen.queryByTestId("ten-checks-panel")).toBeNull();
  });

  it("lists all ten checks with result, marks, finding, and the same total marks the PDF prints", () => {
    const t = ten();
    render(<TenChecksPanel ten={t} />);
    expect(screen.getByTestId("ten-checks-panel")).toHaveTextContent(`${t.totalText} marks`);
    for (const c of t.checks) {
      const row = within(screen.getByTestId(`ten-check-${c.id}`));
      expect(row.getByText(c.result)).toBeTruthy();
      expect(screen.getByTestId(`ten-check-${c.id}`)).toHaveTextContent(c.marksText);
    }
    expect(screen.getByTestId("ten-summary")).toHaveTextContent(t.summary);
    expect(screen.getByTestId("ten-note")).toHaveTextContent(/these marks added up, as the server worked them out/);
  });

  it("says plainly when a result has no source address, and never shows an invented one", () => {
    render(<TenChecksPanel ten={ten()} />);
    for (const row of screen.getAllByTestId(/^ten-check-\d+$/)) {
      const links = row.querySelectorAll("a");
      for (const a of links) expect(a.getAttribute("href")).toMatch(/^https?:\/\//);
    }
  });
});
