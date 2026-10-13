import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DsrCase } from "../api/dsr";
import { DsrEmailFirst } from "./DsrEmailFirst";

const BASE_URL = "http://127.0.0.1:8000";

function jsonResponse(body: unknown, status = 200) {
  return { status, ok: status < 400, headers: { get: () => "application/json" }, json: async () => body };
}

function makeCase(overrides: Partial<DsrCase> = {}): DsrCase {
  return {
    id: "case-1",
    reference: "DSR-0001",
    status: "received",
    request_type: "access",
    classification_method: "rules",
    classification_confidence: 0.9,
    raw_request: "Self-service data request.",
    requester_email: "person@example.com",
    requester_phone: null,
    requester_reference: null,
    error_code: null,
    error_detail: null,
    identity_status: null,
    allowed_transitions: ["identity_pending", "classified"],
    is_terminal: false,
    sla: {
      received_at: "2026-01-01T00:00:00+00:00",
      due_at: "2026-02-01T00:00:00+00:00",
      remaining_seconds: 600_000,
      overdue: false,
      breached: false,
      escalated: false,
      closed: false,
    },
    created_at: "2026-01-01T00:00:00+00:00",
    closed_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  sessionStorage.setItem("consiva.access_token", "test-token");
});

afterEach(() => {
  sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

/** Routes every fetch call this flow needs, reading `caseStatus()` fresh on each
 *  GET of the case itself so a test can move the backend's state between poll
 *  ticks exactly the way the real server would. */
function routeFetch(caseStatus: () => string) {
  return vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";

    if (url.endsWith("/api/v1/dsr/requests") && method === "POST") {
      return Promise.resolve(jsonResponse(makeCase({ status: "received" }), 201));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/identity/verify") && method === "POST") {
      const verified = makeCase({ status: "identity_verified", identity_status: "manually_verified" });
      return Promise.resolve(
        jsonResponse({ status: "manually_verified", satisfied: true, attempts: 0, case: verified }),
      );
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/search") && method === "POST") {
      const searching = makeCase({ status: "searching", identity_status: "manually_verified" });
      return Promise.resolve(jsonResponse({ job_id: "job-1", case: searching }));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1") && method === "GET") {
      return Promise.resolve(
        jsonResponse(
          makeCase({
            status: caseStatus(),
            identity_status: "manually_verified",
            error_code: caseStatus() === "failed" ? "SOURCE_NOT_AUTHORIZED" : null,
            error_detail: caseStatus() === "failed" ? "no source is authorized for DSR" : null,
          }),
        ),
      );
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/results")) {
      return Promise.resolve(jsonResponse({ search_runs: [], evidence: [] }));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/plan")) {
      return Promise.resolve(jsonResponse({ plan: null, actions: [], decisions: null }));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/executions")) {
      return Promise.resolve(jsonResponse([]));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/response")) {
      return Promise.resolve(jsonResponse(null));
    }
    throw new Error(`unexpected fetch: ${url} ${method}`);
  });
}

async function driveToDataStep(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(window, "prompt").mockReturnValue("Checked a government ID over a video call.");

  render(<DsrEmailFirst />);

  await userEvent.type(screen.getByLabelText("Email address"), "person@example.com");
  await userEvent.click(screen.getByRole("button", { name: "Find my data" }));

  await waitFor(() => expect(screen.getByRole("button", { name: "Verify manually" })).toBeInTheDocument());
  await userEvent.click(screen.getByRole("button", { name: "Verify manually" }));

  await waitFor(() =>
    expect(screen.getByRole("heading", { name: "Searching connected systems…" })).toBeInTheDocument(),
  );
}

describe("DsrEmailFirst: evidence messaging reflects the actual search outcome", () => {
  it("a failed search shows a distinct failure message and a working retry button, never 'no record matched'", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "searching";
    const fetchMock = routeFetch(() => status);
    await driveToDataStep(fetchMock);

    // The backend's search fails between poll ticks.
    status = "failed";
    await vi.advanceTimersByTimeAsync(2000);

    await waitFor(() =>
      expect(
        screen.getByText(/The search did not complete -- it failed with SOURCE_NOT_AUTHORIZED/),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByText("No record matched that email in the systems we searched. Nothing will be changed."),
    ).not.toBeInTheDocument();

    const retryButton = screen.getByRole("button", { name: "Retry search" });
    expect(retryButton).toBeEnabled();

    const searchCallsBefore = fetchMock.mock.calls.filter(
      ([url, init]) => url === `${BASE_URL}/api/v1/dsr/requests/case-1/search` && init?.method === "POST",
    ).length;
    expect(searchCallsBefore).toBe(1);

    status = "searching";
    await userEvent.click(retryButton);
    await waitFor(() => {
      const after = fetchMock.mock.calls.filter(
        ([url, init]) => url === `${BASE_URL}/api/v1/dsr/requests/case-1/search` && init?.method === "POST",
      ).length;
      expect(after).toBe(searchCallsBefore + 1);
    });
  });

  it("a genuine zero-match completion shows the plain 'no record matched' message, not the failure wording", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "searching";
    const fetchMock = routeFetch(() => status);
    await driveToDataStep(fetchMock);

    status = "search_completed";
    await vi.advanceTimersByTimeAsync(2000);

    await waitFor(() =>
      expect(
        screen.getByText("No record matched that email in the systems we searched. Nothing will be changed."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/did not complete/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry search" })).not.toBeInTheDocument();
  });
});
