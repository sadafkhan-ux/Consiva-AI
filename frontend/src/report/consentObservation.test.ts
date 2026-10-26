import { describe, expect, it } from "vitest";

import type { EvidenceConsentSignalItem, ScanEvidenceResponse, WebsiteScanMetadata } from "../api/types";
import { bannerFactText, bannerNotDisplayed, observedBannerFact } from "./consentObservation";
import { buildReportVisual } from "./reportVisual";

const signal = (over: Partial<EvidenceConsentSignalItem> = {}, evidence: Record<string, unknown> = {}): EvidenceConsentSignalItem => ({
  mechanism_type: "cmp", cmp_vendor: "Some CMP", has_reject_all: false, has_granular_choices: true,
  evidence: { banner_ui_observed: false, accept_interaction: "cmp_not_automatable", reject_interaction: "cmp_not_automatable", ...evidence }, ...over,
});

describe("a consent tool that was loaded but whose banner the scanner never saw", () => {
  it("is recognised, and only then", () => {
    expect(bannerNotDisplayed(signal())).toBe(true);
    expect(bannerNotDisplayed(signal({}, { banner_ui_observed: true }))).toBe(false);          // the banner was seen
    expect(bannerNotDisplayed(signal({}, { accept_interaction: "clicked" }))).toBe(false);      // a click proves it was on screen
    expect(bannerNotDisplayed(signal({}, { reject_interaction: "clicked" }))).toBe(false);
    expect(bannerNotDisplayed(signal({}, { banner_ui_observed: undefined }))).toBe(false);      // an older scan: unknown, not "not displayed"
    expect(bannerNotDisplayed(signal({ mechanism_type: "banner" }))).toBe(false);               // the rule is about consent tools
    expect(bannerNotDisplayed(null)).toBe(false);
  });

  it("has no observed reject-all or per-purpose choices, whatever an older scan recorded", () => {
    expect(observedBannerFact(signal(), false)).toBeNull();
    expect(observedBannerFact(signal(), true)).toBeNull();
    expect(observedBannerFact(signal({}, { banner_ui_observed: true }), false)).toBe(false);
    expect(observedBannerFact(signal({}, { banner_ui_observed: true }), true)).toBe(true);
  });

  it("is worded as not observed, never as Yes or No", () => {
    expect(bannerFactText(signal(), false)).toMatch(/^Not observed/);
    expect(bannerFactText(signal({}, { banner_ui_observed: true }), false)).toBe("No");
    expect(bannerFactText(signal({}, { banner_ui_observed: true }), true)).toBe("Yes");
    expect(bannerFactText(signal({}, { banner_ui_observed: true }), null)).toBe("Not recorded");
  });
});

describe("the report's 'privacy elements' for such a scan", () => {
  const evidence = (s: EvidenceConsentSignalItem) => ({ pages: [], forms: [], cookies: [], trackers: [], policies: [], third_party_services: [], consent_signals: [s] }) as unknown as ScanEvidenceResponse;
  const META = { consent_mechanism: "cmp", cmp_vendor: "Some CMP", accept_interaction: "cmp_not_automatable", reject_interaction: "cmp_not_automatable" } as unknown as WebsiteScanMetadata;

  it("does not say a banner was found, or that per-purpose choices are offered in it", () => {
    const v = buildReportVisual(evidence(signal()), META, null)!;
    const el = (t: string) => v.elements.find((e) => e.title === t)!;
    expect(el("Consent banner").status).toBe("unverified");
    expect(el("Per-purpose choices").status).toBe("unverified");
    expect(v.consent.banner[0]).toBe("Tool loaded, no banner shown");
  });

  it("still says so when the banner was displayed", () => {
    const v = buildReportVisual(evidence(signal({}, { banner_ui_observed: true })), META, null)!;
    expect(v.elements.find((e) => e.title === "Consent banner")!.status).toBe("found");
    expect(v.elements.find((e) => e.title === "Per-purpose choices")!.status).toBe("found");
    expect(v.consent.banner[0]).toBe("Banner found");
  });
});

describe("the backend's own reading is used when the API gives it", () => {
  it("banner_displayed from the evidence API decides, so the client never re-derives what the server already says", () => {
    expect(bannerNotDisplayed(signal({ banner_displayed: false }, { banner_ui_observed: true }))).toBe(true);
    expect(bannerNotDisplayed(signal({ banner_displayed: true }))).toBe(false);
    expect(bannerNotDisplayed(signal({ banner_displayed: null }))).toBe(false);
  });
});

describe("a consent tool whose script failed to load", () => {
  const failed = signal({}, { cmp_script_failure: { url: "https://api.consiva.ai/sdk.js", error: "net::ERR_CERT_DATE_INVALID" } });
  const META = { consent_mechanism: "cmp", cmp_vendor: "Consiva Consent SDK", accept_interaction: "cmp_not_automatable", reject_interaction: "cmp_not_automatable" } as unknown as WebsiteScanMetadata;
  const ev = (s: EvidenceConsentSignalItem) => ({ pages: [], forms: [], cookies: [], trackers: [], policies: [], third_party_services: [], consent_signals: [s] }) as unknown as ScanEvidenceResponse;

  it("is read, and ignored when absent or malformed", async () => {
    const { scriptFailure, scriptFailureNote } = await import("./consentObservation");
    expect(scriptFailure(failed)).toEqual({ url: "https://api.consiva.ai/sdk.js", error: "net::ERR_CERT_DATE_INVALID" });
    expect(scriptFailureNote(failed)).toContain("could not be loaded (net::ERR_CERT_DATE_INVALID)");
    for (const s of [signal(), signal({}, { cmp_script_failure: null }), signal({}, { cmp_script_failure: { error: "x" } }), null, undefined]) {
      expect(scriptFailure(s as never)).toBeNull();
      expect(scriptFailureNote(s as never)).toBe("");
    }
  });

  it("is named in the report's privacy elements", () => {
    const v = buildReportVisual(ev(failed), META, null)!;
    expect(v.elements.find((e) => e.title === "Consent banner")!.note).toContain("script failed to load: net::ERR_CERT_DATE_INVALID");
  });
});
