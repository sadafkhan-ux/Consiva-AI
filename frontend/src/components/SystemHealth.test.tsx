import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/types";

const api = vi.hoisted(() => ({ overview: vi.fn(), runNow: vi.fn(), testEmail: vi.fn(), acknowledge: vi.fn() }));
vi.mock("../api/monitoring", async (orig) => ({ ...(await orig<typeof import("../api/monitoring")>()), monitoringApi: api }));

import { ago } from "../api/monitoring";
import { SystemHealth } from "./SystemHealth";

const NOW = "2026-11-13T12:00:00Z";
const alert = (o: object = {}) => ({
  id: "a1", source: "anthropic_api", severity: "critical", status: "open", title: "Claude API key rejected (HTTP 401)", detail: "Anthropic answered HTTP 401: invalid x-api-key",
  action: "Create a new key at console.anthropic.com", first_seen_at: new Date(Date.now() - 3 * 3600_000).toISOString(), last_seen_at: NOW, occurrences: 12, resolved_at: null,
  acknowledged_by: null, acknowledged_at: null, last_notified_at: null, notify_count: 0, notify_status: null, ...o,
});
const health = (o: object = {}) => ({ source: "database", label: "Database", status: "ok", detail: "The database answered.", latency_ms: 4, checked_at: new Date().toISOString(), last_ok_at: NOW, consecutive_failures: 0, ...o });
const overview = (o: object = {}) => ({
  monitoring_enabled: true, check_interval_seconds: 900, open_count: 0, health: [health()], alerts: [],
  email: { enabled: true, configured: true, problem: null, to: ["sadaf.khan@swaransoft.com"], cc: [] }, ...o,
});

describe("the System health screen", () => {
  beforeEach(() => { Object.values(api).forEach((f) => f.mockReset()); });

  it("says plainly when nothing needs attention", async () => {
    api.overview.mockResolvedValue(overview());
    render(<SystemHealth />);
    await waitFor(() => expect(screen.getByTestId("no-alerts")).toHaveTextContent("Nothing needs attention"));
    expect(screen.getByTestId("health-table")).toHaveTextContent("Database");
  });

  it("shows an open problem with what to do, how long, and how many emails went out", async () => {
    api.overview.mockResolvedValue(overview({ open_count: 1, alerts: [alert({ notify_count: 2 })], health: [health({ source: "anthropic_api", label: "Claude API key", status: "failing", detail: "Anthropic answered HTTP 401" })] }));
    render(<SystemHealth />);
    const card = await screen.findByTestId("alert-anthropic_api");
    expect(card).toHaveTextContent("Claude API key rejected (HTTP 401)");
    expect(card).toHaveTextContent("What to do: Create a new key at console.anthropic.com");
    expect(card).toHaveTextContent("seen in 12 checks");
    expect(card).toHaveTextContent("2 emails sent");
    expect(screen.getByTestId("health-table")).toHaveTextContent("Failing");
  });

  it("never claims emails are going out when the mail server is not set up", async () => {
    api.overview.mockResolvedValue(overview({ email: { enabled: true, configured: false, problem: "No mail server is set (ALERT_SMTP_HOST), so no email can be sent.", to: ["sadaf.khan@swaransoft.com"], cc: [] } }));
    render(<SystemHealth />);
    const banner = await screen.findByTestId("email-status");
    expect(banner).toHaveTextContent("Alert emails are not being sent");
    expect(banner).toHaveTextContent("No mail server is set");
    expect(screen.getByRole("button", { name: "Send test email" })).toBeDisabled();
  });

  it("names the recipient and the people in copy when email is set up", async () => {
    api.overview.mockResolvedValue(overview({ email: { enabled: true, configured: true, problem: null, to: ["sadaf.khan@swaransoft.com"], cc: ["boss@swaransoft.com"] } }));
    render(<SystemHealth />);
    const banner = await screen.findByTestId("email-status");
    expect(banner).toHaveTextContent("sadaf.khan@swaransoft.com");
    expect(banner).toHaveTextContent("boss@swaransoft.com");
  });

  it("acknowledges an alert and reloads", async () => {
    api.overview.mockResolvedValueOnce(overview({ open_count: 1, alerts: [alert()] })).mockResolvedValue(overview({ alerts: [alert({ status: "acknowledged" })] }));
    api.acknowledge.mockResolvedValue(alert({ status: "acknowledged" }));
    render(<SystemHealth />);
    fireEvent.click(await screen.findByRole("button", { name: /Acknowledge/ }));
    await waitFor(() => expect(api.acknowledge).toHaveBeenCalledWith("a1"));
    await waitFor(() => expect(screen.getByTestId("alert-anthropic_api")).toHaveTextContent("acknowledged"));
    expect(screen.queryByRole("button", { name: /Acknowledge/ })).toBeNull();     // an acknowledged alert stays visible but offers no second acknowledge
  });

  it("runs the checks now and shows the new result", async () => {
    api.overview.mockResolvedValue(overview());
    api.runNow.mockResolvedValue(overview({ health: [health({ label: "Claude API key", source: "anthropic_api", detail: "Claude accepted the key (HTTP 200)." })] }));
    render(<SystemHealth />);
    fireEvent.click(await screen.findByRole("button", { name: "Run checks now" }));
    await waitFor(() => expect(screen.getByTestId("health-table")).toHaveTextContent("Claude accepted the key"));
  });

  it("reports the real outcome of the test email, success or failure", async () => {
    api.overview.mockResolvedValue(overview());
    api.testEmail.mockResolvedValueOnce({ status: "sent", reason: null }).mockResolvedValueOnce({ status: "failed", reason: "SMTPAuthenticationError: 535 login failed" });
    render(<SystemHealth />);
    const button = await screen.findByRole("button", { name: "Send test email" });
    fireEvent.click(button);
    await waitFor(() => expect(screen.getByTestId("email-note")).toHaveTextContent("Test email sent"));
    fireEvent.click(button);
    await waitFor(() => expect(screen.getByTestId("email-note")).toHaveTextContent("535 login failed"));
  });

  it("tells a non-administrator the screen is not for them", async () => {
    api.overview.mockRejectedValue(new ApiError(403, "The system health screen is for administrators."));
    render(<SystemHealth />);
    await waitFor(() => expect(screen.getByTestId("system-error")).toHaveTextContent("for administrators"));
  });
});

describe("ago()", () => {
  const now = Date.parse("2026-11-13T12:00:00Z");
  it("reads in minutes, hours and days", () => {
    expect(ago("2026-11-13T11:59:40Z", now)).toBe("just now");
    expect(ago("2026-11-13T11:15:00Z", now)).toBe("45 minutes ago");
    expect(ago("2026-11-13T09:00:00Z", now)).toBe("3 hours ago");
    expect(ago("2026-11-10T12:00:00Z", now)).toBe("3 days ago");
    expect(ago(null, now)).toBe("never");
  });
});
