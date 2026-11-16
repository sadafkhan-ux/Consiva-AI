import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/types";
import { agentFromHash } from "./dsrRoute";

const api = vi.hoisted(() => ({ createScan: vi.fn(), status: vi.fn(), coverage: vi.fn(), cancel: vi.fn(), resume: vi.fn(), verificationToken: vi.fn(), verifyDomain: vi.fn() }));
vi.mock("../api/enterprise", async (orig) => {
  const real = await orig<typeof import("../api/enterprise")>();
  return { ...real, enterpriseApi: api };
});

import { EnterpriseConsole } from "./EnterpriseConsole";

const counts = (o: Partial<Record<string, number>> = {}) => ({ pending: 0, in_progress: 0, done: 0, failed: 0, excluded: 0, total: 0, ...o });
const status = (o: object = {}) => ({
  scan_id: "s1", website_url: "https://example.com", status: "completed", urls: counts({ done: 3, excluded: 1, total: 4 }), progress: 100,
  stop_reason: null, error: null, options: {}, created_at: "2026-11-12T10:00:00Z", started_at: null, completed_at: null, ...o,
});
const coverage = (items: object[] = []) => ({ scan_id: "s1", urls: counts(), items, total: items.length, limit: 50, offset: 0, note: "Discovered is not scanned." });

describe("the Enterprise console", () => {
  beforeEach(() => {
    Object.values(api).forEach((f) => f.mockReset());
    window.sessionStorage.clear();
  });

  it("is reachable from the address: #/enterprise names the Enterprise screen", () => {
    expect(agentFromHash("#/enterprise")).toBe("enterprise");
    expect(agentFromHash("#/consent")).toBe("consent");
  });

  it("will not start a scan until a URL is given and ownership is attested", () => {
    render(<EnterpriseConsole />);
    const start = screen.getByRole("button", { name: "Start full scan" });
    expect(start).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Website address"), { target: { value: "https://example.com" } });
    expect(start).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(start).toBeEnabled();
  });

  it("starts a scan with authorized=true and shows the coverage it reads back", async () => {
    api.createScan.mockResolvedValue({ scan_id: "s1", status: "queued", message: "queued", options: {} });
    api.status.mockResolvedValue(status());
    api.coverage.mockResolvedValue(coverage([
      { url: "https://example.com/", depth: 0, status: "done", discovery_sources: ["seed"], http_status: 200, status_reason: null, last_error: null },
      { url: "https://example.com/shop?color=red", depth: 1, status: "excluded", discovery_sources: ["link"], http_status: null, status_reason: "query_variant: the same page with filter or sort parameters; not crawled", last_error: null },
    ]));
    render(<EnterpriseConsole />);
    fireEvent.change(screen.getByLabelText("Website address"), { target: { value: " https://example.com " } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Start full scan" }));
    await waitFor(() => expect(screen.getByTestId("enterprise-counts")).toHaveTextContent("4 URLs found"));
    expect(api.createScan).toHaveBeenCalledWith("https://example.com", true);
    expect(screen.getByTestId("enterprise-counts")).toHaveTextContent("3 scanned");
    expect(screen.getAllByText("Scanned").length).toBeGreaterThan(0);        // the result pill (and the filter option)
    expect(screen.getAllByText("Not scanned").length).toBeGreaterThan(0);          // an excluded URL is never presented as scanned
    expect(screen.getByText(/query_variant/)).toBeInTheDocument();
    expect(screen.getByTestId("enterprise-state")).toHaveTextContent("completed");
  });

  it("shows a validation error as plain words, not as JSON", async () => {
    api.createScan.mockRejectedValue(new ApiError(422, JSON.stringify([{ type: "value_error", loc: ["body", "website_url"], msg: "Value error, website_url must include a full domain name, for example example.com" }])));
    render(<EnterpriseConsole />);
    fireEvent.change(screen.getByLabelText("Website address"), { target: { value: "localhost" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Start full scan" }));
    await waitFor(() => expect(screen.getByTestId("enterprise-error")).toHaveTextContent("website_url must include a full domain name, for example example.com"));
    expect(screen.getByTestId("enterprise-error").textContent).not.toMatch(/value_error|\[\{/);
  });

  it("claims no score and no finding: the Enterprise assessment is not built", () => {
    render(<EnterpriseConsole />);
    expect(document.body.textContent).toMatch(/does not score the site/);
    expect(document.body.textContent).not.toMatch(/\d+\s*\/\s*100/);
  });

  it("says plainly when the product is switched off on the server", async () => {
    api.createScan.mockRejectedValue(new ApiError(404, "Not found"));
    render(<EnterpriseConsole />);
    fireEvent.change(screen.getByLabelText("Website address"), { target: { value: "https://example.com" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Start full scan" }));
    await waitFor(() => expect(screen.getByTestId("enterprise-error")).toHaveTextContent("not enabled on this server"));
  });

  it("shows the server's reason when the caller may not scan (not an admin, domain not verified)", async () => {
    api.createScan.mockRejectedValue(new ApiError(403, "A full-site scan needs a domain that has passed DNS ownership verification."));
    render(<EnterpriseConsole />);
    fireEvent.change(screen.getByLabelText("Website address"), { target: { value: "https://example.com" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Start full scan" }));
    await waitFor(() => expect(screen.getByTestId("enterprise-error")).toHaveTextContent("DNS ownership verification"));
  });

  it("offers Cancel and Resume only while the scan can still change", async () => {
    window.sessionStorage.setItem("consiva.enterprise.scan", "s1");
    api.status.mockResolvedValue(status({ status: "running", progress: 40, urls: counts({ done: 2, pending: 3, total: 5 }) }));
    api.coverage.mockResolvedValue(coverage());
    render(<EnterpriseConsole />);
    await waitFor(() => expect(screen.getByTestId("enterprise-state")).toHaveTextContent("running"));
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.getByTestId("enterprise-counts")).toHaveTextContent("3 waiting");
  });

  it("offers neither once the scan is final", async () => {
    window.sessionStorage.setItem("consiva.enterprise.scan", "s1");
    api.status.mockResolvedValue(status());
    api.coverage.mockResolvedValue(coverage());
    render(<EnterpriseConsole />);
    await waitFor(() => expect(screen.getByTestId("enterprise-state")).toHaveTextContent("completed"));
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
  });

  it("when the domain is not verified, walks the person through the DNS record and verifies it", async () => {
    api.createScan.mockRejectedValue(new ApiError(403, "A full-site scan needs a domain that has passed DNS ownership verification."));
    api.verificationToken.mockResolvedValue({ domain: "consiva.ai", txt_record: "consiva-verify=abc123", instructions: "x" });
    api.verifyDomain.mockResolvedValue({ domain: "consiva.ai", verified_at: "2026-11-12T12:00:00Z" });
    render(<EnterpriseConsole />);
    fireEvent.change(screen.getByLabelText("Website address"), { target: { value: "https://www.consiva.ai/about" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Start full scan" }));
    await waitFor(() => expect(screen.getByTestId("enterprise-verify")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Get the DNS record" }));
    await waitFor(() => expect(screen.getByTestId("enterprise-txt")).toHaveTextContent("consiva-verify=abc123"));
    expect(api.verificationToken).toHaveBeenCalledWith("consiva.ai");            // scheme, www and path are dropped: the host a scan is filed under
    fireEvent.click(screen.getByRole("button", { name: "I have added it: verify" }));
    await waitFor(() => expect(screen.getByTestId("enterprise-verify-note")).toHaveTextContent("consiva.ai is verified"));
    expect(screen.queryByTestId("enterprise-error")).toBeNull();
  });

  it("shows why verification failed when the DNS record is not there yet", async () => {
    api.createScan.mockRejectedValue(new ApiError(403, "A full-site scan needs a domain that has passed DNS ownership verification."));
    api.verificationToken.mockResolvedValue({ domain: "consiva.ai", txt_record: "consiva-verify=abc123", instructions: "x" });
    api.verifyDomain.mockRejectedValue(new ApiError(403, "No TXT record with the expected value was found on consiva.ai."));
    render(<EnterpriseConsole />);
    fireEvent.change(screen.getByLabelText("Website address"), { target: { value: "consiva.ai" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Start full scan" }));
    await waitFor(() => screen.getByRole("button", { name: "Get the DNS record" }));
    fireEvent.click(screen.getByRole("button", { name: "Get the DNS record" }));
    await waitFor(() => screen.getByRole("button", { name: "I have added it: verify" }));
    fireEvent.click(screen.getByRole("button", { name: "I have added it: verify" }));
    await waitFor(() => expect(screen.getByTestId("enterprise-verify-note")).toHaveTextContent("No TXT record"));
  });
});
