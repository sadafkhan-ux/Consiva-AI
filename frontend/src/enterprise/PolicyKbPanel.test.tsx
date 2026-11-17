import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { policyKb, type PolicyDocument, type PolicyKbMetrics } from "../api/enterprise";
import { PolicyKbPanel } from "./PolicyKbPanel";

const metrics = (over: Partial<PolicyKbMetrics> = {}): PolicyKbMetrics => ({
  websites_monitored: 1, documents: { active: 2, removed: 0, stale: 1, versions: 3 },
  sync: { new: 2, changed: 1, unchanged: 4, removed: 0, url_changed: 0, extraction_failed: 1, sync_failed: 0, marked_stale: 0, awaiting_processing: 0, failed_versions: 0, partial_extractions: 0, last_successful_sync: "2026-11-16T10:00:00Z" },
  embeddings: { chunks: 20, active_chunks: 18, generated: 12, reused: 8, missing: 0 }, retrieval: { queries: 5, keyword_only_fallbacks: 0 },
  llm: { passages_sent_to_model: 10, calls_saved: 30, characters_sent: 4000, characters_not_sent: 12000, tokens_sent_estimate: 1000, tokens_saved_estimate: 3000, token_figures_are: "estimates (characters / 4)" }, ...over,
});
const doc = (over: Partial<PolicyDocument>): PolicyDocument => ({
  id: "d1", domain: "acme.test", url: "https://acme.test/privacy", doc_type: "privacy_policy", status: "active", stale: false, stale_reason: null, current_version: 2, versions: 2, title: "Privacy Policy",
  text_chars: 9000, chunk_count: 7, extraction_status: "complete", extraction_note: null, sync_status: "ready", last_verified_at: "2026-11-16T10:00:00Z", extracted_at: "2026-11-16T10:00:00Z", ...over,
});

afterEach(() => vi.restoreAllMocks());

describe("PolicyKbPanel", () => {
  it("shows figures from the backend and each document's state, saying why a stale one is not current", async () => {
    vi.spyOn(policyKb, "metrics").mockResolvedValue(metrics());
    vi.spyOn(policyKb, "documents").mockResolvedValue({ total: 2, items: [doc({}), doc({ id: "d2", url: "https://acme.test/cookie-policy", doc_type: "cookie_policy", stale: true, stale_reason: "the page could not be read in the latest scan; the last verified version is kept" })] });
    render(<PolicyKbPanel />);
    await waitFor(() => expect(screen.getByTestId("policy-kb")).toBeInTheDocument());
    expect(screen.getByText("Model calls saved").nextSibling).toHaveTextContent("30");
    expect(screen.getByText(/last verified version is kept/)).toBeInTheDocument();
    expect(screen.getByText("Not re-verified")).toBeInTheDocument();
    expect(screen.getByText(/estimates \(characters \/ 4\)/)).toBeInTheDocument();
  });

  it("says plainly when nothing is stored yet", async () => {
    vi.spyOn(policyKb, "metrics").mockResolvedValue(metrics({ documents: { active: 0, removed: 0, stale: 0, versions: 0 } }));
    vi.spyOn(policyKb, "documents").mockResolvedValue({ total: 0, items: [] });
    render(<PolicyKbPanel />);
    await waitFor(() => expect(screen.getByTestId("kb-empty")).toBeInTheDocument());
  });
});
