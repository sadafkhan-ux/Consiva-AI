import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../ConsentAgentView", () => ({ ConsentAgentView: ({ wholeSite }: { wholeSite?: boolean }) => <div data-testid="consent-stub">{wholeSite ? "whole-site" : "five-page"}</div> }));
vi.mock("../components/BreachConsole", () => ({ BreachConsole: () => <div>breach</div> }));
vi.mock("../enterprise/EnterpriseWorkspace", () => ({ EnterpriseWorkspace: () => <div data-testid="enterprise-workspace">workspace</div> }));

import App from "../App";
import { agentFromHash } from "../components/dsrRoute";

describe("navigation", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.sessionStorage.setItem("consiva.access_token", "tok");
    window.sessionStorage.setItem("consiva.profile", JSON.stringify({ user_id: "u", org_id: "o", role: "member", email: "a@b.c" }));
    window.history.replaceState(null, "", "/");
  });
  afterEach(() => cleanup());

  it("has an Enterprise DPDP item that opens the new console, next to the existing Enterprise item", async () => {
    render(<App />);
    expect(screen.getByRole("button", { name: "Enterprise" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Enterprise DPDP" }));
    expect(await screen.findByTestId("enterprise-workspace")).toBeInTheDocument();
    expect(window.location.hash).toBe("#/dpdp");
  });

  it("leaves the existing items working: Enterprise is still the uncapped 5-page screen and Privacy Policy is the 5-page one", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Enterprise" }));
    expect(screen.getByTestId("consent-stub")).toHaveTextContent("whole-site");
    fireEvent.click(screen.getByRole("button", { name: "Privacy Policy" }));
    expect(screen.getByTestId("consent-stub")).toHaveTextContent("five-page");
    fireEvent.click(screen.getByRole("button", { name: "Breach" }));
    expect(await screen.findByText("breach")).toBeInTheDocument();
  });

  it("reloads onto the new console from its address", () => {
    expect(agentFromHash("#/dpdp")).toBe("dpdp");
    expect(agentFromHash("#/dpdp/abc-123")).toBe("dpdp");
    expect(agentFromHash("#/enterprise")).toBe("enterprise");
  });
});
