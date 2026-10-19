import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ThreeStateSection } from "./ThreeStateSection";
import { TrackersTable } from "./TrackersTable";

const t = (id: string, states: string[], page_url?: string | null) => ({
  id, script_src: `https://x${id}.example/t.js`, vendor: null, category: "marketing", source: "rule",
  consent_states: states, page_url,
});

describe("tracker page attribution", () => {
  it("names the page each state's trackers were recorded on, and never invents one", () => {
    const trackers = [
      t("aaaaaaaa1", ["pre_consent"], "https://masterclass.gignaati.com/"),
      t("bbbbbbbb2", ["post_accept", "post_reject"], "https://gignaati.com/"),
      t("cccccccc3", ["pre_consent"], null),
    ];
    render(<ThreeStateSection meta={{} as never} trackers={trackers as never} />);

    expect(screen.getByTestId("state-pages-pre_consent")).toHaveTextContent("masterclass.gignaati.com");
    expect(screen.getByTestId("state-pages-pre_consent")).not.toHaveTextContent(/^Trackers recorded on: gignaati/);
    expect(screen.getByTestId("state-pages-post_reject")).toHaveTextContent("gignaati.com");
  });

  it("the trackers table shows the page, or says it was not recorded", () => {
    render(<TrackersTable trackers={[t("aaaaaaaa1", ["pre_consent"], "https://masterclass.gignaati.com/x"), t("bbbbbbbb2", ["pre_consent"])] as never} />);

    expect(screen.getByText("masterclass.gignaati.com/x")).toBeInTheDocument();
    expect(screen.getByText("not recorded")).toBeInTheDocument();
  });
});
