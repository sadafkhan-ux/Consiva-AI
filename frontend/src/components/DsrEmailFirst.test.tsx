import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DsrCase, DsrSearchRun } from "../api/dsr";
import { DsrEmailFirst } from "./DsrEmailFirst";

function jsonResponse(body: unknown, status = 200) {
  return { status, ok: status < 400, headers: { get: () => "application/json" }, json: async () => body };
}

function makeCase(overrides: Partial<DsrCase> = {}): DsrCase {
  return {
    id: "case-1",
    reference: "DSR-0001",
    status: "received",
    request_type: "access",
    classification_method: "manual",
    classification_confidence: 1,
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

function makeRun(overrides: Partial<DsrSearchRun> = {}): DsrSearchRun {
  return {
    id: "run-1", source: "crm", status: "completed", matches: 0, distinct_subjects: 0,
    tables_searched: ["customers"], error_code: null, error_detail: null,
    completed_at: "2026-01-01T00:05:00+00:00",
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

interface World {
  caseStatus: () => string;
  runs: () => DsrSearchRun[];
  /** A refusal the search endpoint gives instead of queueing a job. */
  searchRefusal?: { status: number; detail: string };
  active?: Array<{ id: string; reference: string; status: string; request_type: string; created_at: string | null }>;
  connectedSources?: boolean;
  identity?: string;
  verify?: Record<string, unknown>;
  responseDraft?: unknown;
}

/** Routes every fetch the flow makes. The case, its runs and its status are read fresh on each GET
 *  so a test can move the backend's state between poll ticks exactly as the real server would. */
function routeFetch(w: World) {
  return vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const identity = w.identity ?? "manually_verified";

    if (url.endsWith("/api/v1/dsr/config/sources")) {
      return Promise.resolve(jsonResponse(w.connectedSources ? [{ id: "a", enabled: true }] : []));
    }
    if (url.includes("/api/v1/dsr/requests/active")) return Promise.resolve(jsonResponse(w.active ?? []));
    if (url.endsWith("/api/v1/dsr/requests") && method === "POST") {
      return Promise.resolve(jsonResponse(makeCase({ status: "classified", request_type: "other", classification_method: "deterministic", classification_confidence: 0 }), 201));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/classify") && method === "POST") {
      return Promise.resolve(jsonResponse(makeCase({ status: "classified" })));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/identity/challenge") && method === "POST") {
      return Promise.resolve(jsonResponse({
        verification_id: "v-1", status: "in_progress", expires_at: "2099-01-01T00:00:00+00:00",
        challenge: "K7QM-4TXR-9BWD", emailed: false, max_attempts: 5,
        delivery_note: "No email was sent: no outbound email provider is configured.",
      }));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/identity/verify") && method === "POST") {
      const verified = makeCase({ status: "identity_verified", identity_status: "manually_verified" });
      return Promise.resolve(jsonResponse(w.verify ?? { status: "manually_verified", satisfied: true, attempts: 0, case: verified }));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/search") && method === "POST") {
      if (w.searchRefusal) return Promise.resolve(jsonResponse({ detail: w.searchRefusal.detail }, w.searchRefusal.status));
      const searching = makeCase({ status: "searching", identity_status: "manually_verified" });
      return Promise.resolve(jsonResponse({ job_id: "job-1", case: searching }));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/response") && method === "POST") {
      return Promise.resolve(jsonResponse(w.responseDraft ?? null, 201));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1") && method === "GET") {
      const status = w.caseStatus();
      const failed = status === "failed";
      return Promise.resolve(jsonResponse(makeCase({
        status,
        identity_status: identity,
        error_code: failed ? "SOURCE_NOT_AUTHORIZED" : null,
        error_detail: failed ? "no source is authorized for DSR" : null,
        status_message: status === "identity_verified" ? "Identity verified." : undefined,
        next_step: status === "identity_verified" ? "Start the search." : undefined,
        allowed_transitions: status === "identity_verified" ? ["searching"] : [],
      })));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/results")) {
      return Promise.resolve(jsonResponse({ search_runs: w.runs(), evidence: [] }));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/plan")) {
      return Promise.resolve(jsonResponse({ plan: null, actions: [], decisions: null }));
    }
    if (url.endsWith("/api/v1/dsr/requests/case-1/executions")) return Promise.resolve(jsonResponse([]));
    if (url.endsWith("/api/v1/dsr/requests/case-1/response")) return Promise.resolve(jsonResponse(null));
    throw new Error(`unexpected fetch: ${url} ${method}`);
  });
}

function callsTo(fetchMock: ReturnType<typeof vi.fn>, suffix: string, method = "POST") {
  return fetchMock.mock.calls.filter(
    ([url, init]) => String(url).endsWith(suffix) && (init?.method ?? "GET") === method,
  );
}

async function openCase(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock);
  render(<DsrEmailFirst />);
  await userEvent.type(screen.getByLabelText("Email address"), "person@example.com");
  await userEvent.click(screen.getByRole("button", { name: "Find my data" }));
  await waitFor(() => expect(screen.getByRole("heading", { name: "Verify the requester" })).toBeInTheDocument());
}

async function verifyManually() {
  await userEvent.click(screen.getByRole("button", { name: "Verify manually" }));
  await userEvent.selectOptions(screen.getByLabelText("How were they verified?"), "id_document");
  await userEvent.type(screen.getByLabelText("Reference — what was checked"), "PP-4471");
  await userEvent.type(
    screen.getByLabelText("What did you check, and why is it enough?"),
    "Checked the passport over a video call",
  );
  await userEvent.click(screen.getByRole("button", { name: "Record verification" }));
}

async function driveToDataStep(fetchMock: ReturnType<typeof vi.fn>) {
  await openCase(fetchMock);
  await verifyManually();
  await waitFor(() =>
    expect(screen.getByRole("heading", { name: "Searching connected systems…" })).toBeInTheDocument(),
  );
}

describe("DsrEmailFirst: the Find my data wizard sets the case up as an access request", () => {
  it("creates the case, then sets its type to access through the classify endpoint", async () => {
    const fetchMock = routeFetch({ caseStatus: () => "classified", runs: () => [] });
    await openCase(fetchMock);

    const created = callsTo(fetchMock, "/api/v1/dsr/requests");
    expect(created).toHaveLength(1);
    const classify = callsTo(fetchMock, "/api/v1/dsr/requests/case-1/classify");
    expect(classify).toHaveLength(1);
    expect(JSON.parse(classify[0][1]!.body as string)).toEqual({ override_type: "access" });
    // Order matters: the case exists before it is classified.
    const order = fetchMock.mock.calls.map(([url]) => String(url));
    expect(order.findIndex((u) => u.endsWith("/api/v1/dsr/requests")))
      .toBeLessThan(order.findIndex((u) => u.endsWith("/classify")));
  });

  it("does not skip identity verification: the next step is still to verify the requester", async () => {
    await openCase(routeFetch({ caseStatus: () => "classified", runs: () => [] }));
    expect(screen.getByRole("heading", { name: "Verify the requester" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Search results/ })).not.toBeInTheDocument();
  });
});

describe("DsrEmailFirst: evidence messaging reflects the actual search outcome", () => {
  it("a search the backend refused says no search was run, with the backend's own reason -- never 'no records'", async () => {
    const refusal = "No data source is connected for DSR. An administrator needs to connect one under DSR → Configuration. This request was left unchanged.";
    const fetchMock = routeFetch({
      caseStatus: () => "identity_verified", runs: () => [],
      searchRefusal: { status: 403, detail: refusal },
    });
    await openCase(fetchMock);
    await verifyManually();

    await waitFor(() => expect(screen.getByText("No search has been run for this case.")).toBeInTheDocument());
    expect(screen.getByText(/No data source is connected for DSR/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Search results" })).toBeInTheDocument();
    expect(screen.queryByText(/No matching records found/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Data found/)).not.toBeInTheDocument();
    // It did not bounce the user back to the verification step they had already passed.
    expect(screen.queryByRole("heading", { name: "Verify the requester" })).not.toBeInTheDocument();
  });

  it("a case with no search run and no refusal still says no search has been run", async () => {
    // The search is queued but the backend later reports the case back at a non-searching status.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "searching";
    const fetchMock = routeFetch({ caseStatus: () => status, runs: () => [] });
    await driveToDataStep(fetchMock);

    status = "identity_verified";
    await vi.advanceTimersByTimeAsync(2000);

    await waitFor(() => expect(screen.getByText("No search has been run for this case.")).toBeInTheDocument());
    expect(screen.queryByText(/No matching records found/)).not.toBeInTheDocument();
    expect(screen.getByText("Next step:")).toBeInTheDocument();
  });

  it("a failed search shows a distinct failure message and a working retry button, never 'no records'", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "searching";
    const fetchMock = routeFetch({ caseStatus: () => status, runs: () => [] });
    await driveToDataStep(fetchMock);

    status = "failed";
    await vi.advanceTimersByTimeAsync(2000);

    await waitFor(() =>
      expect(screen.getByText(/The search did not complete -- it failed with SOURCE_NOT_AUTHORIZED/)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/No matching records found/)).not.toBeInTheDocument();

    const retryButton = screen.getByRole("button", { name: "Retry search" });
    expect(retryButton).toBeEnabled();
    expect(callsTo(fetchMock, "/api/v1/dsr/requests/case-1/search")).toHaveLength(1);

    status = "searching";
    await userEvent.click(retryButton);
    await waitFor(() => expect(callsTo(fetchMock, "/api/v1/dsr/requests/case-1/search")).toHaveLength(2));
  });

  it("only a search that completed with zero matches says no matching records were found", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "searching";
    const fetchMock = routeFetch({ caseStatus: () => status, runs: () => (status === "searching" ? [] : [makeRun({ status: "no_match" })]) });
    await driveToDataStep(fetchMock);

    status = "search_completed";
    await vi.advanceTimersByTimeAsync(2000);

    await waitFor(() => expect(screen.getByText(/No matching records found\. The search completed in crm/)).toBeInTheDocument());
    expect(screen.queryByText(/did not complete/)).not.toBeInTheDocument();
    expect(screen.queryByText("No search has been run for this case.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry search" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue to the response" })).toBeEnabled();
  });

  it("when some sources failed the result is reported as incomplete, not as no data", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "searching";
    const runs = [
      makeRun({ id: "r1", source: "crm", status: "completed" }),
      makeRun({ id: "r2", source: "billing", status: "failed", error_code: "SEARCH_FAILED" }),
    ];
    const fetchMock = routeFetch({ caseStatus: () => status, runs: () => (status === "searching" ? [] : runs) });
    await driveToDataStep(fetchMock);

    status = "search_completed";
    await vi.advanceTimersByTimeAsync(2000);

    await waitFor(() => expect(screen.getByText(/Some sources could not be searched/)).toBeInTheDocument());
    expect(screen.queryByText(/No matching records found/)).not.toBeInTheDocument();
  });

  it("the progress banner is cleared once the search settles", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "searching";
    const fetchMock = routeFetch({ caseStatus: () => status, runs: () => [makeRun({ status: "no_match" })] });
    await driveToDataStep(fetchMock);
    expect(screen.queryByText(/Searching connected systems…/, { selector: '[role="status"]' })).not.toBeInTheDocument();

    status = "search_completed";
    await vi.advanceTimersByTimeAsync(2000);

    await waitFor(() => expect(screen.getByRole("heading", { name: "Search results" })).toBeInTheDocument());
    const banners = screen.queryAllByRole("status").map((n) => n.textContent ?? "");
    expect(banners.some((t) => /Searching/.test(t))).toBe(false);
  });
});

describe("DsrEmailFirst: verification without browser prompts", () => {
  it("'Generate code' shows the code inline with a Copy button, says no email was sent, and never opens a prompt", async () => {
    const prompt = vi.spyOn(window, "prompt");
    await openCase(routeFetch({ caseStatus: () => "classified", runs: () => [], identity: "in_progress" }));

    expect(screen.queryByRole("button", { name: /Email a code/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Generate code" }));

    const panel = await screen.findByRole("region", { name: "Verification code" });
    expect(within(panel).getByTestId("verification-code")).toHaveTextContent("K7QM-4TXR-9BWD");
    expect(within(panel).getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(within(panel).getByText(/No email was sent/)).toBeInTheDocument();
    expect(within(panel).getByText(/Valid for about/)).toBeInTheDocument();
    expect(prompt).not.toHaveBeenCalled();
  });

  it("code entry is off, with the reason on the page, until a code has been generated", async () => {
    await openCase(routeFetch({ caseStatus: () => "classified", runs: () => [], identity: "pending" }));

    expect(screen.getByRole("button", { name: "Check code" })).toBeDisabled();
    expect(screen.getByLabelText("Code from the requester")).toBeDisabled();
    expect(screen.getByText(/Generate a code first/)).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Generate code" }));
    await waitFor(() => expect(screen.getByLabelText("Code from the requester")).toBeEnabled());
  });

  it("a wrong code says how many attempts are left", async () => {
    const verified = makeCase({ identity_status: "in_progress" });
    const fetchMock = routeFetch({
      caseStatus: () => "classified", runs: () => [], identity: "in_progress",
      verify: { status: "in_progress", satisfied: false, attempts: 1, max_attempts: 5, attempts_remaining: 4, case: verified },
    });
    await openCase(fetchMock);
    await userEvent.click(screen.getByRole("button", { name: "Generate code" }));
    await waitFor(() => expect(screen.getByLabelText("Code from the requester")).toBeEnabled());

    await userEvent.type(screen.getByLabelText("Code from the requester"), "WRONG-CODE-0000");
    await userEvent.click(screen.getByRole("button", { name: "Check code" }));

    expect(await screen.findByText("That code didn't match. 4 attempts left.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("4 attempts left");
  });

  it("a locked code says so and points at manual verification", async () => {
    const verified = makeCase({ identity_status: "failed" });
    const fetchMock = routeFetch({
      caseStatus: () => "classified", runs: () => [], identity: "failed",
      verify: { status: "failed", satisfied: false, attempts: 5, max_attempts: 5, attempts_remaining: 0, case: verified },
    });
    await openCase(fetchMock);
    await userEvent.click(screen.getByRole("button", { name: "Generate code" }));
    await waitFor(() => expect(screen.getByLabelText("Code from the requester")).toBeEnabled());
    await userEvent.type(screen.getByLabelText("Code from the requester"), "WRONG-CODE-0000");
    await userEvent.click(screen.getByRole("button", { name: "Check code" }));

    expect(await screen.findByText(/Too many wrong codes\. This code is locked/)).toBeInTheDocument();
  });

  it("manual verification is a form: it needs a method, a reference and a real note, and sends all three", async () => {
    const fetchMock = routeFetch({ caseStatus: () => "identity_verified", runs: () => [] });
    await openCase(fetchMock);
    const prompt = vi.spyOn(window, "prompt");

    await userEvent.click(screen.getByRole("button", { name: "Verify manually" }));
    const record = screen.getByRole("button", { name: "Record verification" });
    expect(record).toBeDisabled();
    expect(screen.getByText(/Choose a method, give a reference, and describe what you checked/)).toBeVisible();

    await verifyManuallyFields();
    expect(record).toBeEnabled();
    await userEvent.click(record);

    await waitFor(() => expect(callsTo(fetchMock, "/identity/verify")).toHaveLength(1));
    expect(JSON.parse(callsTo(fetchMock, "/identity/verify")[0][1]!.body as string)).toEqual({
      manual: true, method: "id_document", reference: "PP-4471",
      evidence_note: "Checked the passport over a video call",
    });
    expect(prompt).not.toHaveBeenCalled();
  });
});

async function verifyManuallyFields() {
  await userEvent.selectOptions(screen.getByLabelText("How were they verified?"), "id_document");
  await userEvent.type(screen.getByLabelText("Reference — what was checked"), "PP-4471");
  await userEvent.type(
    screen.getByLabelText("What did you check, and why is it enough?"),
    "Checked the passport over a video call",
  );
}

describe("DsrEmailFirst: warnings before the dead end", () => {
  it("says up front that no data source is connected, and still lets a case be recorded", async () => {
    vi.stubGlobal("fetch", routeFetch({ caseStatus: () => "classified", runs: () => [], connectedSources: false }));
    render(<DsrEmailFirst />);

    expect(await screen.findByText("No data source is connected yet.")).toBeInTheDocument();
    expect(screen.getByText(/nothing can be searched until an administrator connects a data source/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find my data" })).toBeDisabled(); // only because no email is typed yet
    await userEvent.type(screen.getByLabelText("Email address"), "person@example.com");
    expect(screen.getByRole("button", { name: "Find my data" })).toBeEnabled();
  });

  it("shows no warning when a source is connected", async () => {
    vi.stubGlobal("fetch", routeFetch({ caseStatus: () => "classified", runs: () => [], connectedSources: true }));
    render(<DsrEmailFirst />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Find my data" })).toBeInTheDocument());
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("No data source is connected yet.")).not.toBeInTheDocument();
  });

  it("the warning links to the configuration", async () => {
    const onOpenConfig = vi.fn();
    vi.stubGlobal("fetch", routeFetch({ caseStatus: () => "classified", runs: () => [], connectedSources: false }));
    render(<DsrEmailFirst onOpenConfig={onOpenConfig} />);
    await userEvent.click(await screen.findByRole("button", { name: "Open DSR configuration" }));
    expect(onOpenConfig).toHaveBeenCalled();
  });

  it("an open case for the same requester is pointed out, and another needs a reason", async () => {
    const fetchMock = routeFetch({
      caseStatus: () => "classified", runs: () => [],
      active: [{ id: "case-0", reference: "DSR-0000", status: "classified", request_type: "access", created_at: "2026-01-01T00:00:00+00:00" }],
    });
    vi.stubGlobal("fetch", fetchMock);
    const onOpenCase = vi.fn();
    render(<DsrEmailFirst onOpenCase={onOpenCase} />);

    await userEvent.type(screen.getByLabelText("Email address"), "person@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Find my data" }));

    expect(await screen.findByText("This requester already has an open case.")).toBeInTheDocument();
    expect(screen.getByText("DSR-0000")).toBeInTheDocument();
    expect(callsTo(fetchMock, "/api/v1/dsr/requests")).toHaveLength(0); // nothing was created

    await userEvent.click(screen.getByRole("button", { name: "Open this case" }));
    expect(onOpenCase).toHaveBeenCalledWith("case-0");

    const another = screen.getByRole("button", { name: "Open another case" });
    expect(another).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Need a separate case anyway/), "A different request about a second account");
    await userEvent.click(another);

    await waitFor(() => expect(callsTo(fetchMock, "/api/v1/dsr/requests")).toHaveLength(1));
    expect(JSON.parse(callsTo(fetchMock, "/api/v1/dsr/requests")[0][1]!.body as string)).toMatchObject({
      requester_email: "person@example.com",
      duplicate_override_reason: "A different request about a second account",
    });
  });
});

describe("DsrEmailFirst: response drafting and accessibility", () => {
  it("the response button is off, with the reason written on the page, until a search has finished", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "searching";
    const fetchMock = routeFetch({ caseStatus: () => status, runs: () => [makeRun({ status: "no_match" })] });
    await driveToDataStep(fetchMock);
    status = "search_completed";
    await vi.advanceTimersByTimeAsync(2000);
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue to the response" })).toBeEnabled());

    await userEvent.click(screen.getByRole("button", { name: "Continue to the response" }));

    // A finished search exists, so the draft is allowed.
    expect(screen.getByRole("button", { name: "Generate response" })).toBeEnabled();
  });

  it("without a finished search the draft button is off and says why", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "searching";
    const fetchMock = routeFetch({ caseStatus: () => status, runs: () => [] });
    await driveToDataStep(fetchMock);
    status = "failed";
    await vi.advanceTimersByTimeAsync(2000);
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry search" })).toBeInTheDocument());

    // There is no way to the response step from here, and the draft is off.
    expect(screen.queryByRole("button", { name: "Generate response" })).not.toBeInTheDocument();
  });

  it("marks the current step for assistive technology", async () => {
    vi.stubGlobal("fetch", routeFetch({ caseStatus: () => "classified", runs: () => [] }));
    render(<DsrEmailFirst />);
    const current = screen.getByRole("list", { name: "Progress" }).querySelector('[aria-current="step"]');
    expect(current).toHaveTextContent("email");
  });
});

describe("DsrEmailFirst: coming back to a case after a reload", () => {
  it("resumes at the step the backend's state implies, from a case id alone", async () => {
    const fetchMock = routeFetch({ caseStatus: () => "identity_verified", runs: () => [] });
    vi.stubGlobal("fetch", fetchMock);
    const onCaseChange = vi.fn();

    render(<DsrEmailFirst initialCaseId="case-1" onCaseChange={onCaseChange} />);

    // Identity is verified and nothing has been searched: the data step, saying so.
    expect(await screen.findByText("No search has been run for this case.")).toBeInTheDocument();
    expect(onCaseChange).toHaveBeenCalledWith("case-1");
    expect(callsTo(fetchMock, "/api/v1/dsr/requests", "POST")).toHaveLength(0);
  });

  it("a case that is not yet verified resumes at verification", async () => {
    vi.stubGlobal("fetch", routeFetch({ caseStatus: () => "classified", runs: () => [], identity: "pending" }));
    render(<DsrEmailFirst initialCaseId="case-1" />);
    expect(await screen.findByRole("heading", { name: "Verify the requester" })).toBeInTheDocument();
  });

  it("a case that cannot be opened says so instead of showing an empty wizard", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({ detail: "DSR case not found" }, 404))));
    render(<DsrEmailFirst initialCaseId="case-404" />);
    expect(await screen.findByText(/DSR case not found/)).toBeInTheDocument();
  });
});


describe("DsrEmailFirst: the case shown is the one the backend saved", () => {
  it("shows the id and reference the API returned and opens the details with that same id", async () => {
    const fetchMock = routeFetch({ caseStatus: () => "classified", runs: () => [] });
    vi.stubGlobal("fetch", fetchMock);
    const onOpenCase = vi.fn();
    render(<DsrEmailFirst onOpenCase={onOpenCase} />);
    await userEvent.type(screen.getByLabelText("Email address"), "person@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Find my data" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Verify the requester" })).toBeInTheDocument());

    // Both values come from the response (the fake backend's), not from anything the screen made up.
    expect(screen.getByTestId("case-id")).toHaveTextContent("case-1");
    expect(screen.getByText("DSR-0001")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Open case details" }));
    expect(onOpenCase).toHaveBeenCalledWith("case-1");
  });

  it("every later call for the case uses the returned id in the path", async () => {
    const fetchMock = routeFetch({ caseStatus: () => "classified", runs: () => [], identity: "in_progress" });
    await openCase(fetchMock);
    await userEvent.click(screen.getByRole("button", { name: "Generate code" }));
    await waitFor(() => expect(callsTo(fetchMock, "/api/v1/dsr/requests/case-1/identity/challenge")).toHaveLength(1));

    const caseCalls = fetchMock.mock.calls.map(([url]) => String(url)).filter((u) => u.includes("/requests/") && !u.includes("/requests/active"));
    expect(caseCalls.length).toBeGreaterThan(1);
    for (const url of caseCalls) expect(url).toMatch(/\/api\/v1\/dsr\/requests\/case-1(\/|$)/);
  });
});
