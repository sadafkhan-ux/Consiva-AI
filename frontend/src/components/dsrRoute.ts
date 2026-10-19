/**
 * Where the app is, kept in the URL hash so a reload lands on the same screen.
 *
 * The app has no router (the active agent was only React state, so a reload always went back to
 * Consent). The hash is the smallest thing consistent with that: no library, no server
 * configuration, and nothing in it but a screen name and a case id -- never a token or any
 * personal data. The case id is only a pointer: the case is re-read from the backend, with the
 * caller's own session, on every load, so a pasted link shows nothing to someone who may not see it.
 */

export type DsrView = "new" | "cases" | "config";

export interface DsrRoute {
  view: DsrView;
  caseId: string | null;
}

const AGENTS = ["consent", "ropa", "dsr", "breach", "regwatch", "purpose"] as const;
export type AgentId = (typeof AGENTS)[number];

/** The agent named by `#/<agent>...`, or null for no/unknown hash. */
export function agentFromHash(hash: string): AgentId | null {
  const m = /^#\/([a-z]+)(?:\/|$)/.exec(hash);
  return m && (AGENTS as readonly string[]).includes(m[1]) ? (m[1] as AgentId) : null;
}

/** The DSR screen named by `#/dsr[/<view>[/<case id>]]`, or null when the hash is not a DSR one. */
export function parseDsrHash(hash: string): DsrRoute | null {
  const m = /^#\/dsr(?:\/(new|cases|config)(?:\/([0-9A-Za-z-]{1,64}))?)?$/.exec(hash);
  if (!m) return null;
  return { view: (m[1] as DsrView | undefined) ?? "new", caseId: m[2] ?? null };
}

export function dsrHash(view: DsrView, caseId: string | null): string {
  return `#/dsr/${view}${caseId ? `/${caseId}` : ""}`;
}

/** Update the address without adding a history entry or firing hashchange. Never throws: a
 *  sandboxed frame that forbids it simply loses reload-restore. */
export function writeHash(hash: string): void {
  try {
    if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
  } catch {
    /* reload-restore is a convenience, not a requirement */
  }
}
