import { describe, expect, it, vi } from "vitest";

import { friendlyError } from "./friendlyError";

const TECHNICAL = /\b(RAG|API|LLM|NVIDIA|HTTP|TLS|SSL|DNS|stack|exception|traceback|Playwright|Chromium|ERR_|ECONN|robots\.txt|timeout|stage|token|embedding|JSON)\b/i;

const CASES: [string, string][] = [
  ["The robots.txt of www.facebook.com does not allow the scanner to visit https://www.facebook.com", "Not allowed by the website"],
  ["Could not read the robots.txt of in.louisvuitton.com (APIRequestContext.get: Timeout 5000ms exceeded.)", "Couldn't check the website's rules"],
  ["Could not read the robots.txt of uat.consiva.ai (APIRequestContext.get: certificate has expired)", "Website security has expired"],
  ["net::ERR_CERT_AUTHORITY_INVALID at https://x.test/", "Website security problem"],
  ["Could not reach the site at all: the domain name of https://nosuch.example/ did not resolve on any of 4 attempts (net::ERR_NAME_NOT_RESOLVED)", "Website not found"],
  ["https://x.test/ closed the connection to the scanner on every one of 4 attempts (net::ERR_CONNECTION_RESET)", "Couldn't connect"],
  ["https://x.test/ kept answering with HTTP 429 (temporarily unavailable) after 6 attempts", "Website is limiting us"],
  ["https://x.test/ answered with HTTP 401 instead of a page", "Needs a login"],
  ["https://x.test/ answered with HTTP 403 instead of a page. The site is refusing the scanner (access denied)", "Blocked by the website"],
  ["https://x.test/ kept answering with HTTP 503 (temporarily unavailable) after 6 attempts", "Website had a problem"],
  ["https://x.test/ was too slow or unavailable over the whole retry window (10 attempts; Timeout 30000ms exceeded)", "Website too slow"],
  ["Scan of https://smartians.ai/ exceeded its 1800s budget and was aborted so the worker could continue with other jobs.", "Scan took too long"],
  ["Scan rate limit reached (50/day for this org).", "Scan limit reached"],
  ["This organisation already has 20 scans waiting to run (limit 20).", "Scan limit reached"],
  ["The scan was cancelled.", "Scan stopped"],
  ["The service did not answer (502). It may be restarting. The scan may still be running or finished on the server.", "Service unavailable"],
  ["<html><body>502 Bad Gateway</body></html>", "Service unavailable"],
  ["Analysis failed at stage \"rag_retrieval\": embedding request timed out", "Explanations unavailable"],
  ["LLM call failed: NVIDIA API returned an error", "Explanations unavailable"],
  ["Output validation failed: structured output did not match the schema", "Explanations unavailable"],
  ["Unsupported URL scheme: ftp", "Address can't be scanned"],
  ["Could not parse a domain from url: not-a-url", "Address can't be scanned"],
  ["Scanning requires an explicit authorization attestation for this domain.", "Confirmation needed"],
  ["Traceback (most recent call last): File app/x.py, line 3, in module ValueError: boom", "Something went wrong"],
  ["", "Something went wrong"],
];

describe("every raw error becomes a short, plain message with a next step", () => {
  for (const [raw, short] of CASES) {
    it(`${raw.slice(0, 60) || "(empty)"} -> ${short}`, () => {
      vi.spyOn(console, "warn").mockImplementation(() => undefined);
      const f = friendlyError(raw);
      expect(f.short).toBe(short);
      for (const text of [f.short, f.message, f.next]) {
        expect(text.length).toBeGreaterThan(5);
        expect(text.length).toBeLessThanOrEqual(140);
        expect(text).not.toMatch(TECHNICAL);
      }
      expect(f.message.endsWith(".")).toBe(true);
      expect(f.next.endsWith(".")).toBe(true);
    });
  }

  it("the server's own plain description is used when it has one", () => {
    const f = friendlyError("raw", { user_message: "Plain.", what_you_can_do: "Do this.", short: "Short", category: "X" } as never);
    expect(f).toEqual({ short: "Short", message: "Plain.", next: "Do this." });
  });

  it("the raw text goes to the developer console, not into the message", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const f = friendlyError("net::ERR_CERT_DATE_INVALID at https://x.test/");
    expect(warn).toHaveBeenCalled();
    expect(f.message).not.toContain("ERR_CERT");
  });
});
