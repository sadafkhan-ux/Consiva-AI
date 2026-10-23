import { describe, expect, it } from "vitest";
import { formatRef, formatRefShort, formatRefs } from "./legalRefs";

describe("legal references are spelt one way", () => {
  it.each([
    ["DPDP Act, 2023 s.6(4)", "DPDP Act, 2023, Section 6(4)"],
    ["DPDP Act s.5(1)(i)", "DPDP Act, 2023, Section 5(1)(i)"],
    ["DPDP Rules, 2025 r.3(c)(i)", "DPDP Rules, 2025, Rule 3(c)(i)"],
    ["DPDP Rules r.14(3)", "DPDP Rules, 2025, Rule 14(3)"],
    ["DPDP Act s.13", "DPDP Act, 2023, Section 13"],
    ["DPDP Rules r.12, Fourth Schedule Part A items 1 and 2", "DPDP Rules, 2025, Rule 12, Fourth Schedule, Part A, items 1 and 2"],
    ["DPDP Rules r.8(1), r.8(2), Third Schedule item 1", "DPDP Rules, 2025, Rule 8(1), Rule 8(2), Third Schedule item 1"],
    ["DPDP Rules r.10, r.12, Fourth Schedule Part A item 3", "DPDP Rules, 2025, Rule 10, Rule 12, Fourth Schedule, Part A, item 3"],
    ["DPDP Act, 2023 Section 7", "DPDP Act, 2023, Section 7"],
  ])("%s -> %s", (raw, out) => expect(formatRef(raw)).toBe(out));

  it("is idempotent: a reference already in the full form is left as it is", () => {
    const once = formatRef("DPDP Act, 2023 s.6(4); DPDP Rules, 2025 r.3(c)(i)");
    expect(formatRef(once)).toBe(once);
    expect(once).toBe("DPDP Act, 2023, Section 6(4); DPDP Rules, 2025, Rule 3(c)(i)");
  });

  it("never changes which provision is named: every section and rule number survives", () => {
    const raw = ["DPDP Act, 2023 s.5(1)", "DPDP Act, 2023 s.6(1)", "DPDP Act, 2023 s.6(4)", "DPDP Rules, 2025 r.3(a)", "DPDP Rules, 2025 r.3(b)(ii)", "DPDP Rules r.14(1)(a)"];
    const nums = (t: string) => (t.match(/\d+(?:\([a-z0-9]+\))*/g) ?? []).filter((n) => n !== "2023" && n !== "2025");
    expect(nums(formatRefs(raw))).toEqual(nums(raw.join(" ")));
  });

  it("leaves text that is not a reference alone", () => {
    expect(formatRef("Whether the privacy notice is present")).toBe("Whether the privacy notice is present");
    expect(formatRef("")).toBe("");
  });

  it("has a short form for a narrow column", () => {
    expect(formatRefShort("DPDP Act, 2023 s.5(1); DPDP Rules, 2025 r.3(a)")).toBe("Act, Section 5(1); Rules, Rule 3(a)");
  });

  it("joins a list", () => {
    expect(formatRefs(["DPDP Act s.8(7)", "DPDP Rules r.14(3)"])).toBe("DPDP Act, 2023, Section 8(7) · DPDP Rules, 2025, Rule 14(3)");
  });
});
