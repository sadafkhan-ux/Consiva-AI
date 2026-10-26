import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ScanFailure } from "../api/types";
import { FailureNotice, OUTCOME_LABELS } from "./FailureNotice";

const failure = (over: Partial<ScanFailure> = {}): ScanFailure => ({
  category: "TLS_CERTIFICATE_EXPIRED", code: "TLS_CERTIFICATE_EXPIRED", title: "The site's security certificate has expired",
  explanation: "The site presented a TLS certificate that has expired.", retryable: false, transient: false, owner: "website_owner",
  next_action: "The site's owner must renew the certificate.", detail: "certificate has expired", url: "https://uat.consiva.ai/", ...over,
});

describe("what a failed or blocked scan says", () => {
  it("names the cause, who must act, whether a retry helps, the next step and the underlying error", () => {
    render(<FailureNotice failure={failure()} outcome="FAILED" />);
    const n = screen.getByTestId("failure-notice");
    expect(n).toHaveTextContent("Failed: The site's security certificate has expired.");
    expect(n).toHaveTextContent("Next step: The site's owner must renew the certificate.");
    expect(n).toHaveTextContent("The website's owner has to act");
    expect(n).toHaveTextContent("Scanning again without a change will not help");
    expect(n).toHaveTextContent("certificate has expired");
    expect(n).toHaveAttribute("role", "alert");
  });

  it("says plainly that it is not a compliance result", () => {
    render(<FailureNotice failure={failure()} outcome="BLOCKED" />);
    expect(screen.getByTestId("failure-notice")).toHaveTextContent("says nothing about the site's consent practices");
  });

  it("a transient failure says a retry can help, and nobody has to act", () => {
    render(<FailureNotice failure={failure({ category: "RATE_LIMITED", title: "The site is rate limiting the scanner", retryable: true, transient: true, owner: "none", detail: "" })} outcome="FAILED" />);
    const n = screen.getByTestId("failure-notice");
    expect(n).toHaveTextContent("Scanning again can help");
    expect(n).toHaveTextContent("Nobody has to change anything");
  });

  it("a partial scan is a warning, not an error, and says which outcome it is", () => {
    render(<FailureNotice failure={failure({ category: "PARTIAL_SCAN", title: "Some pages could not be assessed", retryable: true, owner: "none" })} outcome="PARTIALLY_COMPLETED" />);
    const n = screen.getByTestId("failure-notice");
    expect(n).toHaveTextContent("Partially completed: Some pages could not be assessed.");
    expect(n).toHaveAttribute("role", "status");
  });

  it("shows nothing when there is no failure", () => {
    const { container } = render(<FailureNotice failure={null} outcome="COMPLETED" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("has a label for every lifecycle word the backend sends", () => {
    for (const w of ["QUEUED", "RUNNING", "COMPLETED", "COMPLETED_WITH_WARNINGS", "PARTIALLY_COMPLETED", "BLOCKED", "FAILED", "CANCELLED"]) expect(OUTCOME_LABELS[w]).toBeTruthy();
  });
});
