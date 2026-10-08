import { useState } from "react";
import { getProfile } from "../api/auth";
import { DsrConfig } from "./DsrConfig";
import { DsrDashboard } from "./DsrDashboard";
import { DsrEmailFirst } from "./DsrEmailFirst";

/**
 * Agent 3's three views over the SAME cases and the same API.
 *
 * "New request" is the email-first flow a person is walked through. "All cases" is
 * the operator's list -- it is how you pick up a case that is mid-flight, waiting on
 * approval, or already closed. "Configuration" is what each data source is allowed
 * to do and how long rows must be kept before an erasure may touch them -- it was
 * previously API-only; see DsrConfig.tsx. None of the three is a separate system:
 * all read and write the same DSR state.
 */
export function DsrConsole() {
  const [view, setView] = useState<"new" | "cases" | "config">("new");
  const isAdmin = getProfile()?.role === "admin";
  return (
    <div className="dsr-console">
      <nav className="tabs dsr-console-tabs">
        <button className={view === "new" ? "active" : ""} onClick={() => setView("new")}>
          New request
        </button>
        <button className={view === "cases" ? "active" : ""} onClick={() => setView("cases")}>
          All cases
        </button>
        <button className={view === "config" ? "active" : ""} onClick={() => setView("config")}>
          Configuration
        </button>
      </nav>
      {view === "new" && <DsrEmailFirst />}
      {view === "cases" && <DsrDashboard />}
      {view === "config" && <DsrConfig isAdmin={isAdmin} />}
    </div>
  );
}
