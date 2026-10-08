import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DsrCase } from "../api/dsr";
import { DsrConsole } from "./DsrConsole";

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
});

describe("DsrConsole: tab wiring", () => {
  it("defaults to the New request tab and renders DsrEmailFirst's entry screen", () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse([]))));

    render(<DsrConsole />);

    expect(screen.getByRole("button", { name: "New request" })).toHaveClass("active");
    expect(screen.getByRole("heading", { name: "Find my data" })).toBeInTheDocument();
  });

  it("switching to All cases loads cases from the real endpoint and renders them", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/dsr/requests")) return Promise.resolve(jsonResponse([makeCase()]));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConsole />);
    await userEvent.click(screen.getByRole("button", { name: "All cases" }));

    await waitFor(() => expect(screen.getByText("DSR-0001")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(`${BASE_URL}/api/v1/dsr/requests`, expect.anything());
  });

  it("switching to Configuration loads the real config and shows the admin-required banner for a non-admin", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/ropa/sources")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/v1/dsr/config/sources")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/v1/dsr/config/retention")) return Promise.resolve(jsonResponse([]));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConsole />);
    await userEvent.click(screen.getByRole("button", { name: "Configuration" }));

    await waitFor(() =>
      expect(screen.getByText(/changing it requires an\s*administrator\./)).toBeInTheDocument(),
    );
  });

  it("shows admin edit controls in Configuration when the signed-in profile's role is admin", async () => {
    sessionStorage.setItem(
      "consiva.profile",
      JSON.stringify({ user_id: "u1", org_id: "org1", role: "admin", email: "admin@example.com" }),
    );
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/ropa/sources")) {
        return Promise.resolve(jsonResponse([{ id: "source-1", name: "acme", connector: "postgres", source_type: "database", config: {}, credential_ref: null, has_stored_credential: true, enabled: true, last_verified_at: null }]));
      }
      if (url.endsWith("/api/v1/dsr/config/sources")) {
        return Promise.resolve(
          jsonResponse([
            {
              id: "auth-1",
              data_source_id: "source-1",
              source_name: "acme",
              searchable_tables: ["users"],
              identity_tables: ["users"],
              identifier_columns: {},
              returnable_columns: {},
              record_key_columns: {},
              erasable_columns: {},
              allow_execution: false,
              write_credential_configured: false,
              enabled: true,
            },
          ]),
        );
      }
      if (url.endsWith("/api/v1/dsr/config/retention")) return Promise.resolve(jsonResponse([]));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConsole />);
    await userEvent.click(screen.getByRole("button", { name: "Configuration" }));

    await waitFor(() => expect(screen.getByRole("cell", { name: "acme" })).toBeInTheDocument());
    expect(screen.queryByText(/changing it requires an\s*administrator\./)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });
});

describe("DsrConsole: New request interaction", () => {
  it("submitting an email on the New request tab calls the real create-case endpoint and advances the step", async () => {
    const CREATED = makeCase({ id: "case-9", reference: "DSR-0009" });
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/v1/dsr/requests") && init?.method === "POST") {
        return Promise.resolve(jsonResponse(CREATED, 201));
      }
      throw new Error(`unexpected fetch: ${url} ${init?.method ?? "GET"}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConsole />);
    await userEvent.type(screen.getByLabelText("Email address"), "person@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Find my data" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: "Verify the requester" })).toBeInTheDocument());
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE_URL}/api/v1/dsr/requests`);
    expect(JSON.parse(init!.body as string)).toMatchObject({ requester_email: "person@example.com" });
  });
});
