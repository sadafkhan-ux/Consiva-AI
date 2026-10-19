import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DsrCase } from "../api/dsr";
import { DsrConsole } from "./DsrConsole";
import { dsrHash, parseDsrHash, agentFromHash } from "./dsrRoute";

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
  window.location.hash = "";
});

afterEach(() => {
  sessionStorage.clear();
  vi.unstubAllGlobals();
  window.location.hash = "";
});

describe("DsrConsole: tab wiring", () => {
  it("defaults to the New request tab and renders DsrEmailFirst's entry screen", () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse([]))));

    render(<DsrConsole />);

    expect(screen.getByRole("tab", { name: "New request" })).toHaveClass("active");
    expect(screen.getByRole("heading", { name: "Find my data" })).toBeInTheDocument();
  });

  it("switching to All cases loads cases from the real endpoint and renders them", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/dsr/requests")) return Promise.resolve(jsonResponse([makeCase()]));
      if (url.endsWith("/api/v1/dsr/config/sources")) return Promise.resolve(jsonResponse([{ id: "a", enabled: true }]));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConsole />);
    await userEvent.click(screen.getByRole("tab", { name: "All cases" }));

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
    await userEvent.click(screen.getByRole("tab", { name: "Configuration" }));

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
    await userEvent.click(screen.getByRole("tab", { name: "Configuration" }));

    await waitFor(() => expect(screen.getByRole("cell", { name: "acme" })).toBeInTheDocument());
    expect(screen.queryByText(/changing it requires an\s*administrator\./)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });
});

describe("DsrConsole: accessibility", () => {
  it("the sections are a tablist: the selected tab is marked and the panel is labelled by it", () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse([]))));
    render(<DsrConsole />);

    const tablist = screen.getByRole("tablist", { name: "DSR sections" });
    expect(tablist).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "New request", selected: true })).toHaveAttribute("aria-controls", "dsr-panel");
    expect(screen.getByRole("tab", { name: "All cases", selected: false })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "dsr-tab-new");
  });

  it("the arrow keys move between tabs", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      if (url.endsWith("/api/v1/dsr/requests")) return Promise.resolve(jsonResponse([]));
      return Promise.resolve(jsonResponse([]));
    }));
    render(<DsrConsole />);

    screen.getByRole("tab", { name: "New request" }).focus();
    await userEvent.keyboard("{ArrowRight}");

    expect(screen.getByRole("tab", { name: "All cases", selected: true })).toHaveFocus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "New request", selected: true })).toHaveFocus();
  });
});

describe("DsrConsole: reload keeps the section and the case", () => {
  it("opens the section and case named in the address", async () => {
    window.location.hash = dsrHash("cases", "case-1");
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/dsr/requests")) return Promise.resolve(jsonResponse([makeCase()]));
      if (url.endsWith("/api/v1/dsr/requests/case-1")) return Promise.resolve(jsonResponse(makeCase()));
      if (url.endsWith("/api/v1/dsr/requests/case-1/results")) return Promise.resolve(jsonResponse({ search_runs: [], evidence: [] }));
      if (url.endsWith("/api/v1/dsr/config/sources")) return Promise.resolve(jsonResponse([{ id: "a", enabled: true }]));
      if (url.endsWith("/response")) return Promise.resolve(jsonResponse(null));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConsole />);

    expect(screen.getByRole("tab", { name: "All cases", selected: true })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "DSR-0001" })).toBeInTheDocument();
  });

  it("the address follows the tab and the open case", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/dsr/requests")) return Promise.resolve(jsonResponse([makeCase()]));
      if (url.endsWith("/api/v1/dsr/requests/case-1")) return Promise.resolve(jsonResponse(makeCase()));
      if (url.endsWith("/api/v1/dsr/requests/case-1/results")) return Promise.resolve(jsonResponse({ search_runs: [], evidence: [] }));
      if (url.endsWith("/api/v1/dsr/config/sources")) return Promise.resolve(jsonResponse([{ id: "a", enabled: true }]));
      if (url.endsWith("/response")) return Promise.resolve(jsonResponse(null));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<DsrConsole />);

    await userEvent.click(screen.getByRole("tab", { name: "All cases" }));
    expect(window.location.hash).toBe("#/dsr/cases");

    await userEvent.click(await screen.findByRole("button", { name: /DSR-0001/ }));
    await waitFor(() => expect(window.location.hash).toBe("#/dsr/cases/case-1"));
  });

  it("a pasted link to a section is followed", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse([]))));
    render(<DsrConsole />);

    window.location.hash = "#/dsr/config";
    window.dispatchEvent(new HashChangeEvent("hashchange"));

    await waitFor(() => expect(screen.getByRole("tab", { name: "Configuration", selected: true })).toBeInTheDocument());
  });

  it("the Configuration screen can open ROPA to add a source", async () => {
    sessionStorage.setItem(
      "consiva.profile",
      JSON.stringify({ user_id: "u1", org_id: "org1", role: "admin", email: "admin@example.com" }),
    );
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/ropa/sources")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/v1/dsr/config/sources")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/v1/dsr/config/retention")) return Promise.resolve(jsonResponse([]));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onOpenRopa = vi.fn();
    render(<DsrConsole onOpenRopa={onOpenRopa} />);

    await userEvent.click(screen.getByRole("tab", { name: "Configuration" }));
    await userEvent.click(await screen.findByRole("button", { name: "Open the ROPA agent" }));

    expect(onOpenRopa).toHaveBeenCalled();
    // The empty retention list is a warning, not plain muted text.
    expect(screen.getByText("No retention rules configured yet — erasures proceed unblocked by one.").closest('[role="status"]')).not.toBeNull();
  });
});

describe("dsrRoute", () => {
  it("parses and builds DSR addresses", () => {
    expect(parseDsrHash("#/dsr")).toEqual({ view: "new", caseId: null });
    expect(parseDsrHash("#/dsr/cases")).toEqual({ view: "cases", caseId: null });
    expect(parseDsrHash("#/dsr/cases/3f2a-91")).toEqual({ view: "cases", caseId: "3f2a-91" });
    expect(parseDsrHash("#/dsr/new/abc")).toEqual({ view: "new", caseId: "abc" });
    expect(dsrHash("config", null)).toBe("#/dsr/config");
    expect(dsrHash("cases", "abc")).toBe("#/dsr/cases/abc");
  });

  it("refuses anything that is not a screen name and a plain id", () => {
    expect(parseDsrHash("#/dsr/cases/<script>")).toBeNull();
    expect(parseDsrHash("#/dsr/cases/a/b")).toBeNull();
    expect(parseDsrHash("#/dsr/unknown")).toBeNull();
    expect(parseDsrHash("#/ropa")).toBeNull();
    expect(parseDsrHash("#/dsr/" + "x".repeat(80))).toBeNull();
  });

  it("recognises the agent in the address, and only a known one", () => {
    expect(agentFromHash("#/dsr/cases/abc")).toBe("dsr");
    expect(agentFromHash("#/ropa")).toBe("ropa");
    expect(agentFromHash("#/nonsense")).toBeNull();
    expect(agentFromHash("")).toBeNull();
  });
});

describe("DsrConsole: New request interaction", () => {
  it("submitting an email on the New request tab calls the real create-case endpoint and advances the step", async () => {
    const CREATED = makeCase({ id: "case-9", reference: "DSR-0009", request_type: "other" });
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/v1/dsr/config/sources")) return Promise.resolve(jsonResponse([{ id: "a", enabled: true }]));
      if (url.includes("/api/v1/dsr/requests/active")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/v1/dsr/requests") && init?.method === "POST") {
        return Promise.resolve(jsonResponse(CREATED, 201));
      }
      if (url.endsWith("/api/v1/dsr/requests/case-9/classify") && init?.method === "POST") {
        return Promise.resolve(jsonResponse({ ...CREATED, request_type: "access" }));
      }
      throw new Error(`unexpected fetch: ${url} ${init?.method ?? "GET"}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConsole />);
    await userEvent.type(screen.getByLabelText("Email address"), "person@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Find my data" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: "Verify the requester" })).toBeInTheDocument());
    const create = fetchMock.mock.calls.find(
      ([url, init]) => url === `${BASE_URL}/api/v1/dsr/requests` && init?.method === "POST",
    )!;
    expect(JSON.parse(create[1]!.body as string)).toMatchObject({ requester_email: "person@example.com" });
    await waitFor(() => expect(window.location.hash).toBe("#/dsr/new/case-9"));
  });
});
