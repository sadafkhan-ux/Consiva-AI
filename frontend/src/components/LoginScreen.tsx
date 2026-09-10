import { useState } from "react";
import { login, type Profile } from "../api/auth";

export function LoginScreen({ onSignedIn }: { onSignedIn: (profile: Profile) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!email.trim() || !password) return;
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await login(email.trim(), password));
    } catch (err) {
      // The backend returns one identical message for unknown email, wrong
      // password and disabled account -- show it verbatim rather than guessing.
      setError(err instanceof Error ? err.message : "Sign in failed.");
    } finally {
      setBusy(false);
      setPassword("");
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={handleSubmit}>
        <div className="brand">Consiva AI</div>
        <div className="login-sub">Sign in to the compliance console</div>

        <label className="login-label" htmlFor="login-email">Email</label>
        <input
          id="login-email"
          type="email"
          autoComplete="username"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          disabled={busy}
          required
        />

        <label className="login-label" htmlFor="login-password">Password</label>
        <input
          id="login-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          disabled={busy}
          required
        />

        {error && <div className="error-box login-error">{error}</div>}

        <button type="submit" disabled={busy || !email.trim() || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <div className="login-hint">
          Accounts are provisioned by an administrator — there is no self-signup.
        </div>
      </form>
    </div>
  );
}
