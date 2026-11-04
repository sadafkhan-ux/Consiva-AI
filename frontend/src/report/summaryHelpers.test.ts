import { describe, expect, it } from "vitest";
import { classificationOf, pagesGlance, provisionLines, shortLine, shortReason, stateWord } from "./summaryReport";

describe("the words the two-page report uses", () => {
  it("shortens a long line at a word, marked, and leaves a short one whole", () => {
    expect(shortLine("A privacy policy page was found.")).toBe("A privacy policy page was found.");
    const s = shortLine(`start ${"word ".repeat(80)}`);
    expect(s.length).toBeLessThanOrEqual(111);
    expect(s.endsWith("…")).toBe(true);
  });

  it("writes the Act and the Rules of a check as two short lines, naming each section once and dropping nothing", () => {
    expect(provisionLines("DPDP Act, 2023 s.6(1); DPDP Act, 2023 s.6(4); DPDP Act, 2023 s.6(1); DPDP Rules, 2025 r.3(c)(i)")).toEqual([{ label: "Act", text: "Section 6(1), 6(4)" }, { label: "Rules", text: "Rule 3(c)(i)" }]);
    expect(provisionLines("Act, Section 8(5); General duty")).toEqual([{ label: "Act", text: "Section 8(5)" }, { label: "", text: "General duty" }]);
    expect(provisionLines("")).toEqual([]);
  });

  it("writes the pages-scanned figure compactly, and only that figure", () => {
    expect(pagesGlance("5 (of 112 found)")).toBe("5 of 112");
    expect(pagesGlance("Not recorded")).toBe("Not recorded");
  });

  it("names the classifier's result and provider, and calls an unclassified cookie Not classified", () => {
    expect(classificationOf({ category: "analytics", vendor: "Google Analytics", provider: "Google Analytics" })).toBe("Analytics · Google Analytics");
    expect(classificationOf({ category: "Unclassified", vendor: "Not available", provider: "Not identified" })).toBe("Not classified");
    expect(classificationOf({ category: "functional", vendor: "Not available", provider: "Not identified" })).toBe("Functional");
    expect(stateWord("Not tested")).toBe("Not Tested");
    expect(stateWord("Set")).toBe("Set");
  });

  it("keeps the kind of page and where it was found, and says the entered address is always first", () => {
    expect(shortReason("Privacy or cookie page: ranked first, because it settles the notice and cookie checks. Selected from pages found in the sitemap.")).toBe("Privacy or cookie page · sitemap");
    expect(shortReason("Terms page: ranked after the privacy and cookie pages. Selected from pages found through a link on a scanned page.")).toBe("Terms page · link");
    expect(shortReason("The address that was entered: always scanned first.")).toBe("Entered address (always first)");
  });
});
