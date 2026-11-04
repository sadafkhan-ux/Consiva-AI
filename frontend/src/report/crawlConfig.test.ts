import { describe, expect, it } from "vitest";
import { CRAWL_FIELDS, validateCrawlConfig } from "./crawlConfig";

describe("crawl configuration validation", () => {
  it("an empty form is valid and sets nothing: the server's defaults apply", () => {
    const v = validateCrawlConfig({});
    expect(v.ok).toBe(true);
    expect(v.value).toEqual({});
    expect(validateCrawlConfig({ max_depth: "  ", max_urls: "" }).value).toEqual({});
  });

  it("valid values become numbers", () => {
    const v = validateCrawlConfig({ max_depth: "3", max_urls: "500", concurrency: "4", per_domain_concurrency: "2", requests_per_second: "1.5", memory_budget_mb: "2048" });
    expect(v.ok).toBe(true);
    expect(v.value).toEqual({ max_depth: 3, max_urls: 500, concurrency: 4, per_domain_concurrency: 2, requests_per_second: 1.5, memory_budget_mb: 2048 });
  });

  it("depth 0 is allowed (the entered page only) and 0 memory means no budget", () => {
    expect(validateCrawlConfig({ max_depth: "0" }).value.max_depth).toBe(0);
    expect(validateCrawlConfig({ memory_budget_mb: "0" }).value.memory_budget_mb).toBe(0);
  });

  it.each([
    ["max_depth", "-1", /at least 0/],
    ["max_depth", "1001", /at most/],
    ["max_urls", "0", /at least 1/],
    ["max_urls", "10000001", /at most/],
    ["concurrency", "0", /at least 1/],
    ["concurrency", "257", /at most 256/],
    ["requests_per_second", "0", /at least 0.1/],
    ["requests_per_second", "1001", /at most/],
    ["memory_budget_mb", "-5", /at least 0/],
  ] as const)("%s = %s is refused", (key, raw, message) => {
    const v = validateCrawlConfig({ [key]: raw });
    expect(v.ok).toBe(false);
    expect(v.errors[key]).toMatch(message);
    expect(v.value[key]).toBeUndefined();
  });

  it.each(["abc", "1e", "NaN", "Infinity", "--3"])("a non-number (%s) is refused", (raw) => {
    expect(validateCrawlConfig({ max_urls: raw }).errors.max_urls).toBe("Enter a number.");
  });

  it("a whole-number field refuses a fraction, a rate accepts one", () => {
    expect(validateCrawlConfig({ max_depth: "2.5" }).errors.max_depth).toBe("Enter a whole number.");
    expect(validateCrawlConfig({ requests_per_second: "2.5" }).ok).toBe(true);
  });

  it("per-domain concurrency cannot be higher than overall concurrency", () => {
    const v = validateCrawlConfig({ concurrency: "2", per_domain_concurrency: "5" });
    expect(v.ok).toBe(false);
    expect(v.errors.per_domain_concurrency).toMatch(/higher than overall/);
    expect(v.value.per_domain_concurrency).toBeUndefined();
    expect(validateCrawlConfig({ concurrency: "5", per_domain_concurrency: "5" }).ok).toBe(true);
  });

  it("one bad field does not stop the others from being checked", () => {
    const v = validateCrawlConfig({ max_depth: "x", max_urls: "100" });
    expect(v.errors.max_depth).toBeTruthy();
    expect(v.value.max_urls).toBe(100);
  });

  it("every field the form offers is one the API accepts, with the same bounds", () => {
    expect(CRAWL_FIELDS.map((f) => f.key)).toEqual(["max_depth", "max_urls", "concurrency", "per_domain_concurrency", "requests_per_second", "memory_budget_mb"]);
    for (const f of CRAWL_FIELDS) expect(f.min).toBeLessThan(f.max);
  });
});
