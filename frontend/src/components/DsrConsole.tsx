import { useState } from "react";
import { DsrDashboard } from "./DsrDashboard";
import { DsrEmailFirst } from "./DsrEmailFirst";

/**
 * Agent 3's two views over the SAME cases and the same API.
 *
 * "New request" is the email-first flow a person is walked through. "All cases" is
 * the operator's list, unchanged -- it is how you pick up a case that is mid-flight,
 * waiting on approval, or already closed. Neither is a separate system: both read
 * and write the same DSR case.
 */
export function DsrConsole() {
  const [view, setView] = useState<"new" | "cases">("new");
  return (
    <div className="dsr-console">
      <nav className="tabs dsr-console-tabs">
        <button className={view === "new" ? "active" : ""} onClick={() => setView("new")}>
          New request
        </button>
        <button className={view === "cases" ? "active" : ""} onClick={() => setView("cases")}>
          All cases
        </button>
      </nav>
      {view === "new" ? <DsrEmailFirst /> : <DsrDashboard />}
    </div>
  );
}
