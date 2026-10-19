import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConsentAgentView } from "./ConsentAgentView";
import { analysisStagesAreDone, scanEvidenceUnavailable } from "./hooks/useConsentScan";

function jsonResponse(body: unknown, status = 200) {
  return { status, ok: status < 400, headers: { get: () => "application/json" }, json: async () => body };
}

const COUNTS = { pages: 0, forms: 0, cookies: 0, trackers: 0, third_party_services: 0, policies: 0 };
const TIMEOUT_ERROR =
  "Scan of https://smartians.ai/ exceeded its 1800s budget and was aborted so the worker could continue with other jobs.";

beforeEach(() => {
  sessionStorage.setItem("consiva.access_token", "test-token");
});

afterEach(() => {
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

/** A backend whose scan fails, reporting `pages` pages collected before it did. */
function failingBackend(pages: number) {
  const counts = { ...COUNTS, pages };
  return vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.endsWith("/api/v1/consent/scans") && method === "POST") {
      return Promise.resolve(jsonResponse({ id: "b4569e0b-aaaa", url: "https://smartians.ai/", status: "queued" }, 202));
    }
    if (url.endsWith("/stages")) {
      return Promise.resolve(jsonResponse([
        { stage: "url_validation", status: "completed", duration_ms: 18, error: null, metadata: {} },
        { stage: "website_scan", status: "failed", duration_ms: 330_090, error: TIMEOUT_ERROR, metadata: {} },
      ]));
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve(jsonResponse({
        pages: [], forms: [], cookies: [], trackers: [], third_party_services: [], policies: [], consent_signals: [],
      }));
    }
    if (url.endsWith("/findings") || url.endsWith("/audit")) return Promise.resolve(jsonResponse([]));
    if (url.includes("/api/v1/consent/scans/b4569e0b-aaaa")) {
      return Promise.resolve(jsonResponse({
        id: "b4569e0b-aaaa", url: "https://smartians.ai/", status: "failed", error: TIMEOUT_ERROR, evidence_counts: counts,
      }));
    }
    throw new Error(`unexpected fetch: ${url} ${method}`);
  });
}

async function scanAndWaitForFailure(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock);
  render(<ConsentAgentView />);
  await userEvent.clear(screen.getByPlaceholderText("https://example.com"));
  await userEvent.type(screen.getByPlaceholderText("https://example.com"), "https://smartians.ai/");
  await userEvent.click(screen.getByRole("button", { name: "Scan Website" }));
  await waitFor(() => expect(screen.getByText("Failed")).toBeInTheDocument());
}

describe("a failed scan that collected nothing is not shown as a clean result", () => {
  it("says it was not scanned, names the failing stage, and shows no zero counts or 'none detected'", async () => {
    await scanAndWaitForFailure(failingBackend(0));

    const notice = screen.getByTestId("not-scanned");
    expect(notice).toHaveTextContent("Not scanned — no result");
    expect(notice).toHaveTextContent('failed at the "website_scan" stage');
    expect(notice).toHaveTextContent("this is not a clean result");

    // None of the sections that would read as a verified clean result are on the page.
    for (const text of [
      /No trackers were detected on this scan/,
      /No cookies were detected on this scan/,
      /No forms were detected on this scan/,
      /No policy pages were discovered/,
      /No findings for this scan/,
      /0 cookies/,
      /0 trackers/,
    ]) {
      expect(screen.queryByText(text)).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("heading", { name: /4\. Trackers/ })).not.toBeInTheDocument();
  });

  it("still shows the reason and the failing stage in the diagnostics", async () => {
    await scanAndWaitForFailure(failingBackend(0));

    expect(screen.getAllByText(new RegExp(TIMEOUT_ERROR.slice(0, 40))).length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: /Errors \/ Diagnostics/ })).toBeInTheDocument();
    expect(screen.getByText("Pages Scanned").previousSibling).toHaveTextContent("0");
  });

  it("a scan that failed AFTER collecting pages keeps its real evidence sections", async () => {
    await scanAndWaitForFailure(failingBackend(3));

    expect(screen.queryByTestId("not-scanned")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /4\. Trackers/ })).toBeInTheDocument();
  });
});

describe("scanEvidenceUnavailable", () => {
  const scan = (pages: number | null) =>
    pages === null ? null : ({ evidence_counts: { ...COUNTS, pages } } as never);

  it.each([
    ["failed, no pages", "failed", 0, true],
    ["failed before the scan row could be read", "failed", null, true],
    ["failed after 3 pages were collected", "failed", 3, false],
    ["completed", "completed", 5, false],
    ["completed with zero pages recorded", "completed", 0, false],
    ["still scanning", "scanning", 0, false],
    ["degraded (findings exist, narrative failed)", "degraded", 4, false],
  ])("%s", (_name, phase, pages, expected) => {
    expect(scanEvidenceUnavailable({ phase: phase as never, scan: scan(pages), stages: [] })).toBe(expected);
  });
});

describe("analysisStagesAreDone: a failed retrieval or analysis is not the end of the run", () => {
  const stage = (name: string, status: string) => ({ stage: name, status, duration_ms: null, error: null, metadata: {} });

  it.each([
    ["rag_retrieval failed, fallback not yet written", [stage("rag_retrieval", "failed")], false],
    ["llm_analysis failed, fallback not yet written", [stage("llm_analysis", "failed")], false],
    ["rag_retrieval failed and the rule findings are written", [stage("rag_retrieval", "failed"), stage("rule_findings_generated", "completed")], true],
    ["a normal run reaches findings", [stage("findings_generated", "completed")], true],
    ["the audit is saved", [stage("audit_saved", "completed")], true],
    ["classification failed (no recovery path)", [stage("classification", "failed")], true],
    ["rules_check failed (no recovery path)", [stage("rules_check", "failed")], true],
    ["still running", [stage("rag_retrieval", "running")], false],
  ])("%s", (_name, stages, expected) => {
    expect(analysisStagesAreDone(stages as never)).toBe(expected);
  });
});
