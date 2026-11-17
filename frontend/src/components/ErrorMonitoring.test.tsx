import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/types";

const api = vi.hoisted(() => ({ errorSummary: vi.fn(), errors: vi.fn(), errorAcknowledge: vi.fn(), errorResolve: vi.fn() }));
vi.mock("../api/monitoring", async (orig) => ({ ...(await orig<typeof import("../api/monitoring")>()), monitoringApi: api }));

import { ErrorMonitoring } from "./ErrorMonitoring";

const ev = (o: object = {}) => ({
  id: "e1", error_id: "ERR-20261116-AAAAAA", agent: "enterprise", module: "crawl.runner", service: "crawler", grouping: "website_access", category: "bot_blocked", severity: "warning", http_status: 403,
  message: "Website blocked our scanner.", detail: "Cloudflare challenge", scan_id: "s1", website_url: "https://example.test", request_id: "r1", temporary: false, suggested_steps: "Ask the site owner to allow the scanner.",
  status: "open", occurrences: 3, first_seen_at: new Date().toISOString(), last_seen_at: new Date().toISOString(), resolved_at: null, last_recovery_at: null, acknowledged_by: null, notify_status: "sent", notify_count: 1, ...o,
});
const summary = (o: object = {}) => ({ total: 4, critical: 1, api: 2, rag: 1, database: 0, bot_blocked: 1, open: 2, acknowledged: 0, resolved: 2, latest_error_at: new Date().toISOString(), last_recovery_at: null, latest: null, ...o });

describe("Error monitoring", () => {
  beforeEach(() => { Object.values(api).forEach((f) => f.mockReset()); });

  it("shows the counts and an event with its details", async () => {
    api.errorSummary.mockResolvedValue(summary());
    api.errors.mockResolvedValue({ items: [ev()], total: 1, offset: 0, limit: 25 });
    render(<ErrorMonitoring />);
    expect(await screen.findByTestId("err-total")).toHaveTextContent("4");
    expect(screen.getByTestId("err-blocked")).toHaveTextContent("1");
    expect(screen.getByTestId("error-ERR-20261116-AAAAAA")).toHaveTextContent("Website blocked our scanner.");
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(screen.getByTestId("error-detail-ERR-20261116-AAAAAA")).toHaveTextContent("Ask the site owner to allow the scanner.");
  });

  it("acknowledges an open event and reloads", async () => {
    api.errorSummary.mockResolvedValue(summary());
    api.errors.mockResolvedValue({ items: [ev()], total: 1, offset: 0, limit: 25 });
    api.errorAcknowledge.mockResolvedValue(ev({ status: "acknowledged" }));
    render(<ErrorMonitoring />);
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    fireEvent.click(screen.getByRole("button", { name: /Acknowledge/ }));
    await waitFor(() => expect(api.errorAcknowledge).toHaveBeenCalledWith("ERR-20261116-AAAAAA"));
  });

  it("passes the filters to the server", async () => {
    api.errorSummary.mockResolvedValue(summary());
    api.errors.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 25 });
    render(<ErrorMonitoring />);
    await screen.findByTestId("no-errors");
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "five_page" } });
    await waitFor(() => expect(api.errors).toHaveBeenLastCalledWith(expect.objectContaining({ agent: "five_page" })));
  });

  it("says plainly when the screen is not for this user", async () => {
    api.errorSummary.mockRejectedValue(new ApiError(403, "Admin only"));
    api.errors.mockRejectedValue(new ApiError(403, "Admin only"));
    render(<ErrorMonitoring />);
    expect(await screen.findByTestId("errors-error")).toHaveTextContent("administrators");
  });
});
