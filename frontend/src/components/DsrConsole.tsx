import { useCallback, useEffect, useState, type KeyboardEvent } from "react";
import { getProfile } from "../api/auth";
import { DsrConfig } from "./DsrConfig";
import { DsrDashboard } from "./DsrDashboard";
import { DsrEmailFirst } from "./DsrEmailFirst";
import { dsrHash, parseDsrHash, writeHash, type DsrView } from "./dsrRoute";

/**
 * Agent 3's three views over the SAME cases and the same API.
 *
 * "New request" is the email-first flow a person is walked through. "All cases" is
 * the operator's list -- it is how you pick up a case that is mid-flight, waiting on
 * approval, or already closed. "Configuration" is what each data source is allowed
 * to do and how long rows must be kept before an erasure may touch them -- it was
 * previously API-only; see DsrConfig.tsx. None of the three is a separate system:
 * all read and write the same DSR state.
 *
 * The view and the open case live in the URL hash (see dsrRoute.ts) so a reload returns to the
 * same place instead of the first agent.
 */

const TABS: { id: DsrView; label: string }[] = [
  { id: "new", label: "New request" },
  { id: "cases", label: "All cases" },
  { id: "config", label: "Configuration" },
];

export function DsrConsole({ onOpenRopa }: { onOpenRopa?: () => void }) {
  const [route, setRoute] = useState(() => parseDsrHash(window.location.hash) ?? { view: "new" as DsrView, caseId: null });
  const isAdmin = getProfile()?.role === "admin";

  // Someone pasted or edited a DSR link while the console was open.
  useEffect(() => {
    function onHashChange() {
      const next = parseDsrHash(window.location.hash);
      if (next) setRoute(next);
    }
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const go = useCallback((view: DsrView, caseId: string | null = null) => {
    setRoute({ view, caseId });
    writeHash(dsrHash(view, caseId));
  }, []);

  // A screen reports which case it has open; the address follows without remounting the screen.
  const onCaseChange = useCallback((view: DsrView, caseId: string | null) => {
    setRoute((prev) => (prev.view === view && prev.caseId === caseId ? prev : { view, caseId }));
    writeHash(dsrHash(view, caseId));
  }, []);

  function onTabKeyDown(e: KeyboardEvent<HTMLButtonElement>, index: number) {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const next = TABS[(index + step + TABS.length) % TABS.length];
    go(next.id);
    document.getElementById(`dsr-tab-${next.id}`)?.focus();
  }

  const { view, caseId } = route;
  return (
    <div className="dsr-console">
      <div role="tablist" aria-label="DSR sections" className="tabs dsr-console-tabs">
        {TABS.map((t, i) => (
          <button
            key={t.id}
            id={`dsr-tab-${t.id}`}
            role="tab"
            type="button"
            aria-selected={view === t.id}
            aria-controls="dsr-panel"
            tabIndex={view === t.id ? 0 : -1}
            className={view === t.id ? "active" : ""}
            onClick={() => view !== t.id && go(t.id)}
            onKeyDown={(e) => onTabKeyDown(e, i)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id="dsr-panel" aria-labelledby={`dsr-tab-${view}`}>
        {view === "new" && (
          <DsrEmailFirst
            key="new"
            initialCaseId={caseId}
            onCaseChange={(id) => onCaseChange("new", id)}
            onOpenConfig={() => go("config")}
            onOpenCase={(id) => go("cases", id)}
          />
        )}
        {view === "cases" && (
          <DsrDashboard
            key="cases"
            initialCaseId={caseId}
            onCaseChange={(id) => onCaseChange("cases", id)}
            onOpenConfig={() => go("config")}
          />
        )}
        {view === "config" && <DsrConfig isAdmin={isAdmin} onOpenRopa={onOpenRopa} />}
      </div>
    </div>
  );
}
