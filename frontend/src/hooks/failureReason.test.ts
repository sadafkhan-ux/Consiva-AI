import { describe, expect, it } from "vitest";

import { deriveFailureReason } from "./useConsentScan";

describe("deriveFailureReason: what a scan that could not scan the site is called", () => {
  it("names a site that could not be reached", () => {
    expect(deriveFailureReason("Could not reach the site at all: https://www.nykaa.com/ closed the connection (net::ERR_HTTP2_PROTOCOL_ERROR).", null)).toBe("Page Unreachable");
  });

  it("names a robots.txt that forbids the scanner", () => {
    const msg = "The robots.txt of www.example.com does not allow the scanner to visit https://www.example.com/ (or any page found on it). Nothing was assessed.";
    expect(deriveFailureReason(msg, null)).toBe("Blocked by robots.txt");
  });

  it("names a robots.txt that could not be read, which is not the same as being forbidden", () => {
    const msg = "Could not read the robots.txt of www.example.com (it answered HTTP 503), so it cannot be determined which pages the site permits. Nothing was assessed.";
    expect(deriveFailureReason(msg, null)).toBe("robots.txt Unreadable");
  });

  it("gives no label to an error it does not recognise", () => {
    expect(deriveFailureReason("something else", null)).toBeNull();
  });
});
