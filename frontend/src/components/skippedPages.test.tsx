import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ScanStatusSection } from "./ScanStatusSection";

const state = (m: unknown) => ({
  phase: "completed", scanId: "abc12345", scan: null, stages: [], startedAt: null, completedAt: null, durationMs: null,
  websiteScanMeta: m, error: null, failedStage: null,
}) as never;

describe("pages that were not scanned (main-domain pages only)", () => {
  it("lists each exclusion with its reason, and never other domains", () => {
    render(<ScanStatusSection state={state({
      page_limit: 5, stop_reason: "page_limit", pages_scanned: 5,
      skipped_by_reason: { page_limit: 6, robots_disallowed: 2 },
      skipped_pages: [{ url: "https://x.test/blog", reason: "page_limit" }, { url: "https://x.test/admin", reason: "robots_disallowed" }],
    })} />);

    const block = screen.getByTestId("skipped-pages");
    expect(block).toHaveTextContent("Pages not scanned (8)");
    expect(block).toHaveTextContent("https://x.test/blog — left unscanned: page limit reached");
    expect(block).toHaveTextContent("https://x.test/admin — disallowed by robots.txt");
    expect((block.textContent ?? "").toLowerCase()).not.toContain("subdomain");
  });

  it("shows nothing when nothing was skipped", () => {
    render(<ScanStatusSection state={state({ page_limit: 5, skipped_by_reason: {} })} />);
    expect(screen.queryByTestId("skipped-pages")).not.toBeInTheDocument();
  });
});
