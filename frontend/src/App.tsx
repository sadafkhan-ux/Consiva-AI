import { useEffect, useState } from "react";
import { AUTH_BYPASSED, OPEN_ACCESS, autoLogin, getProfile, logout, type Profile } from "./api/auth";
import { setUnauthorizedHandler } from "./api/client";
import { BreachConsole } from "./components/BreachConsole";
import { ConsentAgentView } from "./ConsentAgentView";
import { DsrConsole } from "./components/DsrConsole";
import { LoginScreen } from "./components/LoginScreen";
import { PurposeConsole } from "./components/PurposeConsole";
import { RegWatchConsole } from "./components/RegWatchConsole";
import { RopaDashboard } from "./components/RopaDashboard";

type Agent = "consent" | "ropa" | "dsr" | "breach" | "regwatch" | "purpose";

/**
 * Shell around the five agents. Agent 1's UI is unchanged -- it moved verbatim
 * into ConsentAgentView; this file only adds the sign-in gate and the switcher.
 */
export default function App() {
  const [profile, setProfile] = useState<Profile | null>(() => getProfile());
  const [agent, setAgent] = useState<Agent>("consent");
  // Only meaningful while AUTH_BYPASSED is true. `null` means "not tried yet", which
  // is different from "tried and failed" -- without that distinction the login screen
  // flashes on every load before the token arrives.
  const [autoLoginError, setAutoLoginError] = useState<string | null>(null);
  const [autoLoginTried, setAutoLoginTried] = useState(false);

  useEffect(() => {
    // A 401 from any request means the token died mid-session; drop straight
    // back to the login screen instead of leaving a broken console on screen.
    // With auto-login on, clearing the attempt makes the effect below sign in again,
    // so an expired token renews itself instead of showing a form.
    setUnauthorizedHandler(() => {
      logout();
      setProfile(null);
      setAutoLoginTried(false);
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    // Development, or a build made with VITE_OPEN_ACCESS=true. Vite replaces
    // `AUTH_BYPASSED` with a literal, so in any other production build this whole
    // effect is removed from the bundle rather than merely never running.
    if (!AUTH_BYPASSED || profile || autoLoginTried) return;
    setAutoLoginTried(true);
    autoLogin()
      .then(setProfile)
      .catch((e) => setAutoLoginError(e instanceof Error ? e.message : String(e)));
  }, [profile, autoLoginTried]);

  if (!profile) {
    // Still waiting on the dev token: show nothing rather than a login form that is
    // about to be replaced.
    if (AUTH_BYPASSED && !autoLoginError) {
      return <div className="login-wrap"><span className="muted">Signing in…</span></div>;
    }
    // Auto-login failed (or this is a production build): the real form is the
    // fallback, not something that was deleted.
    return <LoginScreen onSignedIn={setProfile} signInHint={autoLoginError} />;
  }

  function handleSignOut() {
    logout();
    setProfile(null);
  }

  const AGENTS: { id: Agent; label: string }[] = [
    { id: "consent", label: "Consent" },
    { id: "ropa", label: "ROPA" },
    { id: "dsr", label: "DSR" },
    { id: "breach", label: "Breach" },
    { id: "regwatch", label: "Regulatory Watch" },
    { id: "purpose", label: "Purpose" },
  ];

  return (
    <div className="shell">
      <aside className="shell-sidebar">
        <div className="shell-brand">
          <span className="brand">Consiva</span>
          <span className="tagline">DPDP compliance registry</span>
        </div>

        <nav className="shell-nav">
          {AGENTS.map(({ id, label }) => (
            <button
              key={id}
              className={agent === id ? "active" : ""}
              onClick={() => setAgent(id)}
            >
              <span className="dot" />
              {label}
            </button>
          ))}
        </nav>

        <div className="shell-session">
          <span className="session-user">
            {profile.email}
            {profile.role === "admin" && <span className="pill pill-admin">admin</span>}
          </span>
          {/* Hidden under auto-login: signing out would only sign straight back in. */}
          {!AUTH_BYPASSED && (
            <button className="secondary small" onClick={handleSignOut}>Sign out</button>
          )}
        </div>
      </aside>

      <div className="shell-main">
        {AUTH_BYPASSED && (
          // Loud and permanent. A console that silently skips authentication looks
          // exactly like one that authenticated, and the difference matters the moment
          // anybody screenshots it or points it at something real.
          <div className="banner banner-warn" style={{ margin: "12px 16px 0" }}>
            {OPEN_ACCESS ? (
              <>
                <strong>Login bypassed — open access.</strong> Everyone who opens this
                site is signed in as <span className="mono">{profile.email}</span> with no
                password (<span className="mono">OPEN_ACCESS=true</span>).
              </>
            ) : (
              <>
                <strong>Login bypassed — development build.</strong> Signed in as{" "}
                <span className="mono">{profile.email}</span> with no password. The
                backend only offers this when{" "}
                <span className="mono">APP_ENV=development</span> or{" "}
                <span className="mono">OPEN_ACCESS=true</span>.
              </>
            )}
          </div>
        )}

        <div className="shell-content">
          {agent === "consent" && <ConsentAgentView />}
          {agent === "ropa" && <RopaDashboard />}
          {agent === "dsr" && <DsrConsole />}
          {agent === "breach" && <BreachConsole />}
          {agent === "regwatch" && <RegWatchConsole />}
          {agent === "purpose" && <PurposeConsole />}
        </div>
      </div>
    </div>
  );
}
