import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RopaDataSource, RopaRun } from "../api/ropa";
import { SourcesPanel } from "./RopaSources";

const BASE_URL = "http://127.0.0.1:8000";

function makeSource(overrides: Partial<RopaDataSource> = {}): RopaDataSource {
  return {
    id: "source-1",
    name: "prepmyevent-production",
    connector: "postgres",
    source_type: "database",
    config: { host: "db.internal", dbname: "app", user: "ro" },
    credential_ref: null,
    has_stored_credential: false,
    enabled: true,
    last_verified_at: null,
    ...overrides,
  };
}

function makeRun(overrides: Partial<RopaRun> = {}): RopaRun {
  return {
    id: "run-1",
    data_source_id: "source-1",
    source_name: "prepmyevent-production",
    ingest_mode: "connector",
    status: "completed",
    tables_scanned: 4,
    columns_scanned: 20,
    personal_data_elements: 6,
    overall_confidence: 0.9,
    summary: {},
    error: null,
    created_at: "2026-01-01T00:00:00+00:00",
    started_at: "2026-01-01T00:00:01+00:00",
    completed_at: "2026-01-01T00:00:05+00:00",
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

describe("SourcesPanel: source list", () => {
  it("shows an empty state and does not render a table when there are no sources", () => {
    render(
      <SourcesPanel
        sources={[]}
        connectors={["postgres"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    expect(screen.getByText(/No data sources yet/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders real source fields from the backend shape, not placeholder data", () => {
    render(
      <SourcesPanel
        sources={[makeSource({ name: "acme-crm", connector: "rest_api" })]}
        connectors={["postgres", "rest_api"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    expect(screen.getByText("acme-crm")).toBeInTheDocument();
    expect(screen.getByText("REST API")).toBeInTheDocument();
    expect(screen.getByText("not set")).toBeInTheDocument(); // no credential configured
    expect(screen.getByText("never")).toBeInTheDocument(); // last_verified_at is null
  });

  it("shows the latest run's status and error for its source, not a hardcoded value", () => {
    render(
      <SourcesPanel
        sources={[makeSource()]}
        connectors={["postgres"]}
        runs={[makeRun({ status: "failed", error: "could not connect to source: timeout" })]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("could not connect to source: timeout")).toBeInTheDocument();
  });

  it("disables Discover while a run for that source is still active, and while locally busy", () => {
    const { rerender } = render(
      <SourcesPanel
        sources={[makeSource()]}
        connectors={["postgres"]}
        runs={[makeRun({ status: "discovering" })]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    expect(screen.getByRole("button", { name: /Discovering/ })).toBeDisabled();

    rerender(
      <SourcesPanel
        sources={[makeSource()]}
        connectors={["postgres"]}
        runs={[makeRun({ status: "completed" })]}
        busySourceIds={new Set(["source-1"])}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    expect(screen.getByRole("button", { name: /Discovering/ })).toBeDisabled();
  });

  it("calls onDiscover with the right source when enabled and idle", async () => {
    const onDiscover = vi.fn();
    render(
      <SourcesPanel
        sources={[makeSource()]}
        connectors={["postgres"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={onDiscover}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "Discover" }));
    expect(onDiscover).toHaveBeenCalledWith(makeSource());
  });

  it("does not offer Discover for a disabled source", () => {
    render(
      <SourcesPanel
        sources={[makeSource({ enabled: false })]}
        connectors={["postgres"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    expect(screen.getByRole("button", { name: "Discover" })).toBeDisabled();
    expect(screen.getByText("disabled")).toBeInTheDocument();
  });
});

describe("SourcesPanel: connector list comes from the backend, never hardcoded", () => {
  it("only offers connectors actually passed in (the real GET /connectors response)", async () => {
    render(
      <SourcesPanel
        sources={[]}
        connectors={["postgres"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "+ Add Data Source" }));
    const select = screen.getByLabelText("Connector");
    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["PostgreSQL"]);
    expect(options).not.toContain("REST API");
  });

  it("offers every connector the backend reports, with both config forms available", async () => {
    render(
      <SourcesPanel
        sources={[]}
        connectors={["postgres", "rest_api"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "+ Add Data Source" }));
    const select = screen.getByLabelText("Connector") as HTMLSelectElement;

    expect(screen.getByLabelText("Host")).toBeInTheDocument(); // postgres fields by default

    fireEvent.change(select, { target: { value: "rest_api" } });
    expect(screen.getByLabelText("Base URL")).toBeInTheDocument();
    expect(screen.queryByLabelText("Host")).not.toBeInTheDocument();
  });
});

describe("SourcesPanel: Test Connection", () => {
  it("POSTs the form's connector/config/secret and shows a success message", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      status: 200,
      headers: { get: () => "application/json" },
      ok: true,
      json: async () => ({ ok: true, message: "Connection succeeded." }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <SourcesPanel
        sources={[]}
        connectors={["postgres"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "+ Add Data Source" }));
    await userEvent.type(screen.getByLabelText("Host"), "db.example.com");
    await userEvent.type(screen.getByLabelText("Database name"), "app");
    await userEvent.type(screen.getByLabelText("Username"), "ro_user");
    await userEvent.type(screen.getByLabelText(/^Password/), "s3cret");

    await userEvent.click(screen.getByRole("button", { name: "Test Connection" }));

    await waitFor(() => expect(screen.getByText(/Connection succeeded\./)).toBeInTheDocument());

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE_URL}/api/v1/ropa/sources/test-connection`);
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({
      connector: "postgres",
      config: { host: "db.example.com", port: 5432, dbname: "app", user: "ro_user", sslmode: "require" },
      secret: "s3cret",
    });
    // Never a plain fetch of the saved row -- this request carries the raw
    // secret ONCE, over the same authenticated channel as everything else,
    // and nothing here writes it anywhere.
    expect(init.headers.Authorization).toBe("Bearer test-token");
  });

  it("shows the backend's failure message when the connection does not work", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      status: 200,
      headers: { get: () => "application/json" },
      ok: true,
      json: async () => ({ ok: false, message: "could not connect to source: timeout" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <SourcesPanel
        sources={[]}
        connectors={["postgres"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "+ Add Data Source" }));
    await userEvent.type(screen.getByLabelText("Host"), "db.example.com");
    await userEvent.type(screen.getByLabelText("Database name"), "app");
    await userEvent.type(screen.getByLabelText("Username"), "ro_user");

    await userEvent.click(screen.getByRole("button", { name: "Test Connection" }));

    await waitFor(() =>
      expect(screen.getByText(/could not connect to source: timeout/)).toBeInTheDocument()
    );
  });

  it("disables Test Connection until the required config fields are filled in", async () => {
    render(
      <SourcesPanel
        sources={[]}
        connectors={["postgres"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "+ Add Data Source" }));
    expect(screen.getByRole("button", { name: "Test Connection" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Host"), "db.example.com");
    await userEvent.type(screen.getByLabelText("Database name"), "app");
    await userEvent.type(screen.getByLabelText("Username"), "ro_user");
    expect(screen.getByRole("button", { name: "Test Connection" })).toBeEnabled();
  });
});

describe("SourcesPanel: Save Data Source", () => {
  it("creates the source, then stores the credential separately, never inline", async () => {
    const createdSource = makeSource({ id: "new-source-id", has_stored_credential: false });
    const fetchMock = vi
      .fn()
      // POST /sources
      .mockResolvedValueOnce({
        status: 201,
        headers: { get: () => "application/json" },
        ok: true,
        json: async () => createdSource,
      })
      // POST /sources/{id}/credential -> 204 No Content
      .mockResolvedValueOnce({ status: 204, headers: { get: () => null }, ok: true });
    vi.stubGlobal("fetch", fetchMock);

    const onSourceCreated = vi.fn();
    render(
      <SourcesPanel
        sources={[]}
        connectors={["postgres"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={onSourceCreated}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "+ Add Data Source" }));
    await userEvent.type(screen.getByLabelText("Source name"), "acme-prod");
    await userEvent.type(screen.getByLabelText("Host"), "db.example.com");
    await userEvent.type(screen.getByLabelText("Database name"), "app");
    await userEvent.type(screen.getByLabelText("Username"), "ro_user");
    await userEvent.type(screen.getByLabelText(/^Password/), "s3cret");

    await userEvent.click(screen.getByRole("button", { name: "Save Data Source" }));

    await waitFor(() => expect(onSourceCreated).toHaveBeenCalledWith(createdSource));
    expect(fetchMock).toHaveBeenCalledTimes(2);

    const [createUrl, createInit] = fetchMock.mock.calls[0];
    expect(createUrl).toBe(`${BASE_URL}/api/v1/ropa/sources`);
    const createBody = JSON.parse(createInit.body as string);
    expect(createBody).toEqual({
      name: "acme-prod",
      connector: "postgres",
      source_type: "database",
      config: { host: "db.example.com", port: 5432, dbname: "app", user: "ro_user", sslmode: "require" },
    });
    // The raw secret is NEVER part of the source-creation body.
    expect(createInit.body as string).not.toContain("s3cret");

    const [credUrl, credInit] = fetchMock.mock.calls[1];
    expect(credUrl).toBe(`${BASE_URL}/api/v1/ropa/sources/new-source-id/credential`);
    expect(JSON.parse(credInit.body as string)).toEqual({ secret: "s3cret" });
  });

  it("does not call the credential endpoint at all when no secret was entered", async () => {
    const createdSource = makeSource({ id: "new-source-id" });
    const fetchMock = vi.fn().mockResolvedValueOnce({
      status: 201,
      headers: { get: () => "application/json" },
      ok: true,
      json: async () => createdSource,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <SourcesPanel
        sources={[]}
        connectors={["postgres"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={() => {}}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={() => {}}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "+ Add Data Source" }));
    await userEvent.type(screen.getByLabelText("Source name"), "acme-prod");
    await userEvent.type(screen.getByLabelText("Host"), "db.example.com");
    await userEvent.type(screen.getByLabelText("Database name"), "app");
    await userEvent.type(screen.getByLabelText("Username"), "ro_user");

    await userEvent.click(screen.getByRole("button", { name: "Save Data Source" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("surfaces a save failure through onError instead of silently closing the form", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      status: 422,
      headers: { get: () => "application/json" },
      ok: false,
      json: async () => ({ detail: "config must not contain secrets ['password']" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const onError = vi.fn();
    const onSourceCreated = vi.fn();
    render(
      <SourcesPanel
        sources={[]}
        connectors={["postgres"]}
        runs={[]}
        busySourceIds={new Set()}
        onSourceCreated={onSourceCreated}
        onDiscover={() => {}}
        onToggleEnabled={() => {}}
        onError={onError}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "+ Add Data Source" }));
    await userEvent.type(screen.getByLabelText("Source name"), "acme-prod");
    await userEvent.type(screen.getByLabelText("Host"), "db.example.com");
    await userEvent.type(screen.getByLabelText("Database name"), "app");
    await userEvent.type(screen.getByLabelText("Username"), "ro_user");

    await userEvent.click(screen.getByRole("button", { name: "Save Data Source" }));

    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith("422: config must not contain secrets ['password']")
    );
    expect(onSourceCreated).not.toHaveBeenCalled();
  });
});
