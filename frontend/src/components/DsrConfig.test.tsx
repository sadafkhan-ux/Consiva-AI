import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DsrRetentionRule, DsrSourceAuthorization } from "../api/dsr";
import type { RopaDataSource } from "../api/ropa";
import { DsrConfig } from "./DsrConfig";

const BASE_URL = "http://127.0.0.1:8000";

function jsonResponse(body: unknown, status = 200) {
  return { status, ok: status < 400, headers: { get: () => "application/json" }, json: async () => body };
}

function makeSource(overrides: Partial<RopaDataSource> = {}): RopaDataSource {
  return {
    id: "source-1",
    name: "prepmyevent-production",
    connector: "postgres",
    source_type: "database",
    config: { host: "db.internal" },
    credential_ref: null,
    has_stored_credential: true,
    enabled: true,
    last_verified_at: null,
    ...overrides,
  };
}

function makeAuthorization(overrides: Partial<DsrSourceAuthorization> = {}): DsrSourceAuthorization {
  return {
    id: "auth-1",
    data_source_id: "source-1",
    source_name: "prepmyevent-production",
    searchable_tables: ["users", "orders"],
    identity_tables: ["users"],
    identifier_columns: { users: { email: "email" } },
    returnable_columns: { users: ["id", "email"] },
    record_key_columns: { users: ["id"] },
    erasable_columns: { users: ["email"] },
    allow_execution: false,
    write_credential_configured: false,
    enabled: true,
    ...overrides,
  };
}

function makeRule(overrides: Partial<DsrRetentionRule> = {}): DsrRetentionRule {
  return {
    id: "rule-1",
    table_name: "invoices",
    date_column: "created_at",
    retention_days: 2190,
    authority: "Income Tax Act, 1961 s.44AA",
    applies_to_operations: ["delete_record"],
    data_source_id: null,
    scope: "organisation",
    notes: null,
    enabled: true,
    ...overrides,
  };
}

function configFetchMock({
  sources = [makeSource()],
  authorizations = [makeAuthorization()],
  rules = [makeRule()],
}: {
  sources?: RopaDataSource[];
  authorizations?: DsrSourceAuthorization[];
  rules?: DsrRetentionRule[];
} = {}) {
  return vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/api/v1/ropa/sources")) return Promise.resolve(jsonResponse(sources));
    if (url.endsWith("/api/v1/dsr/config/sources") && (!init || init.method === undefined)) {
      return Promise.resolve(jsonResponse(authorizations));
    }
    if (url.endsWith("/api/v1/dsr/config/retention") && (!init || init.method === undefined)) {
      return Promise.resolve(jsonResponse(rules));
    }
    if (url.endsWith("/api/v1/dsr/config/sources") && init?.method === "PUT") {
      return Promise.resolve(jsonResponse({ id: "auth-1" }));
    }
    if (url.endsWith("/api/v1/dsr/config/retention") && init?.method === "PUT") {
      return Promise.resolve(jsonResponse({ id: "rule-2" }));
    }
    throw new Error(`unexpected fetch: ${url} ${init?.method ?? "GET"}`);
  });
}

beforeEach(() => {
  sessionStorage.setItem("consiva.access_token", "test-token");
});

afterEach(() => {
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe("DsrConfig: loading and rendering", () => {
  it("shows a loading state before configuration resolves", async () => {
    let resolveSources: (v: unknown) => void = () => {};
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/ropa/sources")) {
        return new Promise((resolve) => {
          resolveSources = resolve;
        });
      }
      return Promise.resolve(jsonResponse([]));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConfig isAdmin={false} />);
    expect(screen.getByText("Loading configuration…")).toBeInTheDocument();

    resolveSources(jsonResponse([]));
    await waitFor(() => expect(screen.queryByText("Loading configuration…")).not.toBeInTheDocument());
  });

  it("renders real source authorization and retention rule data from the backend, not placeholders", async () => {
    const fetchMock = configFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConfig isAdmin={false} />);

    await waitFor(() => expect(screen.getByText("prepmyevent-production")).toBeInTheDocument());
    expect(screen.getByText("users, orders")).toBeInTheDocument();
    expect(screen.getByText("read only")).toBeInTheDocument();
    expect(screen.getByText("invoices")).toBeInTheDocument();
    expect(screen.getByText("2190 days")).toBeInTheDocument();
    expect(screen.getByText("Income Tax Act, 1961 s.44AA")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(`${BASE_URL}/api/v1/dsr/config/sources`, expect.anything());
    expect(fetchMock).toHaveBeenCalledWith(`${BASE_URL}/api/v1/dsr/config/retention`, expect.anything());
  });

  it("shows empty states when no authorizations or retention rules exist", async () => {
    const fetchMock = configFetchMock({ authorizations: [], rules: [] });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConfig isAdmin={false} />);

    await waitFor(() =>
      expect(screen.getByText("No source has a DSR authorization configured yet.")).toBeInTheDocument(),
    );
    expect(
      screen.getByText("No retention rules configured yet — erasures proceed unblocked by one."),
    ).toBeInTheDocument();
  });

  it("surfaces a load failure as an error banner rather than crashing", async () => {
    const fetchMock = vi.fn(() => Promise.reject(new Error("boom")));
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConfig isAdmin={false} />);

    await waitFor(() => expect(screen.getByText(/Network error/)).toBeInTheDocument());
  });
});

describe("DsrConfig: non-admin view", () => {
  it("shows the admin-required banner and hides edit controls for a non-admin viewer", async () => {
    const fetchMock = configFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConfig isAdmin={false} />);

    await waitFor(() => expect(screen.getByText("prepmyevent-production")).toBeInTheDocument());
    expect(
      screen.getByText(/changing it requires an\s*administrator\./),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "+ Add rule" })).not.toBeInTheDocument();
  });
});

describe("DsrConfig: admin edits", () => {
  it("editing a source authorization saves through the real PUT endpoint", async () => {
    const fetchMock = configFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConfig isAdmin />);
    await waitFor(() => expect(screen.getByRole("cell", { name: "prepmyevent-production" })).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByText("Edit authorization")).toBeInTheDocument();
    expect(screen.getByDisplayValue("users, orders")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Save authorization" }));

    await waitFor(() =>
      expect(screen.getByText("Authorization saved for prepmyevent-production.")).toBeInTheDocument(),
    );
    const putCall = fetchMock.mock.calls.find(
      ([url, init]) => url === `${BASE_URL}/api/v1/dsr/config/sources` && init?.method === "PUT",
    );
    expect(putCall).toBeTruthy();
    const body = JSON.parse((putCall![1] as RequestInit).body as string);
    expect(body).toMatchObject({
      data_source_id: "source-1",
      searchable_tables: ["users", "orders"],
      identity_tables: ["users"],
      allow_execution: false,
    });
  });

  it("adding a retention rule saves through the real PUT endpoint", async () => {
    const fetchMock = configFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConfig isAdmin />);
    await waitFor(() => expect(screen.getByText("invoices")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "+ Add rule" }));
    await userEvent.type(screen.getByPlaceholderText("invoices"), "support_tickets");
    await userEvent.clear(screen.getByPlaceholderText("created_at"));
    await userEvent.type(screen.getByPlaceholderText("created_at"), "opened_at");
    await userEvent.type(
      screen.getByPlaceholderText("e.g. Income Tax Act, 1961 s.44AA — 6 years"),
      "Consumer Protection Act — 2 years",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save rule" }));

    await waitFor(() =>
      expect(screen.getByText("Retention rule for support_tickets saved.")).toBeInTheDocument(),
    );
    const putCall = fetchMock.mock.calls.find(
      ([url, init]) => url === `${BASE_URL}/api/v1/dsr/config/retention` && init?.method === "PUT",
    );
    expect(putCall).toBeTruthy();
    const body = JSON.parse((putCall![1] as RequestInit).body as string);
    expect(body).toMatchObject({
      table_name: "support_tickets",
      date_column: "opened_at",
      authority: "Consumer Protection Act — 2 years",
      applies_to_operations: ["delete_record"],
    });
  });

  it("disabling a retention rule calls the real DELETE endpoint", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/v1/ropa/sources")) return Promise.resolve(jsonResponse([makeSource()]));
      if (url.endsWith("/api/v1/dsr/config/sources")) return Promise.resolve(jsonResponse([makeAuthorization()]));
      if (url.endsWith("/api/v1/dsr/config/retention") && (!init || init.method === undefined)) {
        return Promise.resolve(jsonResponse([makeRule()]));
      }
      if (url.endsWith("/api/v1/dsr/config/retention/rule-1") && init?.method === "DELETE") {
        return Promise.resolve(jsonResponse({ id: "rule-1", enabled: false }));
      }
      throw new Error(`unexpected fetch: ${url} ${init?.method ?? "GET"}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrConfig isAdmin />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Disable" })).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Disable" }));

    await waitFor(() =>
      expect(screen.getByText("Rule for invoices disabled.")).toBeInTheDocument(),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE_URL}/api/v1/dsr/config/retention/rule-1`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});
