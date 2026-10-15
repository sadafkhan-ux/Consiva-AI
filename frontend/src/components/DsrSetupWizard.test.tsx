import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DsrSetupWizard } from "./DsrSetupWizard";

function jsonResponse(body: unknown, status = 200) {
  return { status, ok: status < 400, headers: { get: () => "application/json" }, json: async () => body };
}

const OWNERSHIP_PROVEN = {
  proven: true, method: "existing_verification", stale: false, verified_at: "2026-10-01T00:00:00Z",
  detail: "this domain was already verified for your organisation",
};

const PATHS = [
  { id: "postgres_direct", title: "Direct PostgreSQL connection", status: "supported", summary: "Read-only role.", requires: ["A read-only role"] },
  { id: "rest_connector", title: "Call your REST API", status: "not_available", summary: "Not yet.", alternative: "evidence_push" },
];

const SOURCES = [
  { data_source_id: "src-1", name: "crm", connector: "postgres", readiness: "ready_to_authorize", next_step: "Choose which tables and columns DSR may use.", allow_execution: false, write_credential_stored: false },
  { data_source_id: "src-2", name: "erp", connector: "sqlserver", readiness: "connector_not_supported", next_step: "Use the push-evidence path instead.", allow_execution: false, write_credential_stored: false },
];

const SCHEMA = {
  available: true, source_name: "crm", truncated: false,
  tables: [{ name: "customers", columns: [{ name: "id", type: "uuid" }, { name: "email", type: "text" }, { name: "name", type: "text" }] }],
};

interface Calls { url: string; method: string; body: unknown }

function wizardFetch(options: { proven?: boolean } = {}) {
  const calls: Calls[] = [];
  const proven = options.proven ?? true;
  const fn = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ url, method, body });
    if (url.endsWith("/api/v1/dsr/setup/domain")) {
      const verified = proven || body?.verify;
      return Promise.resolve(jsonResponse({
        domain: "example.com",
        ownership: verified ? OWNERSHIP_PROVEN : { proven: false, method: null, stale: false, verified_at: null, detail: "this domain has not been verified yet" },
        txt_record: verified ? null : { type: "TXT", host: "example.com", value: "consiva-verify=abc123" },
        grants_access: false,
        note: "Proving a domain shows you control the website. It does not give Consiva access to any database.",
      }));
    }
    if (url.endsWith("/api/v1/dsr/setup/discover")) {
      return Promise.resolve(jsonResponse({
        domain: "example.com", ownership: OWNERSHIP_PROVEN,
        manifest: { found: false, reason: "not_published", declared_by_customer: true },
        paths: PATHS, sources: SOURCES, grants_access: false,
      }));
    }
    if (url.endsWith("/api/v1/dsr/config/sources/src-1/schema")) return Promise.resolve(jsonResponse(SCHEMA));
    if (url.endsWith("/api/v1/dsr/config/sources") && method === "PUT") return Promise.resolve(jsonResponse({ id: "auth-1" }));
    if (url.endsWith("/api/v1/dsr/config/sources/src-1/test")) {
      return Promise.resolve(jsonResponse({
        source_name: "crm", healthy: true, error_code: null, reachable: true,
        tables: [{ table: "customers", ok: true, columns_checked: 3 }],
        write_credential_resolved: false, write_path_verified: false,
        proves: "the source is reachable and every allowlisted table and column is selectable",
        does_not_prove: "that a search matches the right person, that an execution verifies, or that the result reaches the portal",
      }));
    }
    throw new Error(`unexpected fetch: ${url} ${method}`);
  });
  return { fn, calls };
}

beforeEach(() => sessionStorage.setItem("consiva.access_token", "test-token"));
afterEach(() => {
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

async function reachSchema(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Website domain"), "example.com");
  await user.click(screen.getByRole("button", { name: "Check domain" }));
  await screen.findByText("3. Choose a source to authorise");
  await user.click(screen.getAllByRole("button", { name: "Authorise tables" })[0]);
  await screen.findByText("4. Choose what DSR may use in crm");
}

describe("DsrSetupWizard", () => {
  it("tells a non-admin to ask an administrator and makes no request", () => {
    const { fn } = wizardFetch();
    vi.stubGlobal("fetch", fn);
    render(<DsrSetupWizard isAdmin={false} />);
    expect(screen.getByText(/Only an administrator can connect a data source/)).toBeInTheDocument();
    expect(fn).not.toHaveBeenCalled();
  });

  it("shows the DNS record to publish for an unverified domain and discovers nothing yet", async () => {
    const { fn, calls } = wizardFetch({ proven: false });
    vi.stubGlobal("fetch", fn);
    const user = userEvent.setup();
    render(<DsrSetupWizard isAdmin />);

    await user.type(screen.getByLabelText("Website domain"), "example.com");
    await user.click(screen.getByRole("button", { name: "Check domain" }));

    expect(await screen.findByLabelText("DNS record to publish")).toHaveTextContent("consiva-verify=abc123");
    expect(screen.queryByText("2. What can be connected")).not.toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith("/setup/discover"))).toBe(false);
  });

  it("verifies on request and then shows what can be connected, marking unavailable options", async () => {
    const { fn, calls } = wizardFetch({ proven: false });
    vi.stubGlobal("fetch", fn);
    const user = userEvent.setup();
    render(<DsrSetupWizard isAdmin />);

    await user.type(screen.getByLabelText("Website domain"), "example.com");
    await user.click(screen.getByRole("button", { name: "Check domain" }));
    await user.click(await screen.findByRole("button", { name: "I added it — verify now" }));

    expect(await screen.findByText("2. What can be connected")).toBeInTheDocument();
    expect(calls.find((c) => c.url.endsWith("/setup/domain") && (c.body as { verify: boolean }).verify)).toBeTruthy();
    expect(screen.getByText("Not available")).toBeInTheDocument();
    expect(screen.getByText("Available")).toBeInTheDocument();
    // A source DSR cannot search can't be authorised, and says why.
    expect(screen.getByText("Not supported by DSR")).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: "Authorise tables" });
    expect(buttons[0]).toBeEnabled();
    expect(buttons[1]).toBeDisabled();
  });

  it("will not save until each chosen table has an identifier column, and defaults to read-only", async () => {
    const { fn, calls } = wizardFetch();
    vi.stubGlobal("fetch", fn);
    const user = userEvent.setup();
    render(<DsrSetupWizard isAdmin />);
    await reachSchema(user);

    const save = screen.getByRole("button", { name: "Save authorisation" });
    expect(save).toBeDisabled();

    await user.click(screen.getByRole("checkbox", { name: "customers" }));
    expect(save).toBeDisabled();
    expect(screen.getByText(/needs at least one identifier column/)).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("customers email column"), "email");
    await user.click(screen.getByLabelText("customers show name"));
    expect(save).toBeEnabled();
    await user.click(save);

    await screen.findByText(/Saved\. crm will be reused/);
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.body).toMatchObject({
      data_source_id: "src-1",
      searchable_tables: ["customers"],
      identifier_columns: { customers: { email: "email" } },
      returnable_columns: { customers: ["name"] },
      record_key_columns: { customers: ["id"] },
      erasable_columns: { customers: [] },
      allow_execution: false,
    });
    expect(put.body).not.toHaveProperty("write_secret");
  });

  it("shows what a passing connection test does and does not prove", async () => {
    const { fn } = wizardFetch();
    vi.stubGlobal("fetch", fn);
    const user = userEvent.setup();
    render(<DsrSetupWizard isAdmin />);
    await reachSchema(user);
    await user.click(screen.getByRole("checkbox", { name: "customers" }));
    await user.selectOptions(screen.getByLabelText("customers email column"), "email");
    await user.click(screen.getByRole("button", { name: "Save authorisation" }));
    await user.click(await screen.findByRole("button", { name: "Test connection" }));

    expect(await screen.findByText("Healthy")).toBeInTheDocument();
    expect(screen.getByText(/Proves: the source is reachable/)).toBeInTheDocument();
    expect(screen.getByText(/Does not prove: that a search matches the right person/)).toBeInTheDocument();
  });

  it("sends the write password once, never renders it, and clears it after saving", async () => {
    const { fn, calls } = wizardFetch();
    vi.stubGlobal("fetch", fn);
    const user = userEvent.setup();
    render(<DsrSetupWizard isAdmin />);
    await reachSchema(user);
    await user.click(screen.getByRole("checkbox", { name: "customers" }));
    await user.selectOptions(screen.getByLabelText("customers email column"), "email");

    await user.click(screen.getByLabelText(/Allow DSR to change or erase data/));
    const password = screen.getByLabelText("Write password") as HTMLInputElement;
    expect(password.type).toBe("password");
    await user.type(password, "s3cr3t-DO-NOT-SHOW");
    // Execution needs at least one column the admin has named as changeable.
    expect(screen.getByRole("button", { name: "Save authorisation" })).toBeDisabled();
    await user.click(screen.getByLabelText("customers change name"));
    await user.click(screen.getByRole("button", { name: "Save authorisation" }));
    await screen.findByText(/Saved\. crm will be reused/);

    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.body).toMatchObject({
      allow_execution: true, erasable_columns: { customers: ["name"] }, write_secret: "s3cr3t-DO-NOT-SHOW",
    });
    expect((screen.getByLabelText("Write password") as HTMLInputElement).value).toBe("");
    expect(document.body.textContent).not.toContain("s3cr3t-DO-NOT-SHOW");
  });

  it("explains a schema that could not be read instead of showing an empty form", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
      calls.push(url);
      const base = wizardFetch().fn;
      if (url.endsWith("/schema")) {
        return Promise.resolve(jsonResponse({
          available: false, source_name: "crm", tables: [], error_code: "CONNECTOR_UNAVAILABLE",
          message: "source 'crm' is unreachable or rejected the connection",
        }));
      }
      return base(url, init);
    }));
    const user = userEvent.setup();
    render(<DsrSetupWizard isAdmin />);
    await user.type(screen.getByLabelText("Website domain"), "example.com");
    await user.click(screen.getByRole("button", { name: "Check domain" }));
    await user.click((await screen.findAllByRole("button", { name: "Authorise tables" }))[0]);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Could not read crm's tables/));
    expect(screen.queryByText(/4\. Choose what DSR may use/)).not.toBeInTheDocument();
  });
});
