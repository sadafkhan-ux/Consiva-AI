import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ConsentScanState } from "../hooks/useConsentScan";
import type { ScanStatusResponse } from "../api/types";
import { ScanStatusSection } from "./ScanStatusSection";

const scan = (status: string, pages = 0): ScanStatusResponse =>
  ({ id: "s1", url: "https://example.com/", status, error: null, evidence_counts: { pages, forms: 0, cookies: 0, trackers: 0, third_party_services: 0, policies: 0 } }) as ScanStatusResponse;

function makeState(overrides: Partial<ConsentScanState> = {}): ConsentScanState {
  return {
    phase: "scanning", scanId: "s1", scan: scan("running"), stages: [], websiteScanMeta: null, findings: [], audit: [], evidence: null,
    error: null, failedStage: null, startedAt: Date.now(), completedAt: null, durationMs: null, ...overrides,
  };
}

describe("the Stop Scan button", () => {
  it.each([
    ["queued", makeState({ phase: "scanning", scan: scan("queued") })],
    ["running", makeState({ phase: "scanning", scan: scan("running") })],
    ["analysing", makeState({ phase: "analyzing", scan: scan("running") })],
    ["just created", makeState({ phase: "scanning", scan: null })],
  ])("is shown while the scan is %s", (_name, state) => {
    render(<ScanStatusSection state={state} onStop={() => undefined} />);
    expect(screen.getByRole("button", { name: "Stop Scan" })).toBeEnabled();
  });

  it.each([
    ["completed", makeState({ phase: "completed", scan: scan("completed") })],
    ["failed", makeState({ phase: "failed", scan: scan("failed") })],
    ["awaiting review", makeState({ phase: "awaiting_review", scan: scan("completed") })],
    ["cancelled", makeState({ phase: "cancelled", scan: scan("cancelled") })],
    ["a crawl that already completed", makeState({ phase: "analyzing", scan: scan("completed") })],
  ])("is not shown once the scan is %s", (_name, state) => {
    render(<ScanStatusSection state={state} onStop={() => undefined} />);
    expect(screen.queryByRole("button", { name: /Stop Scan|Stopping/ })).toBeNull();
  });

  it("calls the stop action on click", async () => {
    const onStop = vi.fn();
    render(<ScanStatusSection state={makeState()} onStop={onStop} />);
    await userEvent.click(screen.getByRole("button", { name: "Stop Scan" }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("is disabled and says Stopping... while the stop is in progress", async () => {
    const onStop = vi.fn();
    render(<ScanStatusSection state={makeState({ stopping: true })} onStop={onStop} />);
    const button = screen.getByRole("button", { name: "Stopping..." });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(onStop).not.toHaveBeenCalled();
  });

  it("states the limits: not instant, and a running analysis step finishes and is discarded", () => {
    render(<ScanStatusSection state={makeState()} onStop={() => undefined} />);
    const copy = screen.getByTestId("stop-scan");
    expect(copy).toHaveTextContent(/few seconds, not an instant/);
    expect(copy).toHaveTextContent(/already running finishes and its result is discarded/);
  });

  it("shows no button when the page gives no stop action", () => {
    render(<ScanStatusSection state={makeState()} />);
    expect(screen.queryByRole("button", { name: /Stop Scan/ })).toBeNull();
  });

  it("shows the short message when the scan could not be stopped", () => {
    render(<ScanStatusSection state={makeState({ stopNote: "The scan could not be stopped (500: boom). It is still running." })} onStop={() => undefined} />);
    expect(screen.getByTestId("stop-note")).toHaveTextContent("It is still running.");
    expect(screen.getByRole("button", { name: "Stop Scan" })).toBeEnabled();
  });
});

describe("a stopped scan", () => {
  it("says Scan stopped, offers Scan again, and never presents partial evidence as a finished result", async () => {
    const onScanAgain = vi.fn();
    render(<ScanStatusSection state={makeState({ phase: "cancelled", scan: scan("cancelled", 3), completedAt: Date.now() })} onScanAgain={onScanAgain} />);
    expect(screen.getByText("Scan stopped")).toBeInTheDocument();
    const note = screen.getByTestId("scan-stopped");
    expect(note).toHaveTextContent("no result or score");
    expect(note).toHaveTextContent("Partial evidence was collected (3 pages so far)");
    expect(note).toHaveTextContent("not a finished result");
    await userEvent.click(screen.getByRole("button", { name: "Scan again" }));
    expect(onScanAgain).toHaveBeenCalledTimes(1);
  });

  it("says nothing was collected when there is no evidence", () => {
    render(<ScanStatusSection state={makeState({ phase: "cancelled", scan: scan("cancelled", 0) })} onScanAgain={() => undefined} />);
    expect(screen.getByTestId("scan-stopped")).toHaveTextContent("Nothing was collected yet.");
  });
});
