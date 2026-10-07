import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RopaRun } from "../api/ropa";
import { RopaDashboard } from "./RopaDashboard";

const BASE_URL = "http://127.0.0.1:8000";

const SOURCE = {
  id: "source-1",
  name: "prepmyevent-production",
  connector: "postgres",
  source_type: "database",
  config: { host: "db.internal", dbname: "app", user: "ro" },
  credential_ref: null,
  has_stored_credential: true,
  enabled: true,
  last_verified_at: null,
};

function runFixture(overrides: Partial<typeof PENDING_RUN> = {}) {
  return { ...PENDING_RUN, ...overrides };
}

// Typed explicitly as RopaRun (the real API contract, api/ropa.ts) rather than
// left to inference: an inferred literal `null` for started_at/completed_at
// narrows runFixture's `Partial<typeof PENDING_RUN>` overrides to `null |
// undefined`, rejecting the real string timestamps the observability tests
// below need to override them with (TS2322) -- even though RopaRun itself
// already types both fields as `string | null`.
const PENDING_RUN: RopaRun = {
  id: "run-1",
  data_source_id: "source-1",
  source_name: "prepmyevent-production",
  ingest_mode: "connector",
  status: "pending",
  tables_scanned: 0,
  columns_scanned: 0,
  personal_data_elements: 0,
  overall_confidence: null,
  summary: {},
  error: null,
  created_at: "2026-01-01T00:00:00+00:00",
  started_at: null,
  completed_at: null,
};

function jsonResponse(body: unknown, status = 200) {
  return { status, ok: status < 400, headers: { get: () => "application/json" }, json: async () => body };
}

beforeEach(() => {
  sessionStorage.setItem("consiva.access_token", "test-token");
});

afterEach(() => {
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe("RopaDashboard: Sources wiring", () => {
  it("loads connectors, sources and runs from the real endpoints on mount", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/ropa/connectors")) return Promise.resolve(jsonResponse({ connectors: ["postgres"] }));
      if (url.endsWith("/api/v1/ropa/sources")) return Promise.resolve(jsonResponse([SOURCE]));
      if (url.endsWith("/api/v1/ropa/runs")) return Promise.resolve(jsonResponse([]));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<RopaDashboard />);

    await waitFor(() => expect(screen.getByText("prepmyevent-production")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(`${BASE_URL}/api/v1/ropa/connectors`, expect.anything());
    expect(fetchMock).toHaveBeenCalledWith(`${BASE_URL}/api/v1/ropa/sources`, expect.anything());
    expect(fetchMock).toHaveBeenCalledWith(`${BASE_URL}/api/v1/ropa/runs`, expect.anything());
  });

  it("starting Discover calls the real endpoint, disables the button immediately, and surfaces the queued run", async () => {
    let runsCallCount = 0;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/v1/ropa/connectors")) return Promise.resolve(jsonResponse({ connectors: ["postgres"] }));
      if (url.endsWith("/api/v1/ropa/sources")) return Promise.resolve(jsonResponse([SOURCE]));
      if (url.endsWith("/api/v1/ropa/sources/source-1/discover") && init?.method === "POST") {
        return Promise.resolve(jsonResponse(runFixture()));
      }
      if (url.endsWith("/api/v1/ropa/runs")) {
        runsCallCount += 1;
        // First call (initial mount): no runs yet. Every call after Discover
        // was clicked: the queued run now exists, same as a real backend
        // would report once POST /discover has created the row.
        return Promise.resolve(jsonResponse(runsCallCount === 1 ? [] : [runFixture()]));
      }
      if (url.match(/\/api\/v1\/ropa\/runs\/run-1\/(records|findings|changes)$/)) {
        return Promise.resolve(jsonResponse([]));
      }
      throw new Error(`unexpected fetch: ${url} ${init?.method ?? "GET"}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<RopaDashboard />);
    await waitFor(() => expect(screen.getByText("prepmyevent-production")).toBeInTheDocument());

    const discoverButton = screen.getByRole("button", { name: "Discover" });
    await userEvent.click(discoverButton);

    // Disabled the instant the click handler runs, before the POST even
    // resolves -- this is the client-side half of duplicate-submission
    // prevention (the backend's 409 is the other half, tested in
    // test_ropa_end_to_end.py::test_discover_source_queues_a_job...).
    expect(screen.getByRole("button", { name: /Discovering/ })).toBeDisabled();

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE_URL}/api/v1/ropa/sources/source-1/discover`,
      expect.objectContaining({ method: "POST" })
    );

    // The queued run (status: pending) becomes the selected/visible run.
    await waitFor(() => expect(screen.getAllByText("pending").length).toBeGreaterThan(0));
  });
});

describe("RopaDashboard: run observability stats", () => {
  it("shows duration computed from started/completed, plus findings and changes counts", async () => {
    const COMPLETED_RUN = runFixture({
      status: "completed",
      started_at: "2026-01-01T00:00:00+00:00",
      completed_at: "2026-01-01T00:00:05+00:00",
      summary: { risk_findings: 2, detected_changes: 1 },
    });
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/ropa/connectors")) return Promise.resolve(jsonResponse({ connectors: ["postgres"] }));
      if (url.endsWith("/api/v1/ropa/sources")) return Promise.resolve(jsonResponse([SOURCE]));
      if (url.endsWith("/api/v1/ropa/runs")) return Promise.resolve(jsonResponse([COMPLETED_RUN]));
      if (url.endsWith("/api/v1/ropa/runs/run-1/records")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/v1/ropa/runs/run-1/findings")) {
        return Promise.resolve(jsonResponse([
          { id: "f1", finding: "a", category: "x", gap_status: "open", severity: "high", severity_factors: [], related_evidence: [], confidence: 0.9, recommendation: "", status: "pending" },
          { id: "f2", finding: "b", category: "x", gap_status: "open", severity: "low", severity_factors: [], related_evidence: [], confidence: 0.5, recommendation: "", status: "pending" },
        ]));
      }
      if (url.endsWith("/api/v1/ropa/runs/run-1/changes")) {
        return Promise.resolve(jsonResponse([
          { id: "c1", change_type: "column_added", target: "t", previous_value: null, current_value: "v", is_material: false, review_required: false },
        ]));
      }
      if (url.endsWith("/api/v1/ropa/runs/run-1/classifications")) return Promise.resolve(jsonResponse([]));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<RopaDashboard />);

    await waitFor(() => expect(screen.getByText("5.0s")).toBeInTheDocument());
    expect(screen.getByText("Duration")).toBeInTheDocument();

    await waitFor(() => expect(screen.getByText("Findings").previousSibling).toHaveTextContent("2"));
    expect(screen.getByText("Changes").previousSibling).toHaveTextContent("1");
  });

  it("shows 'running…' for duration while a run has no completed_at yet", async () => {
    const IN_PROGRESS_RUN = runFixture({
      status: "discovering",
      started_at: "2026-01-01T00:00:00+00:00",
      completed_at: null,
    });
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/ropa/connectors")) return Promise.resolve(jsonResponse({ connectors: ["postgres"] }));
      if (url.endsWith("/api/v1/ropa/sources")) return Promise.resolve(jsonResponse([SOURCE]));
      if (url.endsWith("/api/v1/ropa/runs")) return Promise.resolve(jsonResponse([IN_PROGRESS_RUN]));
      if (url.match(/\/api\/v1\/ropa\/runs\/run-1\/(records|findings|changes|classifications)$/)) {
        return Promise.resolve(jsonResponse([]));
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<RopaDashboard />);

    await waitFor(() => expect(screen.getByText("running…")).toBeInTheDocument());
  });
});
