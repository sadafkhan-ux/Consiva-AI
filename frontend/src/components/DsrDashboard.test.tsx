import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DsrCase, DsrEvidence, DsrSearchRun } from "../api/dsr";
import { DsrDashboard } from "./DsrDashboard";

const BASE_URL = "http://127.0.0.1:8000";

function jsonResponse(body: unknown, status = 200) {
  return { status, ok: status < 400, headers: { get: () => "application/json" }, json: async () => body };
}

function makeCase(overrides: Partial<DsrCase> = {}): DsrCase {
  return {
    id: "case-1",
    reference: "DSR-0001",
    status: "search_completed",
    request_type: "erasure",
    classification_method: "llm",
    classification_confidence: 0.92,
    raw_request: "Please delete all my personal information.",
    requester_email: "person@example.com",
    requester_phone: null,
    requester_reference: null,
    error_code: null,
    error_detail: null,
    identity_status: "verified",
    allowed_transitions: ["searching"],
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
});

describe("DsrDashboard: case list", () => {
  it("loads cases from the real endpoint on mount and renders real fields, not placeholders", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/dsr/requests")) return Promise.resolve(jsonResponse([makeCase()]));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrDashboard />);

    await waitFor(() => expect(screen.getByText("DSR-0001")).toBeInTheDocument());
    expect(screen.getByText("erasure")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(`${BASE_URL}/api/v1/dsr/requests`, expect.anything());
  });

  it("shows an empty state and no case rows when there are no cases", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/dsr/requests")) return Promise.resolve(jsonResponse([]));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrDashboard />);

    await waitFor(() => expect(screen.getByText("No DSR cases yet.")).toBeInTheDocument());
    expect(screen.getByText("Cases (0)")).toBeInTheDocument();
  });

  it("surfaces a load failure as an error banner rather than crashing", async () => {
    const fetchMock = vi.fn(() => Promise.reject(new Error("boom")));
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrDashboard />);

    await waitFor(() => expect(screen.getByText(/Network error/)).toBeInTheDocument());
  });
});

describe("DsrDashboard: case detail", () => {
  it("selecting a case loads its detail and shows evidence from the real endpoints", async () => {
    const CASE = makeCase();
    const RUN: DsrSearchRun = {
      id: "run-1",
      source: "prepmyevent-production",
      status: "completed",
      matches: 1,
      distinct_subjects: 1,
      tables_searched: ["users"],
      error_code: null,
      error_detail: null,
      completed_at: "2026-01-01T00:05:00+00:00",
    };
    const EVIDENCE: DsrEvidence = {
      id: "ev-1",
      source: "prepmyevent-production",
      table: "users",
      matched_column: "email",
      identifier_kind: "email",
      match_type: "exact",
      confidence: 0.99,
      record_reference: { id: 42 },
      record_snapshot: { email: "person@example.com" },
      ropa_category: "Contact Data",
      observed_at: "2026-01-01T00:05:00+00:00",
    };

    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/dsr/requests")) return Promise.resolve(jsonResponse([CASE]));
      if (url.endsWith(`/api/v1/dsr/requests/${CASE.id}`)) return Promise.resolve(jsonResponse(CASE));
      if (url.endsWith(`/api/v1/dsr/requests/${CASE.id}/results`)) {
        return Promise.resolve(jsonResponse({ search_runs: [RUN], evidence: [EVIDENCE] }));
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrDashboard />);
    await waitFor(() => expect(screen.getByText("DSR-0001")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /DSR-0001/ }));

    await waitFor(() => expect(screen.getByText("Evidence (1)")).toBeInTheDocument());
    expect(screen.getByText("prepmyevent-production/users")).toBeInTheDocument();
    expect(screen.getByText(JSON.stringify(EVIDENCE.record_snapshot))).toBeInTheDocument();
  });

  it("issuing an identity challenge calls the real endpoint and shows it via prompt", async () => {
    const CASE = makeCase({ identity_status: null });
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue(null);

    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/v1/dsr/requests")) return Promise.resolve(jsonResponse([CASE]));
      if (url.endsWith(`/api/v1/dsr/requests/${CASE.id}`)) return Promise.resolve(jsonResponse(CASE));
      if (url.endsWith(`/api/v1/dsr/requests/${CASE.id}/results`)) {
        return Promise.resolve(jsonResponse({ search_runs: [], evidence: [] }));
      }
      if (url.endsWith(`/api/v1/dsr/requests/${CASE.id}/identity/challenge`) && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse({
            verification_id: "chal-1",
            status: "pending",
            expires_at: "2026-01-01T01:00:00+00:00",
            challenge: "123456",
            delivery_note: "Read this code to the requester.",
          }),
        );
      }
      throw new Error(`unexpected fetch: ${url} ${init?.method ?? "GET"}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrDashboard />);
    await waitFor(() => expect(screen.getByText("DSR-0001")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /DSR-0001/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Issue identity challenge" })).toBeEnabled());

    await userEvent.click(screen.getByRole("button", { name: "Issue identity challenge" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE_URL}/api/v1/dsr/requests/${CASE.id}/identity/challenge`,
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(promptSpy).toHaveBeenCalledWith("Read this code to the requester.", "123456");
    await waitFor(() => expect(screen.getByText("Identity challenge issued.")).toBeInTheDocument());
  });
});

describe("DsrDashboard: new case intake", () => {
  it("creates a case through the intake form, posting the real payload and showing a success notice", async () => {
    const CREATED = makeCase({ id: "case-2", reference: "DSR-0042", request_type: "access" });

    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/v1/dsr/requests") && (!init || init.method === undefined)) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith("/api/v1/dsr/requests") && init?.method === "POST") {
        return Promise.resolve(jsonResponse(CREATED, 201));
      }
      if (url.endsWith(`/api/v1/dsr/requests/${CREATED.id}`)) return Promise.resolve(jsonResponse(CREATED));
      if (url.endsWith(`/api/v1/dsr/requests/${CREATED.id}/results`)) {
        return Promise.resolve(jsonResponse({ search_runs: [], evidence: [] }));
      }
      throw new Error(`unexpected fetch: ${url} ${init?.method ?? "GET"}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrDashboard />);
    await waitFor(() => expect(screen.getByText("No DSR cases yet.")).toBeInTheDocument());

    await userEvent.type(
      screen.getByPlaceholderText("e.g. Please delete all my personal information."),
      "Please tell me what data you hold on me.",
    );
    await userEvent.type(screen.getByPlaceholderText("person@example.com"), "person@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Create case" }));

    await waitFor(() =>
      expect(
        screen.getByText('Case DSR-0042 created and classified as "access".'),
      ).toBeInTheDocument(),
    );
    const [createUrl, createInit] = fetchMock.mock.calls.find(
      ([url, init]) => url === `${BASE_URL}/api/v1/dsr/requests` && init?.method === "POST",
    )!;
    expect(createUrl).toBe(`${BASE_URL}/api/v1/dsr/requests`);
    expect(JSON.parse(createInit!.body as string)).toEqual({
      raw_request: "Please tell me what data you hold on me.",
      requester_email: "person@example.com",
    });
  });
});
