import { describe, expect, it } from "vitest";

import { render, screen } from "@testing-library/react";
import { createElement } from "react";

import { ThreeStateSection } from "../components/ThreeStateSection";
import { buildReportVisual } from "./reportVisual";
import { countByState } from "./stateCounts";

const tracker = (id: string, category: string | null, states: string[]) => ({
  id, script_src: `https://svc${id}.example/t.js`, vendor: null, category, source: "rule", consent_states: states,
});
const cookie = (id: string, states: string[]) => ({
  id, name: `c${id}`, domain: "x.test", category: "analytics", vendor: "Same Vendor", is_first_party: true, source: "rule", consent_states: states,
});

// Mirrors the stored gignaati.com scan: 13 distinct addresses, 5 analytics or marketing, 2 cookies from one vendor.
const trackers = [
  ...["1", "2", "3", "4", "5"].map((i) => tracker(i, "functional", i === "1" ? ["pre_consent", "post_accept", "post_reject"] : ["pre_consent", "post_accept"])),
  tracker("6", "functional", ["post_accept"]),
  tracker("7", "functional", ["post_accept", "post_reject"]),
  tracker("8", "functional", ["post_accept", "post_reject"]),
  tracker("9", "analytics", ["post_accept"]),
  tracker("10", "analytics", ["post_accept"]),
  tracker("11", "analytics", ["post_accept"]),
  tracker("12", "marketing", ["post_accept"]),
  tracker("13", "marketing", ["post_accept"]),
];
const cookies = [cookie("1", ["post_accept"]), cookie("2", ["post_accept"])];

describe("one counting method", () => {
  it("counts evidence rows per state, a row in several states counting in each", () => {
    const c = countByState(cookies as never, trackers as never);
    expect(c.requests).toEqual({ pre_consent: 5, post_accept: 13, post_reject: 3 });
    expect(c.knownRequests).toEqual({ pre_consent: 0, post_accept: 5, post_reject: 0 });
    // two cookies of one vendor are two cookies, not one vendor
    expect(c.cookies).toEqual({ pre_consent: 0, post_accept: 2, post_reject: 0 });
  });

  it("gives the dashboard cards and the report the same numbers", () => {
    const evidence = { pages: [], forms: [], cookies, trackers, third_party_services: [], policies: [], consent_signals: [] };
    const visual = buildReportVisual(evidence as never, { accept_interaction: "clicked", reject_interaction: "clicked" } as never, null)!;
    const counted = countByState(cookies as never, trackers as never);
    expect(visual.consent.all).toEqual([counted.requests.pre_consent, counted.requests.post_accept, counted.requests.post_reject]);
    expect(visual.consent.known).toEqual([counted.knownRequests.pre_consent, counted.knownRequests.post_accept, counted.knownRequests.post_reject]);
    expect(visual.consent.cookies).toEqual([counted.cookies.pre_consent, counted.cookies.post_accept, counted.cookies.post_reject]);

    // the stored summary says vendors (3, 1): the cards must ignore it when the evidence rows are loaded
    const meta = { accept_interaction: "clicked", reject_interaction: "clicked",
      trackers_by_consent_state: { post_accept: 3 }, cookies_by_consent_state: { post_accept: 1 } };
    render(createElement(ThreeStateSection, { meta: meta as never, trackers: trackers as never, cookies: cookies as never }));
    const card = screen.getByText("After Accept").closest(".state-card") as HTMLElement;
    expect(card).toHaveTextContent("13 third-party requests");
    expect(card).toHaveTextContent("2 cookies");
    expect(card).toHaveTextContent("5 analytics or marketing");
    expect(screen.getByTestId("state-unit-note")).toHaveTextContent("one distinct address");
  });
});
