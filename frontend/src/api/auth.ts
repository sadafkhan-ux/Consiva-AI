// First-party authentication against the backend's own /api/v1/auth endpoints
// (backend/app/api/v1/routes/auth.py). Replaces the previous demo-token call,
// which was hard-gated to APP_ENV=development and therefore forced the whole
// deployment to run in development mode -- meaning anyone who reached the URL
// could mint a token. Real login removes that constraint.

import { ApiError } from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

// sessionStorage, not localStorage: the token dies with the tab. A compliance
// console left open on a shared machine shouldn't still be logged in tomorrow.
const TOKEN_KEY = "consiva.access_token";
const PROFILE_KEY = "consiva.profile";

export interface Profile {
  user_id: string;
  org_id: string;
  role: string;
  email: string;
}

export interface LoginResult {
  access_token: string;
  expires_in: number;
  org_id: string;
  user_id: string;
  role: string;
}

function readStorage(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    // Private-mode browsers can throw on any storage access. Failing to read a
    // token just means "not logged in", never a crash.
    return null;
  }
}

function writeStorage(key: string, value: string | null): void {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    // Non-fatal: the session then lasts only as long as this page view.
  }
}

export function getToken(): string | null {
  return readStorage(TOKEN_KEY);
}

export function getProfile(): Profile | null {
  const raw = readStorage(PROFILE_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Profile;
  } catch {
    return null;
  }
}

export function isAuthenticated(): boolean {
  return getToken() !== null;
}

export async function login(email: string, password: string): Promise<Profile> {
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
  } catch {
    throw new ApiError(0, `Could not reach the backend at ${BASE_URL}. Is it running?`);
  }

  const body = await res.json().catch(() => null);
  if (!res.ok) {
    // The backend deliberately returns one identical message for unknown email,
    // wrong password and disabled account, so it can't be used to enumerate
    // accounts. Surface it as-is rather than guessing at a friendlier reason.
    const detail =
      body && typeof body.detail === "string" ? body.detail : "Login failed.";
    throw new ApiError(res.status, detail);
  }

  const data = body as LoginResult;
  const profile: Profile = {
    user_id: data.user_id,
    org_id: data.org_id,
    role: data.role,
    email,
  };
  writeStorage(TOKEN_KEY, data.access_token);
  writeStorage(PROFILE_KEY, JSON.stringify(profile));
  return profile;
}

export function logout(): void {
  writeStorage(TOKEN_KEY, null);
  writeStorage(PROFILE_KEY, null);
}
