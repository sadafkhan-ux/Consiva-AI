import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { evidenceStatus } from "../hooks/useConsentScan";
import type { EvidenceTrackerItem } from "../api/types";
import { CookiesTable } from "./CookiesTable";
import { FormsSection, PoliciesSection } from "./FormsAndPoliciesSection";
import { TrackersTable } from "./TrackersTable";

const tracker = (over: Partial<EvidenceTrackerItem> = {}): EvidenceTrackerItem => ({
  id: "t1", script_src: "https://cdn.example.net/a.js", category: "analytics", vendor: null, consent_states: [], page_url: "https://x.test/", ...over,
}) as EvidenceTrackerItem;

describe("evidenceStatus: loading, loaded, or could not be loaded", () => {
  it("is loading until the evidence has been read or the read has failed", () => {
    expect(evidenceStatus({ evidence: null })).toBe("loading");
    expect(evidenceStatus({ evidence: null, evidenceFailed: false })).toBe("loading");
  });
  it("is unavailable when reading it failed and nothing earlier is held", () => {
    expect(evidenceStatus({ evidence: null, evidenceFailed: true })).toBe("unavailable");
  });
  it("is loaded once there is evidence, even an empty one", () => {
    expect(evidenceStatus({ evidence: { trackers: [] } as never })).toBe("loaded");
  });
});

describe("the evidence lists never say 'none detected' before the evidence is known", () => {
  it.each(["loading", "unavailable"] as const)("while %s, no list claims nothing was detected", (status) => {
    render(<><TrackersTable trackers={[]} status={status} /><CookiesTable cookies={[]} status={status} /><FormsSection forms={[]} status={status} /><PoliciesSection policies={[]} status={status} /></>);
    for (const text of [/No trackers were detected/, /No cookies were detected/, /No forms were detected/, /No policy pages were discovered/]) {
      expect(screen.queryByText(text)).not.toBeInTheDocument();
    }
  });

  it("loading is a quiet status, not an error", () => {
    render(<TrackersTable trackers={[]} status="loading" />);
    expect(screen.getByTestId("evidence-loading")).toHaveTextContent("Loading trackers");
    expect(screen.queryByTestId("evidence-unavailable")).not.toBeInTheDocument();
  });

  it("unavailable says so and that it is not a clean result", () => {
    render(<CookiesTable cookies={[]} status="unavailable" />);
    expect(screen.getByTestId("evidence-unavailable")).toHaveTextContent("could not be loaded");
    expect(screen.getByTestId("evidence-unavailable")).toHaveTextContent("not a clean result");
  });

  it("loaded and genuinely empty says none were detected", () => {
    render(<><TrackersTable trackers={[]} status="loaded" /><CookiesTable cookies={[]} /></>);
    expect(screen.getByText("No trackers were detected on this scan.")).toBeInTheDocument();
    expect(screen.getByText("No cookies were detected on this scan.")).toBeInTheDocument();
  });

  it("loaded with trackers lists them", () => {
    render(<TrackersTable trackers={[tracker()]} status="loaded" />);
    expect(screen.getAllByText(/cdn\.example\.net/).length).toBeGreaterThan(0);
  });
});

describe("tracker risk", () => {
  const risk = (t: EvidenceTrackerItem) => {
    const { container, unmount } = render(<TrackersTable trackers={[t]} />);
    const text = container.querySelector("tbody td:last-child, tbody tr td .badge")?.textContent ?? "";
    const all = Array.from(container.querySelectorAll(".badge")).map((b) => b.textContent);
    unmount();
    return { text, all };
  };

  it.each([[null], ["other"], ["something-new"], [""]])("a tracker with category %j is Unknown risk, never low", (category) => {
    const { all } = risk(tracker({ category: category as never }));
    expect(all).toContain("Unknown risk");
    expect(all).not.toContain("low");
  });

  it("known categories keep their risk", () => {
    expect(risk(tracker({ category: "functional" })).all).toContain("low");
    expect(risk(tracker({ category: "analytics", consent_states: ["pre_consent"] })).all).toContain("high");
    expect(risk(tracker({ category: "marketing", consent_states: ["post_accept"] })).all).toContain("medium");
  });
});
