import { describe, expect, it } from "vitest";

import type { DsrCase, DsrEvidence, DsrSearchRun } from "../api/dsr";
import {
  REQUEST_TYPE_OPTIONS,
  canDraftResponse,
  caseStatusLabel,
  identityLabel,
  maskEmail,
  needsTypeSelection,
  searchState,
  stepForCase,
  visibleEvidence,
  wrongCodeMessage,
} from "./dsrLogic";

function run(overrides: Partial<DsrSearchRun> = {}): DsrSearchRun {
  return {
    id: "r1", source: "crm", status: "completed", matches: 0, distinct_subjects: 0,
    tables_searched: [], error_code: null, error_detail: null, completed_at: null, ...overrides,
  };
}

function evidence(overrides: Partial<DsrEvidence> = {}): DsrEvidence {
  return {
    id: "e1", search_run_id: "r1", source: "crm", table: "t", matched_column: "email",
    identifier_kind: "email", match_type: "exact", confidence: 1, record_reference: {},
    record_snapshot: null, ropa_category: null, observed_at: null, ...overrides,
  };
}

function kase(overrides: Partial<DsrCase> = {}): DsrCase {
  return {
    id: "c", reference: "DSR-1", status: "classified", request_type: "access",
    classification_method: "deterministic", classification_confidence: 0.95, raw_request: "x",
    requester_email: "a@b.com", requester_phone: null, requester_reference: null,
    error_code: null, error_detail: null, identity_status: "pending", allowed_transitions: [],
    is_terminal: false,
    sla: { received_at: null, due_at: "2026-02-01T00:00:00Z", remaining_seconds: 1, overdue: false, breached: false, escalated: false, closed: false },
    created_at: null, closed_at: null, ...overrides,
  };
}

describe("searchState: a search outcome is read from the search runs, never from empty evidence alone", () => {
  it.each<[string, DsrSearchRun[], DsrEvidence[], string, string]>([
    ["no runs, not failed", [], [], "identity_verified", "not_run"],
    ["no runs and evidence present (nothing to attribute it to)", [], [evidence()], "identity_verified", "not_run"],
    ["no runs, case failed", [], [], "failed", "failed"],
    ["case is searching", [], [], "searching", "running"],
    ["a run still running", [run({ status: "running" })], [], "search_completed", "running"],
    ["a run still pending", [run({ status: "pending" })], [], "search_completed", "running"],
    ["every run failed", [run({ status: "failed" })], [], "failed", "failed"],
    ["one finished, one failed", [run(), run({ id: "r2", status: "failed" })], [], "search_completed", "partial"],
    ["finished with no records", [run({ status: "no_match" })], [], "search_completed", "no_match"],
    ["completed with zero evidence", [run()], [], "search_completed", "no_match"],
    ["completed with evidence", [run()], [evidence()], "search_completed", "found"],
    ["multiple matches", [run({ status: "multiple_matches" })], [evidence()], "review_required", "found"],
  ])("%s", (_name, runs, ev, caseStatus, expected) => {
    expect(searchState(runs, ev, caseStatus)).toBe(expected);
  });

  it("says 'no match' ONLY when a run finished and none failed", () => {
    const states = new Set<string>();
    for (const runs of [[], [run({ status: "failed" })], [run({ status: "running" })], [run(), run({ id: "r2", status: "failed" })]]) {
      states.add(searchState(runs, [], "search_completed"));
    }
    expect(states.has("no_match")).toBe(false);
  });
});

describe("visibleEvidence", () => {
  it("shows nothing when no search ran", () => {
    expect(visibleEvidence([], [evidence()])).toEqual([]);
  });
  it("shows only records that belong to a search this case has", () => {
    const mine = evidence({ id: "mine", search_run_id: "r1" });
    const stray = evidence({ id: "stray", search_run_id: "other-run" });
    expect(visibleEvidence([run()], [mine, stray]).map((e) => e.id)).toEqual(["mine"]);
  });
  it("keeps a record that carries no run id (older responses) when a run exists", () => {
    expect(visibleEvidence([run()], [evidence({ search_run_id: undefined })])).toHaveLength(1);
  });
});

describe("canDraftResponse mirrors the backend rule", () => {
  it("needs a verified requester and a finished search", () => {
    const verified = kase({ identity_status: "verified" });
    expect(canDraftResponse(verified, [])).toBe(false);
    expect(canDraftResponse(verified, [run({ status: "failed" })])).toBe(false);
    expect(canDraftResponse(verified, [run({ status: "running" })])).toBe(false);
    expect(canDraftResponse(verified, [run()])).toBe(true);
    expect(canDraftResponse(kase({ identity_status: "pending" }), [run()])).toBe(false);
    expect(canDraftResponse(kase({ identity_status: "manually_verified" }), [run({ status: "no_match" })])).toBe(true);
  });
  it("a complaint needs neither", () => {
    expect(canDraftResponse(kase({ request_type: "grievance", identity_status: "pending" }), [])).toBe(true);
  });
});

describe("needsTypeSelection", () => {
  it("asks when the type is unset or the classifier was unsure", () => {
    expect(needsTypeSelection(kase({ request_type: "other", classification_confidence: 0 }))).toBe(true);
    expect(needsTypeSelection(kase({ request_type: "unclassified" }))).toBe(true);
    expect(needsTypeSelection(kase({ request_type: "access", classification_confidence: 0.3 }))).toBe(true);
  });
  it("does not ask when the rules were confident, or when a person already chose", () => {
    expect(needsTypeSelection(kase())).toBe(false);
    expect(needsTypeSelection(kase({ request_type: "other", classification_method: "manual", classification_confidence: 1 }))).toBe(false);
  });
  it("does not ask for a finished case", () => {
    expect(needsTypeSelection(kase({ request_type: "other", classification_confidence: 0, is_terminal: true }))).toBe(false);
  });
  it("only offers types the backend implements", () => {
    expect(REQUEST_TYPE_OPTIONS.map((o) => o.value).sort()).toEqual(
      ["access", "correction", "deletion", "export", "grievance", "information", "withdraw_consent"],
    );
  });
});

describe("stepForCase: where the wizard resumes", () => {
  it.each<[Partial<DsrCase>, string]>([
    [{ identity_status: "pending", status: "classified" }, "verify"],
    [{ identity_status: "in_progress", status: "identity_pending" }, "verify"],
    [{ identity_status: "failed", status: "classified" }, "verify"],
    [{ identity_status: "verified", status: "searching" }, "searching"],
    [{ identity_status: "verified", status: "identity_verified" }, "data"],
    [{ identity_status: "verified", status: "search_completed" }, "data"],
    [{ identity_status: "verified", status: "review_required" }, "data"],
    [{ identity_status: "verified", status: "failed" }, "data"],
    [{ identity_status: "verified", status: "approval_required" }, "approval"],
    [{ identity_status: "manually_verified", status: "executing" }, "approval"],
    [{ identity_status: "verified", status: "response_pending" }, "result"],
    [{ identity_status: "verified", status: "completed", is_terminal: true }, "result"],
    [{ identity_status: "pending", status: "cancelled", is_terminal: true }, "result"],
  ])("%j -> %s", (overrides, expected) => {
    expect(stepForCase(kase(overrides))).toBe(expected);
  });
});

describe("labels", () => {
  it("names the statuses a person was seeing as raw codes", () => {
    expect(caseStatusLabel(kase({ status: "review_required", request_type: "other", classification_confidence: 0 }))).toBe("Needs review — request type unclear");
    expect(caseStatusLabel(kase({ status: "review_required", request_type: "grievance" }))).toBe("Complaint — a person needs to respond");
    expect(caseStatusLabel(kase({ status: "review_required" }))).toBe("Needs review");
    expect(caseStatusLabel(kase({ status: "failed", error_code: "SOURCE_NOT_AUTHORIZED" }))).toBe("Waiting for a data source to be connected");
    expect(caseStatusLabel(kase({ status: "failed", error_code: "ACTION_FAILED" }))).toBe("Failed — see the message below");
    expect(caseStatusLabel(kase({ status: "identity_verified" }))).toBe("Identity verified — ready to search");
  });
  it("never shows an underscore for a known status", () => {
    for (const s of ["received", "classified", "identity_pending", "identity_verified", "searching", "search_completed", "review_required", "approval_required", "approved", "executing", "execution_verified", "response_pending", "completed", "partially_completed", "rejected", "cancelled", "expired", "escalated", "failed"]) {
      expect(caseStatusLabel(kase({ status: s }))).not.toContain("_");
    }
  });
  it("describes identity in words", () => {
    expect(identityLabel(null)).toBe("Not verified");
    expect(identityLabel("pending")).toBe("Not verified");
    expect(identityLabel("failed")).toBe("Locked — too many wrong codes");
    expect(identityLabel("manually_verified")).toBe("Verified by a person");
  });
  it("masks a requester's email", () => {
    expect(maskEmail("person@example.com")).toBe("pe••••@example.com");
    expect(maskEmail("a@b.com")).toBe("a•••@b.com");
    expect(maskEmail(null)).toBe("—");
    expect(maskEmail("no-at-sign")).toBe("••••");
  });
  it("says how many attempts are left after a wrong code", () => {
    expect(wrongCodeMessage({ status: "in_progress", attempts_remaining: 4 })).toBe("That code didn't match. 4 attempts left.");
    expect(wrongCodeMessage({ status: "in_progress", attempts_remaining: 1 })).toBe("That code didn't match. 1 attempt left.");
    expect(wrongCodeMessage({ status: "failed", attempts_remaining: 0 })).toMatch(/locked/);
    expect(wrongCodeMessage({ status: "expired", attempts_remaining: null })).toMatch(/expired/);
  });
});
