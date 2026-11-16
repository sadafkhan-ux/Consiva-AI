import ts from "typescript";
import { describe, expect, it } from "vitest";

/** A "${...}" inside an ordinary quoted string is printed as it is: the reader of the report saw `${page.onThe}` where the page name belonged (insights.ts, fixed).
 *  The source is parsed, so a template literal that merely contains quotes is not mistaken for one. */
const files = import.meta.glob(["./**/*.ts", "./**/*.tsx", "!./**/*.test.*"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;

describe("report text never prints a placeholder as it is", () => {
  it("reads the report sources", () => {
    expect(Object.keys(files).length).toBeGreaterThan(20);
  });

  it("has no ordinary quoted string containing ${...}", () => {
    const bad: string[] = [];
    for (const [file, source] of Object.entries(files)) {
      const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, file.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
      const visit = (n: ts.Node) => {
        if (ts.isStringLiteral(n) && /\$\{[^}]*\}/.test(n.text)) bad.push(`${file}:${sf.getLineAndCharacterOfPosition(n.getStart()).line + 1}`);
        ts.forEachChild(n, visit);
      };
      visit(sf);
    }
    expect(bad).toEqual([]);
  });
});
