import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ScanFailure } from "../api/types";
import { FailureNotice, OUTCOME_LABELS, SimpleErrorNotice } from "./FailureNotice";

const failure = (over: Partial<ScanFailure> = {}): ScanFailure => ({
  category: "TLS_CERTIFICATE_EXPIRED", code: "TLS_CERTIFICATE_EXPIRED", title: "The site's security certificate has expired",
  explanation: "The site presented a TLS certificate that has expired.", retryable: false, transient: false, owner: "website_owner",
  next_action: "The site's owner must renew the certificate.", detail: "APIRequestContext.get: certificate has expired", url: "https://uat.consiva.ai/",
  user_message: "This website's security certificate has expired, so we couldn't open it safely.", what_you_can_do: "The website's owner needs to renew it. Try again afterwards.", short: "Website security has expired", ...over,
});

describe("what a failed or blocked scan says", () => {
  it("says what happened in one plain sentence and what the person can do", () => {
    render(<FailureNotice failure={failure()} outcome="FAILED" />);
    const n = screen.getByTestId("failure-notice");
    expect(n).toHaveTextContent("This website's security certificate has expired, so we couldn't open it safely.");
    expect(n).toHaveTextContent("What you can do: The website's owner needs to renew it. Try again afterwards.");
    expect(n).toHaveAttribute("role", "alert");
  });

  it("shows no technical text: not the raw error, not the developer explanation, not a code", () => {
    render(<FailureNotice failure={failure()} outcome="FAILED" />);
    const text = screen.getByTestId("failure-notice").textContent ?? "";
    for (const technical of ["APIRequestContext", "TLS", "TLS_CERTIFICATE_EXPIRED", "owner must renew the certificate"]) expect(text).not.toContain(technical);
  });

  it("says it is about reaching the website, not about its privacy practices", () => {
    render(<FailureNotice failure={failure()} outcome="BLOCKED" />);
    expect(screen.getByTestId("failure-notice")).toHaveTextContent("not about its privacy practices");
  });

  it("a partial scan is a warning, not an error", () => {
    render(<FailureNotice failure={failure({ category: "PARTIAL_SCAN", user_message: "Some pages couldn't be scanned, so the results cover only the pages that were.", what_you_can_do: "Scan again to retry the others." })} outcome="PARTIALLY_COMPLETED" />);
    expect(screen.getByTestId("failure-notice")).toHaveAttribute("role", "status");
  });

  it("an older response without the plain fields still gets a plain message", () => {
    render(<FailureNotice failure={failure({ user_message: undefined, what_you_can_do: undefined })} outcome="FAILED" />);
    expect(screen.getByTestId("failure-notice")).toHaveTextContent("Something went wrong while scanning.");
  });

  it("shows nothing when there is no failure", () => {
    const { container } = render(<FailureNotice failure={null} outcome="COMPLETED" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("the plain-text box has the same shape", () => {
    render(<SimpleErrorNotice message="We couldn't find this website address." next="Check the spelling of the address and try again." />);
    expect(screen.getByTestId("friendly-error")).toHaveTextContent("What you can do: Check the spelling of the address and try again.");
  });

  it("has a label for every lifecycle word the backend sends", () => {
    for (const w of ["QUEUED", "RUNNING", "COMPLETED", "COMPLETED_WITH_WARNINGS", "PARTIALLY_COMPLETED", "BLOCKED", "FAILED", "CANCELLED"]) expect(OUTCOME_LABELS[w]).toBeTruthy();
  });
});
