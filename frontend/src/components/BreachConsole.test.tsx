import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Incident, IncidentAction, PlanView } from "../api/incidents";
import { BreachConsole } from "./BreachConsole";

const BASE_URL = "http://127.0.0.1:8000";

function jsonResponse(body: unknown, status = 200) {
  return { status, ok: status < 400, headers: { get: () => "application/json" }, json: async () => body };
}
type FetchResponse = ReturnType<typeof jsonResponse>;

function makeIncident(overrides: Partial<Incident> = {}): Incident {
  return {
    id: "inc-1",
    reference: "INC-0001",
    title: "Unauthorized access to the customer database",
    description: "An account with no business reason queried it all night.",
    source: "siem",
    reported_by: null,
    incident_type: "unauthorized_access",
    classification_method: "deterministic",
    classification_confidence: 0.9,
    initial_severity: null,
    severity: null,
    severity_score: null,
    severity_confidence: null,
    personal_data_involved: "unknown",
    breach_confirmed: "unknown",
    status: "responding",
    error_code: null,
    error_detail: null,
    occurred_at: null,
    detected_at: "2026-01-01T00:00:00+00:00",
    closure_summary: null,
    allowed_transitions: [],
    is_terminal: false,
    sla: {
      detected_at: "2026-01-01T00:00:00+00:00",
      reported_at: null,
      due_at: "2026-01-04T00:00:00+00:00",
      remaining_seconds: 100_000,
      overdue: false,
      breached: false,
      escalated: false,
      closed: false,
      note: "Internal response target, not a statutory deadline.",
    },
    created_at: "2026-01-01T00:00:00+00:00",
    closed_at: null,
    ...overrides,
  };
}

function makeAction(overrides: Partial<IncidentAction> = {}): IncidentAction {
  return {
    id: "action-1",
    action_kind: "preserve_logs",
    title: "Preserve access and query logs",
    rationale: "Establishing scope later depends entirely on logs kept now.",
    expected_result: "relevant logs are copied somewhere retention cannot expire them",
    target: null,
    risk: "low",
    status: "approved",
    requires_approval: false,
    blocked_reason: null,
    assignee_label: null,
    execution_mode: "tracked",
    executions: [],
    ...overrides,
  };
}

function emptyPlan(actions: IncidentAction[] = []): PlanView {
  return {
    actions,
    summary: {
      total: actions.length,
      approved: actions.filter((a) => a.status === "approved").length,
      rejected: actions.filter((a) => a.status === "rejected").length,
      blocked: actions.filter((a) => a.status === "blocked").length,
      completed: actions.filter((a) => a.status === "completed").length,
      failed: actions.filter((a) => a.status === "failed").length,
      awaiting_decision: 0,
      no_approval_needed: actions.filter((a) => !a.requires_approval && a.status === "proposed").length,
      ready_to_respond: true,
      nothing_to_do: actions.length === 0,
    },
  };
}

/** Every endpoint `loadIncident` fetches in parallel, keyed by URL suffix, so a test
 *  only has to describe the handful of responses it actually cares about. Anything
 *  unexpected throws rather than silently returning something plausible-looking. */
function makeRouter(opts: {
  incidents: () => Incident[];
  plan?: () => PlanView;
  onPost?: (path: string, body: unknown) => FetchResponse | Promise<FetchResponse> | undefined;
}) {
  const detailCallCount = new Map<string, number>();
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const path = url.replace(BASE_URL, "");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(init.body as string) : undefined;

    if (method === "POST" && opts.onPost) {
      const handled = opts.onPost(path, body);
      if (handled !== undefined) return Promise.resolve(handled);
    }

    const incidents = opts.incidents();
    if (path === "/api/v1/incidents") return Promise.resolve(jsonResponse(incidents));

    const match = incidents.find((i) => path === `/api/v1/incidents/${i.id}`);
    if (match) {
      detailCallCount.set(match.id, (detailCallCount.get(match.id) ?? 0) + 1);
      return Promise.resolve(jsonResponse(match));
    }

    if (path.endsWith("/evidence")) return Promise.resolve(jsonResponse([]));
    if (path.endsWith("/timeline")) return Promise.resolve(jsonResponse([]));
    if (path.endsWith("/impact")) {
      return Promise.resolve(
        jsonResponse({ systems: [], data_categories: [], subjects: [], total: { record_count: null, count_basis: "unknown" } }),
      );
    }
    if (path.endsWith("/risk")) return Promise.resolve(jsonResponse({ current: null, history: [] }));
    // getPlan reads /actions (plural, list); buildPlan POSTs to /plan -- genuinely
    // different paths in the real API client, not a typo here.
    if (method === "GET" && path.endsWith("/actions")) {
      return Promise.resolve(jsonResponse((opts.plan ?? (() => emptyPlan()))()));
    }
    if (path.endsWith("/communications")) return Promise.resolve(jsonResponse([]));
    if (path.endsWith("/report")) return Promise.resolve(jsonResponse(null));
    if (path.endsWith("/audit")) return Promise.resolve(jsonResponse([]));

    throw new Error(`unexpected fetch: ${method} ${path}`);
  });
  return { fetchMock, detailCallCount };
}

async function selectIncident(reference: string) {
  await userEvent.click(await screen.findByRole("button", { name: new RegExp(reference) }));
}

beforeEach(() => {
  sessionStorage.setItem("consiva.access_token", "test-token");
});

afterEach(() => {
  sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("BreachConsole: recording a containment action as failed", () => {
  it("records a failure with a reason and shows the result", async () => {
    const incident = makeIncident();
    const action = makeAction({ status: "approved" });
    let currentAction = action;
    const { fetchMock } = makeRouter({
      incidents: () => [incident],
      plan: () => emptyPlan([currentAction]),
      onPost: (path, body) => {
        if (path === `/api/v1/incidents/${incident.id}/actions/${action.id}/failed`) {
          expect(body).toEqual({ decision: "rejected", reason: "The account could not be reached." });
          currentAction = {
            ...action,
            status: "failed",
            blocked_reason: "The account could not be reached.",
          };
          return jsonResponse(currentAction);
        }
        return undefined;
      },
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BreachConsole />);
    await selectIncident(incident.reference);
    await userEvent.click(await screen.findByRole("button", { name: "response" }));
    await screen.findByText(action.title);

    await userEvent.click(screen.getByRole("button", { name: "Couldn't complete it" }));
    await userEvent.type(
      screen.getByLabelText("What went wrong"),
      "The account could not be reached.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Record as not completed" }));

    await waitFor(() => expect(screen.getByText("Recorded as not completed.")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("The account could not be reached.")).toBeInTheDocument());

    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          url === `${BASE_URL}/api/v1/incidents/${incident.id}/actions/${action.id}/failed` &&
          init?.method === "POST",
      ),
    ).toBe(true);
  });

  it("the submit button stays disabled (required reason) until something is typed", async () => {
    const incident = makeIncident();
    const action = makeAction({ status: "approved" });
    const { fetchMock } = makeRouter({ incidents: () => [incident], plan: () => emptyPlan([action]) });
    vi.stubGlobal("fetch", fetchMock);

    render(<BreachConsole />);
    await selectIncident(incident.reference);
    await userEvent.click(await screen.findByRole("button", { name: "response" }));
    await screen.findByText(action.title);

    await userEvent.click(screen.getByRole("button", { name: "Couldn't complete it" }));
    expect(screen.getByRole("button", { name: "Record as not completed" })).toBeDisabled();

    await userEvent.type(screen.getByLabelText("What went wrong"), "x");
    expect(screen.getByRole("button", { name: "Record as not completed" })).toBeEnabled();
  });

  it("shows the backend's error and leaves the action unchanged when the request is rejected", async () => {
    const incident = makeIncident();
    const action = makeAction({ status: "approved" });
    const { fetchMock } = makeRouter({
      incidents: () => [incident],
      plan: () => emptyPlan([action]),
      onPost: (path) =>
        path === `/api/v1/incidents/${incident.id}/actions/${action.id}/failed`
          ? jsonResponse({ detail: "action action-1 is already completed" }, 409)
          : undefined,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BreachConsole />);
    await selectIncident(incident.reference);
    await userEvent.click(await screen.findByRole("button", { name: "response" }));
    await screen.findByText(action.title);

    await userEvent.click(screen.getByRole("button", { name: "Couldn't complete it" }));
    await userEvent.type(screen.getByLabelText("What went wrong"), "Tried twice, no effect.");
    await userEvent.click(screen.getByRole("button", { name: "Record as not completed" }));

    await waitFor(() =>
      expect(screen.getByText(/action action-1 is already completed/)).toBeInTheDocument(),
    );
    expect(screen.queryByText("Recorded as not completed.")).not.toBeInTheDocument();
    // The action is still shown as approved -- a rejected request never looks applied.
    const card = screen.getByText(action.title).closest(".finding-card") as HTMLElement;
    expect(within(card).getByText("approved")).toBeInTheDocument();
  });

  it("disables the form while the request is in flight, preventing a duplicate submit", async () => {
    const incident = makeIncident();
    const action = makeAction({ status: "approved" });
    let resolvePost!: (v: FetchResponse) => void;
    const pending = new Promise<FetchResponse>((resolve) => {
      resolvePost = resolve;
    });
    const { fetchMock } = makeRouter({
      incidents: () => [incident],
      plan: () => emptyPlan([action]),
      onPost: (path) =>
        path === `/api/v1/incidents/${incident.id}/actions/${action.id}/failed` ? pending : undefined,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BreachConsole />);
    await selectIncident(incident.reference);
    await userEvent.click(await screen.findByRole("button", { name: "response" }));
    await screen.findByText(action.title);

    await userEvent.click(screen.getByRole("button", { name: "Couldn't complete it" }));
    await userEvent.type(screen.getByLabelText("What went wrong"), "No effect observed.");
    await userEvent.click(screen.getByRole("button", { name: "Record as not completed" }));

    // The form collapses back to the toggle immediately, and the toggle itself is
    // disabled while the one in-flight request is outstanding.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Couldn't complete it" })).toBeDisabled(),
    );
    const callsBeforeResolve = fetchMock.mock.calls.filter(
      ([, init]) => init?.method === "POST",
    ).length;
    expect(callsBeforeResolve).toBe(1);

    resolvePost(jsonResponse({ ...action, status: "failed", blocked_reason: "No effect observed." }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Couldn't complete it" })).toBeEnabled(),
    );
    // Still exactly one POST -- nothing re-submitted while it was disabled.
    const callsAfter = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST").length;
    expect(callsAfter).toBe(1);
  });
});

describe("BreachConsole: analysis-progress polling", () => {
  it("polls while analysis runs and stops once it completes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status: Incident["status"] = "investigating";
    const { fetchMock, detailCallCount } = makeRouter({
      incidents: () => [makeIncident({ status })],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BreachConsole />);
    await selectIncident("INC-0001");
    expect(await screen.findByText(/Analysis in progress/)).toBeInTheDocument();
    const callsAfterSelect = detailCallCount.get("inc-1") ?? 0;

    // One poll tick while still in progress: one more detail fetch, still polling.
    await vi.advanceTimersByTimeAsync(3000);
    await waitFor(() => expect(detailCallCount.get("inc-1")).toBe(callsAfterSelect + 1));
    expect(screen.getByText(/Analysis in progress/)).toBeInTheDocument();

    // The backend finishes between ticks.
    status = "review_required";
    await vi.advanceTimersByTimeAsync(3000);
    await waitFor(() => expect(screen.queryByText(/Analysis in progress/)).not.toBeInTheDocument());

    const callsAtCompletion = detailCallCount.get("inc-1") ?? 0;
    // Advancing well past another interval must not add further polls -- the effect
    // tore its interval down once the status left the in-progress set.
    await vi.advanceTimersByTimeAsync(9000);
    expect(detailCallCount.get("inc-1")).toBe(callsAtCompletion);
  });

  it("stops polling once analysis reaches a failed status", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status: Incident["status"] = "investigating";
    const { fetchMock, detailCallCount } = makeRouter({
      incidents: () => [
        makeIncident({
          status,
          ...(status === "failed"
            ? { error_code: "INVESTIGATION_FAILED", error_detail: "connector unreachable" }
            : {}),
        }),
      ],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BreachConsole />);
    await selectIncident("INC-0001");
    await screen.findByText(/Analysis in progress/);

    status = "failed";
    await vi.advanceTimersByTimeAsync(3000);
    await waitFor(() => expect(screen.getByText("connector unreachable", { exact: false })).toBeInTheDocument());
    expect(screen.queryByText(/Analysis in progress/)).not.toBeInTheDocument();

    const callsAtFailure = detailCallCount.get("inc-1") ?? 0;
    await vi.advanceTimersByTimeAsync(9000);
    expect(detailCallCount.get("inc-1")).toBe(callsAtFailure);
  });

  it("stops polling the previous incident when a different one is selected", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const incidentA = makeIncident({ id: "inc-a", reference: "INC-A", status: "investigating" });
    const incidentB = makeIncident({ id: "inc-b", reference: "INC-B", status: "closed", is_terminal: true });
    const { fetchMock, detailCallCount } = makeRouter({ incidents: () => [incidentA, incidentB] });
    vi.stubGlobal("fetch", fetchMock);

    render(<BreachConsole />);
    await selectIncident("INC-A");
    await screen.findByText(/Analysis in progress/);

    await selectIncident("INC-B");
    await waitFor(() => expect(screen.queryByText(/Analysis in progress/)).not.toBeInTheDocument());
    const aCallsAtSwitch = detailCallCount.get("inc-a") ?? 0;

    // Enough time for several poll intervals: A's interval must be gone, not merely
    // quiet, so its call count never moves again.
    await vi.advanceTimersByTimeAsync(12000);
    expect(detailCallCount.get("inc-a")).toBe(aCallsAtSwitch);
  });

  it("stops polling on unmount instead of leaking a timer against a gone component", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const { fetchMock, detailCallCount } = makeRouter({
      incidents: () => [makeIncident({ status: "investigating" })],
    });
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(<BreachConsole />);
    await selectIncident("INC-0001");
    await screen.findByText(/Analysis in progress/);
    const callsBeforeUnmount = detailCallCount.get("inc-1") ?? 0;

    unmount();
    await vi.advanceTimersByTimeAsync(12000);

    expect(detailCallCount.get("inc-1")).toBe(callsBeforeUnmount);
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("never overlaps a poll request with one still in flight", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let resolveSlowPoll: (incident: Incident) => void = () => {};
    let pollCount = 0;
    const fetchMock = vi.fn((url: string) => {
      const path = url.replace(BASE_URL, "");
      if (path === "/api/v1/incidents") return Promise.resolve(jsonResponse([makeIncident({ status: "investigating" })]));
      if (path === "/api/v1/incidents/inc-1") {
        pollCount += 1;
        if (pollCount === 1) return Promise.resolve(jsonResponse(makeIncident({ status: "investigating" })));
        if (pollCount === 2) {
          // The first interval tick: hang until the test resolves it, simulating a
          // slow backend -- this is the request a second tick must not overlap.
          return new Promise<FetchResponse>((resolve) => {
            resolveSlowPoll = (incident) => resolve(jsonResponse(incident));
          });
        }
        // Anything after that -- including loadIncident's own follow-up fetch once
        // the slow poll resolves -- just reflects the now-current status.
        return Promise.resolve(jsonResponse(makeIncident({ status: "review_required" })));
      }
      if (path.endsWith("/evidence") || path.endsWith("/timeline") || path.endsWith("/communications") || path.endsWith("/audit")) {
        return Promise.resolve(jsonResponse([]));
      }
      if (path.endsWith("/impact")) {
        return Promise.resolve(jsonResponse({ systems: [], data_categories: [], subjects: [], total: { record_count: null, count_basis: "unknown" } }));
      }
      if (path.endsWith("/risk")) return Promise.resolve(jsonResponse({ current: null, history: [] }));
      if (path.endsWith("/actions")) return Promise.resolve(jsonResponse(emptyPlan()));
      if (path.endsWith("/report")) return Promise.resolve(jsonResponse(null));
      throw new Error(`unexpected fetch: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BreachConsole />);
    await selectIncident("INC-0001");
    await screen.findByText(/Analysis in progress/);
    expect(pollCount).toBe(1); // the initial select, not a poll tick yet

    // Two more interval periods elapse while the first poll request is still hung.
    await vi.advanceTimersByTimeAsync(3000);
    await vi.advanceTimersByTimeAsync(3000);
    expect(pollCount).toBe(2); // only ONE poll tick actually went out, not two

    resolveSlowPoll(makeIncident({ status: "review_required" }));
    await waitFor(() => expect(screen.queryByText(/Analysis in progress/)).not.toBeInTheDocument());
  });
});

describe("BreachConsole: duplicate analysis submissions", () => {
  it("disables Run analysis while analysis is already in progress", async () => {
    const incident = makeIncident({ status: "investigating" });
    const { fetchMock } = makeRouter({ incidents: () => [incident] });
    vi.stubGlobal("fetch", fetchMock);

    render(<BreachConsole />);
    await selectIncident(incident.reference);
    await screen.findByText(/Analysis in progress/);

    expect(screen.getByRole("button", { name: "Run analysis" })).toBeDisabled();
  });

  it("leaves Run analysis enabled once analysis is not running", async () => {
    const incident = makeIncident({ status: "review_required" });
    const { fetchMock } = makeRouter({ incidents: () => [incident] });
    vi.stubGlobal("fetch", fetchMock);

    render(<BreachConsole />);
    await selectIncident(incident.reference);
    await waitFor(() => expect(screen.getByRole("button", { name: "Run analysis" })).toBeEnabled());
  });
});
