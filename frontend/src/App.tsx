import { useEffect, useState } from "react";
import { AUTH_BYPASSED, OPEN_ACCESS, autoLogin, getProfile, logout, type Profile } from "./api/auth";
import { setUnauthorizedHandler } from "./api/client";
import { ConsentAgentView } from "./ConsentAgentView";
import { LoginScreen } from "./components/LoginScreen";
import { ReportPage } from "./report/print/ReportPage";

// One view for now. Add an agent here (and to NAV below) to give it its own screen.
type View = "consent";

const VIEWS: readonly View[] = ["consent"];

/** The view named by `#/<view>`, so a reload returns to it. Never a token or personal data. */
function viewFromHash(hash: string): View | null {
  const m = /^#\/([a-z]+)(?:\/|$)/.exec(hash);
  return m && (VIEWS as readonly string[]).includes(m[1]) ? (m[1] as View) : null;
}

/** Update the address without adding a history entry. Never throws: a sandboxed frame that
 *  forbids it simply loses reload-restore. */
function writeHash(hash: string): void {
  try {
    if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
  } catch {
    /* reload-restore is a convenience, not a requirement */
  }
}

/**
 * Shell around the Consent Agent. Its UI lives in ConsentAgentView; this file only adds
 * the sign-in gate and the sidebar that switches between agent screens.
 */
export default function App() {
  // The report for one scan has its own address (/?report=<scanId>); the server prints it (&print=1) to make the PDF. It shows only the report,
  // with no console around it, and works from the session already in this tab.
  const reportParams = new URLSearchParams(window.location.search);
  const reportScanId = reportParams.get("report");
  if (reportScanId) {
    const variant = reportParams.get("variant");
    const part = reportParams.get("part");
    return (
      <ReportPage
        scanId={reportScanId} print={reportParams.get("print") === "1"}
        variant={variant === "free" || variant === "full" || variant === "summary" ? variant : undefined} part={part === "pages" || part === "appendix" || part === "cover" || part === "body" ? part : undefined}
      />
    );
  }
  return <Console />;
}

function Console() {
  const [profile, setProfile] = useState<Profile | null>(() => getProfile());
  // The active view lives in the address (#/consent), so a reload returns to it.
  const [view, setViewState] = useState<View>(() => viewFromHash(window.location.hash) ?? "consent");
  function setView(next: View) {
    setViewState(next);
    writeHash(`#/${next}`);
  }
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

  const NAV: { id: View; label: string }[] = [
    { id: "consent", label: "Privacy Policy" },
  ];

  return (
    <div className="shell">
      <aside className="shell-sidebar">
        <div className="shell-brand">
          <span className="brand">Consiva</span>
          <span className="tagline">DPDP compliance registry</span>
        </div>

        <nav className="shell-nav">
          {NAV.map(({ id, label }) => (
            <button
              key={id}
              className={view === id ? "active" : ""}
              onClick={() => setView(id)}
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
          {view === "consent" && <ConsentAgentView />}
        </div>
      </div>
    </div>
  );
}
