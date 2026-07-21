/**
 * Framework-agnostic auth session singleton. Holds the access/id/refresh
 * tokens **in memory only** (never written to localStorage/sessionStorage -
 * a page reload always starts a fresh session) and drives them via the
 * stateless Cognito-IDP client in `cognito.ts` (native WebAuthn passkeys +
 * email-OTP - no Hosted UI, no OAuth/PKCE redirect).
 *
 * `client.ts` (the API client) is a plain module, not a React component, so
 * it talks to this singleton directly via `getAccessToken()` /
 * `recoverFromUnauthorized()` rather than through the React context.
 * `auth/AuthContext.tsx` wraps this same singleton for React consumers
 * (header user chip, settings page) via `subscribe()`. `pages/LoginPage.tsx`
 * and `pages/JoinPage.tsx` drive the sign-in/sign-up ceremonies directly via
 * `cognito.ts` and hand the result to `adoptTokens()`.
 */
import * as cognito from "./cognito";
import type { AuthenticationResult, Passkey } from "./cognito";

export interface AuthUser {
  email?: string;
  sub?: string;
}

export type AuthStatus = "disabled" | "signed-out" | "signed-in";

export interface AuthState {
  status: AuthStatus;
  user: AuthUser | null;
  error?: string;
  /**
   * True right after an OTP/recovery-style sign-in (email-OTP recovery, or
   * `/join` onboarding) completes, so the App shell can offer passkey
   * enrolment. `LoginPage`/`JoinPage` unmount the instant `adoptTokens()`
   * flips `status` to "signed-in" (App only renders them while signed-out /
   * on `/join*`), so the offer has to live at the App level - see
   * `App.tsx`'s `PasskeyOfferScreen`.
   */
  pendingPasskeyOffer: boolean;
}

interface TokenSet {
  accessToken: string;
  idToken: string;
  refreshToken?: string;
  /** Epoch ms. */
  expiresAt: number;
}

type Listener = (state: AuthState) => void;

let tokens: TokenSet | null = null;
let authState: AuthState = {
  status: cognito.isCognitoConfigured() ? "signed-out" : "disabled",
  user: null,
  pendingPasskeyOffer: false,
};
const listeners = new Set<Listener>();

function setState(next: AuthState): void {
  authState = next;
  listeners.forEach((listener) => listener(authState));
}

export function getState(): AuthState {
  return authState;
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function isCognitoConfigured(): boolean {
  return cognito.isCognitoConfigured();
}

function applyTokens(result: AuthenticationResult, pendingPasskeyOffer: boolean): void {
  const claims = cognito.decodeJwtPayload(result.IdToken);
  tokens = {
    accessToken: result.AccessToken,
    idToken: result.IdToken,
    // RefreshToken is only returned on the initial authentication, not on refresh.
    refreshToken: result.RefreshToken ?? tokens?.refreshToken,
    expiresAt: Date.now() + (result.ExpiresIn ?? 3600) * 1000,
  };
  setState({
    status: "signed-in",
    user: {
      email: typeof claims.email === "string" ? claims.email : undefined,
      sub: typeof claims.sub === "string" ? claims.sub : undefined,
    },
    pendingPasskeyOffer,
  });
}

/**
 * Apply a completed ceremony's tokens (passkey sign-in, email-OTP, sign-up).
 * Pass `{ offerPasskey: true }` for OTP/recovery-style sign-ins (email-OTP
 * recovery, `/join` onboarding) where the user may not have a passkey yet;
 * omit it (or pass `false`) for ordinary passkey sign-in.
 */
export function adoptTokens(result: AuthenticationResult, options?: { offerPasskey?: boolean }): void {
  applyTokens(result, options?.offerPasskey ?? false);
}

/** Dismiss the post-sign-in passkey offer (App.tsx's `PasskeyOfferScreen`). */
export function dismissPasskeyOffer(): void {
  if (!authState.pendingPasskeyOffer) return;
  setState({ ...authState, pendingPasskeyOffer: false });
}

function clearTokens(): void {
  tokens = null;
}

const EXPIRY_SKEW_MS = 30_000;

// Single-flight refresh: a page can have more than one caller triggering
// getAccessToken()/recoverFromUnauthorized() near-simultaneously; without
// de-duplication both would POST the same refresh token concurrently.
let refreshInFlight: Promise<string | undefined> | null = null;

async function refresh(): Promise<string | undefined> {
  if (refreshInFlight) return refreshInFlight;
  if (!tokens?.refreshToken) return undefined;
  const refreshToken = tokens.refreshToken;
  const attempt = (async (): Promise<string | undefined> => {
    try {
      const result = await cognito.refreshTokens(refreshToken);
      // A silent token refresh never changes whether a passkey offer is
      // pending - carry the current value forward rather than resetting it.
      applyTokens(result, authState.pendingPasskeyOffer);
      return tokens?.accessToken;
    } catch {
      clearTokens();
      setState({ status: "signed-out", user: null, pendingPasskeyOffer: false });
      return undefined;
    }
  })();
  refreshInFlight = attempt;
  void attempt.finally(() => {
    if (refreshInFlight === attempt) refreshInFlight = null;
  });
  return attempt;
}

/**
 * Current access token, silently refreshing first if it's expired or about
 * to expire. Returns `undefined` when Cognito isn't configured (callers
 * fall back to `VITE_DEV_TOKEN`) or when there is no session yet.
 */
export async function getAccessToken(): Promise<string | undefined> {
  if (!cognito.isCognitoConfigured()) return undefined;
  if (tokens && tokens.expiresAt - EXPIRY_SKEW_MS > Date.now()) {
    return tokens.accessToken;
  }
  return refresh();
}

/**
 * Called by `client.ts` on a 401: try one silent refresh; on failure, clear
 * the session and return `undefined` (no redirect - `App.tsx` renders
 * `LoginPage` for `status === "signed-out"`).
 */
export async function recoverFromUnauthorized(): Promise<string | undefined> {
  if (!cognito.isCognitoConfigured()) return undefined;
  const refreshed = await refresh();
  if (refreshed) return refreshed;
  clearTokens();
  setState({ status: "signed-out", user: null, pendingPasskeyOffer: false });
  return undefined;
}

/** Best-effort GlobalSignOut, then clear the local session. No redirect. */
export async function signOut(): Promise<void> {
  const accessToken = tokens?.accessToken;
  if (accessToken) {
    await cognito.globalSignOut(accessToken);
  }
  clearTokens();
  setState({
    status: cognito.isCognitoConfigured() ? "signed-out" : "disabled",
    user: null,
    pendingPasskeyOffer: false,
  });
}

async function requireAccessToken(): Promise<string> {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    throw new Error("Not signed in.");
  }
  return accessToken;
}

/** Enrol a new passkey for the signed-in user (Settings, or post-OTP onboarding). */
export async function enrolPasskey(): Promise<void> {
  const accessToken = await requireAccessToken();
  await cognito.enrolPasskey(accessToken);
}

/** List the signed-in user's registered passkeys (Settings -> Security). */
export async function listPasskeys(): Promise<Passkey[]> {
  const accessToken = await requireAccessToken();
  return cognito.listPasskeys(accessToken);
}

/** Remove one of the signed-in user's passkeys (Settings -> Security). */
export async function deletePasskey(credentialId: string): Promise<void> {
  const accessToken = await requireAccessToken();
  await cognito.deletePasskey(accessToken, credentialId);
}
