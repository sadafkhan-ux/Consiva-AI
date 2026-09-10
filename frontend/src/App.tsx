import { useEffect, useState } from "react";
import { getProfile, logout, type Profile } from "./api/auth";
import { setUnauthorizedHandler } from "./api/client";
import { ConsentAgentView } from "./ConsentAgentView";
import { LoginScreen } from "./components/LoginScreen";
import { RopaDashboard } from "./components/RopaDashboard";

type Agent = "consent" | "ropa";

/**
 * Shell around both agents. Agent 1's UI is unchanged -- it moved verbatim into
 * ConsentAgentView; this file only adds the sign-in gate and the agent switcher.
 */
export default function App() {
  const [profile, setProfile] = useState<Profile | null>(() => getProfile());
  const [agent, setAgent] = useState<Agent>("consent");

  useEffect(() => {
    // A 401 from any request means the token died mid-session; drop straight
    // back to the login screen instead of leaving a broken console on screen.
    setUnauthorizedHandler(() => setProfile(null));
    return () => setUnauthorizedHandler(null);
  }, []);

  if (!profile) return <LoginScreen onSignedIn={setProfile} />;

  function handleSignOut() {
    logout();
    setProfile(null);
  }

  return (
    <div className="app">
      <header className="app-header shell-header">
        <div>
          <div className="brand">Consiva AI</div>
          <div className="tagline">DPDP compliance console</div>
        </div>
        <div className="shell-session">
          <nav className="agent-switch">
            <button
              className={agent === "consent" ? "active" : ""}
              onClick={() => setAgent("consent")}
            >
              Consent Agent
            </button>
            <button
              className={agent === "ropa" ? "active" : ""}
              onClick={() => setAgent("ropa")}
            >
              ROPA Agent
            </button>
          </nav>
          <span className="session-user">
            {profile.email}
            {profile.role === "admin" && <span className="pill pill-admin">admin</span>}
          </span>
          <button className="secondary small" onClick={handleSignOut}>Sign out</button>
        </div>
      </header>

      {agent === "consent" ? <ConsentAgentView /> : <RopaDashboard />}
    </div>
  );
}
