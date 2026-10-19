import { render, screen, waitFor, within } from "@testing-library/react";
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
  search_run_id: "run-1",
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

function localDate(iso: string) {
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

beforeEach(() => {
  sessionStorage.setItem("consiva.access_token", "test-token");
});

afterEach(() => {
  sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** A backend for the cases view: one case, with whatever results and extra routes a test needs. */
function backend(
  CASE: DsrCase,
  opts: {
    results?: { search_runs: DsrSearchRun[]; evidence: DsrEvidence[] };
    extra?: (url: string, init?: RequestInit) => unknown | undefined;
    active?: unknown[];
    sources?: unknown[];
    response?: unknown;
  } = {},
) {
  return vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const extra = opts.extra?.(url, init);
    if (extra !== undefined) return Promise.resolve(extra);
    if (url.endsWith("/api/v1/dsr/config/sources")) return Promise.resolve(jsonResponse(opts.sources ?? [{ id: "a", enabled: true }]));
    if (url.includes("/api/v1/dsr/requests/active")) return Promise.resolve(jsonResponse(opts.active ?? []));
    if (url.endsWith("/api/v1/dsr/requests") && method === "GET") return Promise.resolve(jsonResponse([CASE]));
    if (url.endsWith(`/api/v1/dsr/requests/${CASE.id}`)) return Promise.resolve(jsonResponse(CASE));
    if (url.endsWith(`/api/v1/dsr/requests/${CASE.id}/results`)) {
      return Promise.resolve(jsonResponse(opts.results ?? { search_runs: [], evidence: [] }));
    }
    if (url.endsWith(`/api/v1/dsr/requests/${CASE.id}/response`) && method === "GET") {
      return Promise.resolve(jsonResponse(opts.response ?? null));
    }
    throw new Error(`unexpected fetch: ${url} ${method}`);
  });
}

function calls(fetchMock: ReturnType<typeof vi.fn>, suffix: string, method = "POST") {
  return fetchMock.mock.calls.filter(([url, init]) => String(url).endsWith(suffix) && (init?.method ?? "GET") === method);
}

async function openCase(fetchMock: ReturnType<typeof vi.fn>, props: Parameters<typeof DsrDashboard>[0] = {}) {
  vi.stubGlobal("fetch", fetchMock);
  render(<DsrDashboard {...props} />);
  await waitFor(() => expect(screen.getByText("DSR-0001")).toBeInTheDocument());
  await userEvent.click(screen.getByRole("button", { name: /DSR-0001/ }));
  await waitFor(() => expect(screen.getByRole("heading", { name: "DSR-0001" })).toBeInTheDocument());
}

describe("DsrDashboard: case list", () => {
  it("loads cases from the real endpoint on mount and renders real fields, not placeholders", async () => {
    const fetchMock = backend(makeCase());
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrDashboard />);

    await waitFor(() => expect(screen.getByText("DSR-0001")).toBeInTheDocument());
    expect(screen.getByText("erasure")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(`${BASE_URL}/api/v1/dsr/requests`, expect.anything());
  });

  it("shows the requester masked, when it was created and when it is due", async () => {
    vi.stubGlobal("fetch", backend(makeCase()));
    render(<DsrDashboard />);

    const row = (await screen.findByRole("button", { name: /DSR-0001/ })).textContent ?? "";
    expect(row).toContain("pe••••@example.com");
    expect(row).not.toContain("person@example.com");
    expect(row).toContain(`Created ${localDate("2026-01-01T00:00:00+00:00")}`);
    expect(row).toContain(`Due ${localDate("2026-02-01T00:00:00+00:00")}`);
  });

  it("names a status in words, not as a stored code", async () => {
    vi.stubGlobal("fetch", backend(makeCase({ status: "review_required", request_type: "other", classification_method: "deterministic", classification_confidence: 0 })));
    render(<DsrDashboard />);

    const row = await screen.findByRole("button", { name: /DSR-0001/ });
    expect(row).toHaveTextContent("Needs review — request type unclear");
    expect(row).not.toHaveTextContent("review_required");
  });

  it("shows a loading indicator before the first fetch resolves, distinct from the empty state", async () => {
    let resolveFetch!: (value: unknown) => void;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/dsr/requests")) {
        return new Promise((resolve) => {
          resolveFetch = resolve;
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrDashboard />);

    // Before the list call resolves, the screen must say "loading", never the
    // same empty-list message a genuinely empty org would see.
    expect(screen.getByText("Loading cases…")).toBeInTheDocument();
    expect(screen.queryByText("No DSR cases yet.")).not.toBeInTheDocument();

    resolveFetch(jsonResponse([]));
    await waitFor(() => expect(screen.getByText("No DSR cases yet.")).toBeInTheDocument());
    expect(screen.queryByText("Loading cases…")).not.toBeInTheDocument();
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
    await openCase(backend(makeCase(), { results: { search_runs: [RUN], evidence: [EVIDENCE] } }));

    await waitFor(() => expect(screen.getByText("Evidence (1)")).toBeInTheDocument());
    expect(screen.getByText("prepmyevent-production/users")).toBeInTheDocument();
    expect(screen.getByText(JSON.stringify(EVIDENCE.record_snapshot))).toBeInTheDocument();
  });

  it("shows one plain status, the identity state in words, and the due date", async () => {
    await openCase(backend(makeCase({ status: "identity_verified", identity_status: "manually_verified" })));

    const header = screen.getByRole("heading", { name: "DSR-0001" }).closest("header")!;
    expect(within(header).getByText("Identity verified — ready to search")).toBeInTheDocument();
    expect(within(header).getByText("Identity: Verified by a person")).toBeInTheDocument();
    expect(within(header).getByText(`Due ${localDate("2026-02-01T00:00:00+00:00")}`)).toBeInTheDocument();
    expect(within(header).queryByText("manually_verified")).not.toBeInTheDocument();
  });

  it("'Generate code' shows the code inline with a Copy button -- it never opens a browser prompt", async () => {
    const CASE = makeCase({ identity_status: "pending", status: "classified" });
    const prompt = vi.spyOn(window, "prompt").mockReturnValue(null);
    const fetchMock = backend(CASE, {
      extra: (url, init) =>
        url.endsWith(`/api/v1/dsr/requests/${CASE.id}/identity/challenge`) && init?.method === "POST"
          ? jsonResponse({
              verification_id: "chal-1", status: "in_progress", expires_at: "2099-01-01T00:00:00+00:00",
              challenge: "K7QM-4TXR-9BWD", emailed: false, max_attempts: 5,
              delivery_note: "No email was sent.",
            })
          : undefined,
    });
    await openCase(fetchMock);
    await waitFor(() => expect(screen.getByRole("button", { name: "Generate code" })).toBeEnabled());

    await userEvent.click(screen.getByRole("button", { name: "Generate code" }));

    await waitFor(() => expect(calls(fetchMock, `/api/v1/dsr/requests/${CASE.id}/identity/challenge`)).toHaveLength(1));
    const panel = await screen.findByRole("region", { name: "Verification code" });
    expect(within(panel).getByTestId("verification-code")).toHaveTextContent("K7QM-4TXR-9BWD");
    expect(within(panel).getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(prompt).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Issue identity challenge" })).not.toBeInTheDocument();
  });

  it("entering a code is off until one exists, then reports a wrong code with the attempts left", async () => {
    const CASE = makeCase({ identity_status: "pending", status: "classified" });
    const fetchMock = backend(CASE, {
      extra: (url, init) => {
        if (url.endsWith("/identity/challenge") && init?.method === "POST") {
          return jsonResponse({ verification_id: "c", status: "in_progress", expires_at: "2099-01-01T00:00:00+00:00", challenge: "K7QM-4TXR-9BWD", emailed: false });
        }
        if (url.endsWith("/identity/verify") && init?.method === "POST") {
          return jsonResponse({ status: "in_progress", satisfied: false, attempts: 2, max_attempts: 5, attempts_remaining: 3, case: CASE });
        }
        return undefined;
      },
    });
    await openCase(fetchMock);

    expect(screen.getByRole("button", { name: "Check code" })).toBeDisabled();
    expect(screen.getByText(/Generate a code first/)).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Generate code" }));
    await waitFor(() => expect(screen.getByLabelText("Code from the requester")).toBeEnabled());
    await userEvent.type(screen.getByLabelText("Code from the requester"), "AAAA-BBBB-CCCC");
    await userEvent.click(screen.getByRole("button", { name: "Check code" }));

    expect(await screen.findByText("That code didn't match. 3 attempts left.")).toBeInTheDocument();
  });

  it("manual verification is a structured form whose three fields are all sent", async () => {
    const CASE = makeCase({ identity_status: "pending", status: "classified" });
    const prompt = vi.spyOn(window, "prompt");
    const fetchMock = backend(CASE, {
      extra: (url, init) =>
        url.endsWith("/identity/verify") && init?.method === "POST"
          ? jsonResponse({ status: "manually_verified", satisfied: true, attempts: 0, case: { ...CASE, identity_status: "manually_verified" } })
          : undefined,
    });
    await openCase(fetchMock);

    await userEvent.click(screen.getByRole("button", { name: "Verify manually" }));
    expect(screen.getByRole("button", { name: "Record verification" })).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("How were they verified?"), "account_login");
    await userEvent.type(screen.getByLabelText("Reference — what was checked"), "acct-991");
    await userEvent.type(screen.getByLabelText("What did you check, and why is it enough?"), "Matched the signed-in account email");
    await userEvent.click(screen.getByRole("button", { name: "Record verification" }));

    await waitFor(() => expect(calls(fetchMock, "/identity/verify")).toHaveLength(1));
    expect(JSON.parse(calls(fetchMock, "/identity/verify")[0][1]!.body as string)).toEqual({
      manual: true, method: "account_login", reference: "acct-991", evidence_note: "Matched the signed-in account email",
    });
    expect(prompt).not.toHaveBeenCalled();
  });
});

describe("DsrDashboard: a request whose type is unclear can be set and then continue", () => {
  const UNCLEAR = () =>
    makeCase({
      status: "review_required", request_type: "other", classification_method: "deterministic",
      classification_confidence: 0, identity_status: "verified",
      status_message: "The system could not tell what this request is asking for, so no search was run.",
      next_step: "Read the request, set its type, then start the search.",
      allowed_transitions: ["searching", "approval_required"],
    });

  it("shows a request-type selector with only the types the backend implements", async () => {
    await openCase(backend(UNCLEAR()));

    const select = screen.getByLabelText("What is this request?");
    const options = within(select).getAllByRole("option").map((o) => (o as HTMLOptionElement).value).filter(Boolean);
    expect(options).toEqual(["access", "export", "information", "correction", "deletion", "withdraw_consent", "grievance"]);
    expect(screen.getByText(/so no search has been run/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set request type" })).toBeDisabled();
  });

  it("applying a type calls the classify endpoint, then the normal Start search is available", async () => {
    let current = UNCLEAR();
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/v1/dsr/config/sources")) return Promise.resolve(jsonResponse([{ id: "a", enabled: true }]));
      if (url.endsWith("/api/v1/dsr/requests") && method === "GET") return Promise.resolve(jsonResponse([current]));
      if (url.endsWith("/classify") && method === "POST") {
        current = makeCase({ status: "review_required", request_type: "access", classification_method: "manual", classification_confidence: 1, identity_status: "verified", allowed_transitions: ["searching"] });
        return Promise.resolve(jsonResponse(current));
      }
      if (url.endsWith("/api/v1/dsr/requests/case-1")) return Promise.resolve(jsonResponse(current));
      if (url.endsWith("/results")) return Promise.resolve(jsonResponse({ search_runs: [], evidence: [] }));
      if (url.endsWith("/response")) return Promise.resolve(jsonResponse(null));
      throw new Error(`unexpected fetch: ${url} ${method}`);
    });
    await openCase(fetchMock);
    expect(screen.getByRole("button", { name: "Start search" })).toBeEnabled(); // identity ok + searchable

    await userEvent.selectOptions(screen.getByLabelText("What is this request?"), "access");
    await userEvent.click(screen.getByRole("button", { name: "Set request type" }));

    await waitFor(() => expect(calls(fetchMock, "/classify")).toHaveLength(1));
    expect(JSON.parse(calls(fetchMock, "/classify")[0][1]!.body as string)).toEqual({ override_type: "access" });
    await waitFor(() => expect(screen.queryByLabelText("What is this request?")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Start search" })).toBeEnabled();
  });

  it("a type a person already chose is not asked again", async () => {
    await openCase(backend(makeCase({ request_type: "access", classification_method: "manual", classification_confidence: 1 })));
    expect(screen.queryByLabelText("What is this request?")).not.toBeInTheDocument();
  });
});

describe("DsrDashboard: evidence tab reflects actual search outcome, not just evidence count", () => {
  async function renderCaseWithResults(
    caseOverrides: Partial<DsrCase>,
    results: { search_runs: DsrSearchRun[]; evidence: DsrEvidence[] },
  ) {
    await openCase(backend(makeCase(caseOverrides), { results }));
    await waitFor(() => expect(screen.getByText(/^Evidence \(/)).toBeInTheDocument());
  }

  it("not yet searched: says no search has been run, with the backend's message and next step", async () => {
    await renderCaseWithResults(
      {
        status: "identity_verified", error_code: null, allowed_transitions: ["searching"],
        status_message: "Identity verified.", next_step: "Start the search.",
      },
      { search_runs: [], evidence: [] },
    );
    const panel = screen.getByRole("tabpanel");
    expect(within(panel).getByText("No search has been run for this case.")).toBeInTheDocument();
    expect(within(panel).getByText("Next step:")).toBeInTheDocument();
    expect(screen.queryByText(/No matching records found/)).not.toBeInTheDocument();
    expect(screen.queryByText(/did not complete/)).not.toBeInTheDocument();
  });

  it("evidence with no search behind it is not shown as a result", async () => {
    await renderCaseWithResults(
      { status: "identity_verified", allowed_transitions: ["searching"] },
      { search_runs: [], evidence: [EVIDENCE] },
    );
    expect(screen.getByText("Evidence (0)")).toBeInTheDocument();
    expect(screen.queryByText("prepmyevent-production/users")).not.toBeInTheDocument();
  });

  it("a search still running says so, and does not claim any result", async () => {
    await renderCaseWithResults(
      { status: "searching", allowed_transitions: [] },
      { search_runs: [{ ...RUN, status: "running" }], evidence: [] },
    );
    expect(screen.getByText(/A search is in progress/)).toBeInTheDocument();
    expect(screen.queryByText(/No matching records found/)).not.toBeInTheDocument();
  });

  it("genuine zero-match: a completed search says so, distinctly from a failure", async () => {
    await renderCaseWithResults(
      { status: "search_completed", error_code: null, allowed_transitions: ["response_pending"] },
      { search_runs: [{ ...RUN, status: "no_match", matches: 0 }], evidence: [] },
    );
    expect(
      screen.getByText(
        "No matching records found. The search completed and found no data for this person in the systems searched.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/did not complete/)).not.toBeInTheDocument();
    expect(screen.queryByText("No search has been run for this case.")).not.toBeInTheDocument();
  });

  it("failed search: says the search errored out, never the zero-match wording", async () => {
    const FAILED: DsrSearchRun = {
      ...RUN, status: "failed", matches: 0, distinct_subjects: 0, tables_searched: [],
      error_code: "SOURCE_NOT_AUTHORIZED", error_detail: "no authorized source",
    };
    await renderCaseWithResults(
      {
        status: "failed", error_code: "SOURCE_NOT_AUTHORIZED", error_detail: "no authorized source",
        allowed_transitions: ["searching"],
      },
      { search_runs: [FAILED], evidence: [] },
    );
    expect(screen.getByText(/The search did not complete -- it failed with SOURCE_NOT_AUTHORIZED/)).toBeInTheDocument();
    expect(screen.queryByText(/No matching records found/)).not.toBeInTheDocument();
  });

  it("failed with no run recorded at all is a failure, not 'no search has been run'", async () => {
    await renderCaseWithResults(
      { status: "failed", error_code: "SOURCE_NOT_AUTHORIZED", error_detail: "no authorized source", allowed_transitions: ["searching"] },
      { search_runs: [], evidence: [] },
    );
    expect(screen.getByText(/The search did not complete -- it failed with SOURCE_NOT_AUTHORIZED/)).toBeInTheDocument();
  });

  it("partial: a failed source beside a finished one is reported as incomplete", async () => {
    await renderCaseWithResults(
      { status: "search_completed", allowed_transitions: ["response_pending"] },
      { search_runs: [RUN, { ...RUN, id: "run-2", source: "billing", status: "failed" }], evidence: [EVIDENCE] },
    );
    expect(screen.getByText(/Some sources could not be searched/)).toBeInTheDocument();
  });
});

describe("DsrDashboard: a response is not offered before there is anything true to say", () => {
  it("the draft button is off, and the reason is written on the page, when no search has run", async () => {
    await openCase(backend(makeCase({ status: "identity_verified", allowed_transitions: ["searching"] })));

    expect(screen.getByRole("button", { name: "Generate response" })).toBeDisabled();
    expect(screen.getByText("Generate response is off until the requester is verified and a search has finished.")).toBeVisible();
  });

  it("it is off for an unverified requester even if a run exists", async () => {
    await openCase(backend(makeCase({ identity_status: "pending", status: "classified" }), { results: { search_runs: [RUN], evidence: [] } }));
    expect(screen.getByRole("button", { name: "Generate response" })).toBeDisabled();
  });

  it("it is available once the requester is verified and a search has finished", async () => {
    await openCase(backend(makeCase({ status: "search_completed" }), { results: { search_runs: [{ ...RUN, status: "no_match" }], evidence: [] } }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Generate response" })).toBeEnabled());
    expect(screen.queryByText(/Generate response is off/)).not.toBeInTheDocument();
  });

  it("it is off when the only search failed", async () => {
    await openCase(backend(makeCase({ status: "failed", error_code: "SEARCH_FAILED" }), { results: { search_runs: [{ ...RUN, status: "failed" }], evidence: [] } }));
    expect(screen.getByRole("button", { name: "Generate response" })).toBeDisabled();
  });

  it("a complaint may be answered without a search", async () => {
    await openCase(backend(makeCase({ request_type: "grievance", classification_method: "deterministic", classification_confidence: 0.95, status: "review_required", identity_status: "pending" })));
    expect(screen.getByRole("button", { name: "Generate response" })).toBeEnabled();
  });

  it("'Mark sent & close' says why it is off", async () => {
    await openCase(backend(makeCase({ status: "search_completed" }), { results: { search_runs: [RUN], evidence: [] } }));
    expect(screen.getByText("Mark sent & close needs a drafted response first.")).toBeVisible();
  });
});

describe("DsrDashboard: new case intake", () => {
  function intakeBackend(CREATED: DsrCase, active: unknown[] = []) {
    return vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/v1/dsr/config/sources")) return Promise.resolve(jsonResponse([{ id: "a", enabled: true }]));
      if (url.includes("/api/v1/dsr/requests/active")) return Promise.resolve(jsonResponse(active));
      if (url.endsWith("/api/v1/dsr/requests") && method === "GET") return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/v1/dsr/requests") && method === "POST") return Promise.resolve(jsonResponse(CREATED, 201));
      if (url.endsWith(`/api/v1/dsr/requests/${CREATED.id}/classify`) && method === "POST") {
        const body = JSON.parse(init!.body as string);
        return Promise.resolve(jsonResponse({ ...CREATED, request_type: body.override_type, classification_method: "manual", classification_confidence: 1 }));
      }
      if (url.endsWith(`/api/v1/dsr/requests/${CREATED.id}`)) return Promise.resolve(jsonResponse(CREATED));
      if (url.endsWith(`/api/v1/dsr/requests/${CREATED.id}/results`)) return Promise.resolve(jsonResponse({ search_runs: [], evidence: [] }));
      if (url.endsWith(`/api/v1/dsr/requests/${CREATED.id}/response`)) return Promise.resolve(jsonResponse(null));
      throw new Error(`unexpected fetch: ${url} ${method}`);
    });
  }

  async function fillIntake() {
    await userEvent.type(
      screen.getByPlaceholderText("e.g. Please delete all my personal information."),
      "Please tell me what data you hold on me.",
    );
    await userEvent.type(screen.getByPlaceholderText("person@example.com"), "person@example.com");
  }

  it("creates a case through the intake form, posting the real payload and showing a success notice", async () => {
    const CREATED = makeCase({ id: "case-2", reference: "DSR-0042", request_type: "access" });
    const fetchMock = intakeBackend(CREATED);
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrDashboard />);
    await waitFor(() => expect(screen.getByText("No DSR cases yet.")).toBeInTheDocument());
    await fillIntake();
    await userEvent.click(screen.getByRole("button", { name: "Create case" }));

    await waitFor(() =>
      expect(screen.getByText('Case DSR-0042 created and classified as "access".')).toBeInTheDocument(),
    );
    expect(calls(fetchMock, "/api/v1/dsr/requests")).toHaveLength(1);
    expect(JSON.parse(calls(fetchMock, "/api/v1/dsr/requests")[0][1]!.body as string)).toEqual({
      raw_request: "Please tell me what data you hold on me.",
      requester_email: "person@example.com",
    });
    // Left to the classifier: a person did not choose, so nothing is forced.
    expect(calls(fetchMock, "/classify")).toHaveLength(0);
  });

  it("a type chosen on the form is applied through the same classify step the wizard uses", async () => {
    const CREATED = makeCase({ id: "case-2", reference: "DSR-0042", request_type: "other" });
    const fetchMock = intakeBackend(CREATED);
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrDashboard />);
    await waitFor(() => expect(screen.getByText("No DSR cases yet.")).toBeInTheDocument());
    await fillIntake();
    await userEvent.selectOptions(screen.getByLabelText("Request type"), "access");
    await userEvent.click(screen.getByRole("button", { name: "Create case" }));

    await waitFor(() => expect(calls(fetchMock, "/classify")).toHaveLength(1));
    expect(JSON.parse(calls(fetchMock, "/classify")[0][1]!.body as string)).toEqual({ override_type: "access" });
    expect(await screen.findByText('Case DSR-0042 created and classified as "access".')).toBeInTheDocument();
  });

  it("an open case for the same requester is pointed out first, and another needs a recorded reason", async () => {
    const CREATED = makeCase({ id: "case-2", reference: "DSR-0042", request_type: "access" });
    const existing = [{ id: "case-0", reference: "DSR-0000", status: "classified", request_type: "access", created_at: "2026-01-01T00:00:00+00:00" }];
    const fetchMock = intakeBackend(CREATED, existing);
    vi.stubGlobal("fetch", fetchMock);

    render(<DsrDashboard />);
    await waitFor(() => expect(screen.getByText("No DSR cases yet.")).toBeInTheDocument());
    await fillIntake();
    await userEvent.click(screen.getByRole("button", { name: "Create case" }));

    expect(await screen.findByText("This requester already has an open case.")).toBeInTheDocument();
    expect(calls(fetchMock, "/api/v1/dsr/requests")).toHaveLength(0);

    await userEvent.type(screen.getByLabelText(/Need a separate case anyway/), "Second account, separate request");
    await userEvent.click(screen.getByRole("button", { name: "Open another case" }));

    await waitFor(() => expect(calls(fetchMock, "/api/v1/dsr/requests")).toHaveLength(1));
    expect(JSON.parse(calls(fetchMock, "/api/v1/dsr/requests")[0][1]!.body as string)).toMatchObject({
      duplicate_override_reason: "Second account, separate request",
    });
  });

  it("warns up front when no data source is connected", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/v1/dsr/config/sources")) return Promise.resolve(jsonResponse([]));
      if (url.endsWith("/api/v1/dsr/requests")) return Promise.resolve(jsonResponse([]));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<DsrDashboard />);
    expect(await screen.findByText("No data source is connected yet.")).toBeInTheDocument();
  });
});

describe("DsrDashboard: coming back after a reload, and accessibility", () => {
  it("opens the case named by the address without a click, and reports the open case", async () => {
    const onCaseChange = vi.fn();
    vi.stubGlobal("fetch", backend(makeCase()));

    render(<DsrDashboard initialCaseId="case-1" onCaseChange={onCaseChange} />);

    expect(await screen.findByRole("heading", { name: "DSR-0001" })).toBeInTheDocument();
    await waitFor(() => expect(onCaseChange).toHaveBeenCalledWith("case-1"));
  });

  it("the case detail tabs are real tabs, with the selected one marked", async () => {
    await openCase(backend(makeCase()));

    const tabs = screen.getByRole("tablist", { name: "Case details" });
    expect(within(tabs).getByRole("tab", { name: "evidence", selected: true })).toBeInTheDocument();
    await userEvent.click(within(tabs).getByRole("tab", { name: "plan" }));
    expect(within(tabs).getByRole("tab", { name: "plan", selected: true })).toBeInTheDocument();
    expect(screen.getByRole("tabpanel")).toBeInTheDocument();
  });

  it("status messages are announced: the notice region is a status, errors are alerts", async () => {
    await openCase(backend(makeCase({ status: "identity_verified", status_message: "Identity verified.", next_step: "Start the search." })));
    expect(screen.getAllByRole("status").some((n) => /Identity verified\./.test(n.textContent ?? ""))).toBe(true);
  });
});

describe("DsrDashboard: the id on screen is the one the backend returned", () => {
  it("shows the case id, selectable, next to the reference", async () => {
    await openCase(backend(makeCase({ id: "58ea8dbd-7e40-4f49-9e94-1234124ccc86" })));
    expect(screen.getByTestId("case-id")).toHaveTextContent("58ea8dbd-7e40-4f49-9e94-1234124ccc86");
    expect(screen.getByRole("heading", { name: "DSR-0001" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy ID" })).toBeInTheDocument();
  });

  it("loads the case's details from the returned id, not from the reference", async () => {
    const CASE = makeCase({ id: "58ea8dbd-7e40-4f49-9e94-1234124ccc86" });
    const fetchMock = backend(CASE);
    await openCase(fetchMock);
    const detailCalls = fetchMock.mock.calls.map(([u]) => String(u)).filter((u) => u.includes(`/requests/${CASE.id}`));
    expect(detailCalls.length).toBeGreaterThan(0);
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("DSR-0001"))).toBe(false);
  });
});
