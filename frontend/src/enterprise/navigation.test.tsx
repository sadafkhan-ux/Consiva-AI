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

  it("has one Enterprise item, and it opens the Enterprise DPDP agent (owner's decision, 20 Nov 2026)", async () => {
    render(<App />);
    expect(screen.queryByRole("button", { name: "Enterprise DPDP" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Enterprise" }));
    expect(await screen.findByTestId("enterprise-workspace")).toBeInTheDocument();
    expect(window.location.hash).toBe("#/dpdp");
  });

  it("leaves Privacy Policy as the 5-page screen and the other items working", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Privacy Policy" }));
    expect(screen.getByTestId("consent-stub")).toHaveTextContent("five-page");
    fireEvent.click(screen.getByRole("button", { name: "Breach" }));
    expect(await screen.findByText("breach")).toBeInTheDocument();
  });

  it("an old #/enterprise address opens the Enterprise DPDP agent", async () => {
    window.history.replaceState(null, "", "/#/enterprise");
    render(<App />);
    expect(await screen.findByTestId("enterprise-workspace")).toBeInTheDocument();
  });

  it("reloads onto the new console from its address", () => {
    expect(agentFromHash("#/dpdp")).toBe("dpdp");
    expect(agentFromHash("#/dpdp/abc-123")).toBe("dpdp");
  });
});
