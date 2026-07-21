/**
 * Framework-agnostic auth session singleton: builds the Cognito Hosted-UI
 * authorize/logout URLs, exchanges the authorization code for tokens at the
 * token endpoint (public client, no secret - PKCE verifier proves
 * possession instead), and holds the access/id/refresh tokens **in memory
 * only** (never persisted to localStorage/sessionStorage, so a real XSS
 * can't read them off disk; a page reload starts a fresh sign-in).
 *
 * `state` + PKCE `code_verifier` are stashed in `sessionStorage` only for
 * the short round trip to the Hosted UI and back (cleared as soon as the
 * callback is handled).
 *
 * `client.ts` (the API client) is a plain module, not a React component, so
 * it talks to this singleton directly via `getAccessToken()` /
 * `recoverFromUnauthorized()` rather than through the React context.
 * `auth/AuthContext.tsx` wraps this same singleton for React consumers
 * (header user chip, sign-in/out screens) via `subscribe()`.
 */
import { cognitoConfig } from "./config";
import { deriveCodeChallenge, generateRandomToken } from "./pkce";

const VERIFIER_KEY = "spec_server_auth_verifier";
const STATE_KEY = "spec_server_auth_state";
const RETURN_TO_KEY = "spec_server_auth_return_to";

export interface AuthUser {
  email?: string;
  sub?: string;
}

export type AuthStatus = "disabled" | "signed-out" | "signed-in";

export interface AuthState {
  status: AuthStatus;
  user: AuthUser | null;
  error?: string;
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
let authState: AuthState = { status: cognitoConfig ? "signed-out" : "disabled", user: null };
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
  return Boolean(cognitoConfig);
}

function decodeJwtPayload(jwt: string): Record<string, unknown> {
  const segment = jwt.split(".")[1];
  if (!segment) return {};
  const normalized = segment.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  try {
    return JSON.parse(atob(padded)) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function applyTokens(next: TokenSet): void {
  tokens = next;
  const claims = decodeJwtPayload(next.idToken);
  setState({
    status: "signed-in",
    user: {
      email: typeof claims.email === "string" ? claims.email : undefined,
      sub: typeof claims.sub === "string" ? claims.sub : undefined,
    },
  });
}

function clearTokens(): void {
  tokens = null;
  sessionStorage.removeItem(VERIFIER_KEY);
  sessionStorage.removeItem(STATE_KEY);
  sessionStorage.removeItem(RETURN_TO_KEY);
}

/** Redirect to the Cognito Hosted UI to start Authorization Code + PKCE. */
export async function signIn(returnTo?: string): Promise<void> {
  if (!cognitoConfig) return;
  const verifier = generateRandomToken();
  const requestState = generateRandomToken();
  const challenge = await deriveCodeChallenge(verifier);

  sessionStorage.setItem(VERIFIER_KEY, verifier);
  sessionStorage.setItem(STATE_KEY, requestState);
  sessionStorage.setItem(RETURN_TO_KEY, returnTo ?? `${window.location.pathname}${window.location.search}`);

  const url = new URL(`https://${cognitoConfig.domain}/oauth2/authorize`);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", cognitoConfig.clientId);
  url.searchParams.set("redirect_uri", cognitoConfig.redirectUri);
  url.searchParams.set("scope", cognitoConfig.scopes);
  url.searchParams.set("state", requestState);
  url.searchParams.set("code_challenge", challenge);
  url.searchParams.set("code_challenge_method", "S256");

  window.location.assign(url.toString());
}

/** Clear local session state and redirect through the Cognito logout endpoint. */
export function signOut(): void {
  clearTokens();
  if (!cognitoConfig) {
    setState({ status: "disabled", user: null });
    return;
  }
  setState({ status: "signed-out", user: null });
  const url = new URL(`https://${cognitoConfig.domain}/logout`);
  url.searchParams.set("client_id", cognitoConfig.clientId);
  url.searchParams.set("logout_uri", cognitoConfig.logoutUri);
  window.location.assign(url.toString());
}

async function exchangeToken(body: URLSearchParams): Promise<void> {
  if (!cognitoConfig) throw new Error("Cognito is not configured.");
  const previousRefreshToken = tokens?.refreshToken;
  const response = await fetch(`https://${cognitoConfig.domain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!response.ok) {
    throw new Error(`Cognito token endpoint returned ${response.status}`);
  }
  const data = (await response.json()) as {
    access_token: string;
    id_token: string;
    refresh_token?: string;
    expires_in: number;
  };
  applyTokens({
    accessToken: data.access_token,
    idToken: data.id_token,
    // The refresh grant does not always return a new refresh_token; keep
    // the previous one in that case.
    refreshToken: data.refresh_token ?? previousRefreshToken,
    expiresAt: Date.now() + data.expires_in * 1000,
  });
}

/**
 * Handle the `/callback` redirect: validate `state`, exchange `code` for
 * tokens using the stashed PKCE verifier (public client - no secret).
 * Returns the path the user was on before `signIn()` sent them to Cognito.
 */
export async function handleCallback(params: URLSearchParams): Promise<string> {
  if (!cognitoConfig) throw new Error("Cognito is not configured.");
  const returnTo = sessionStorage.getItem(RETURN_TO_KEY) || "/";

  const oauthError = params.get("error");
  if (oauthError) {
    clearTokens();
    setState({ status: "signed-out", user: null, error: params.get("error_description") ?? oauthError });
    throw new Error(oauthError);
  }

  const code = params.get("code");
  const returnedState = params.get("state");
  const expectedState = sessionStorage.getItem(STATE_KEY);
  const verifier = sessionStorage.getItem(VERIFIER_KEY);

  if (!code || !returnedState || !verifier || returnedState !== expectedState) {
    clearTokens();
    setState({ status: "signed-out", user: null, error: "Sign-in response failed validation." });
    throw new Error("Invalid OAuth callback (state mismatch or missing code/verifier).");
  }

  sessionStorage.removeItem(STATE_KEY);
  sessionStorage.removeItem(VERIFIER_KEY);
  sessionStorage.removeItem(RETURN_TO_KEY);

  await exchangeToken(
    new URLSearchParams({
      grant_type: "authorization_code",
      client_id: cognitoConfig.clientId,
      code,
      redirect_uri: cognitoConfig.redirectUri,
      code_verifier: verifier,
    })
  );

  return returnTo;
}

async function refresh(): Promise<boolean> {
  if (!cognitoConfig || !tokens?.refreshToken) return false;
  try {
    await exchangeToken(
      new URLSearchParams({
        grant_type: "refresh_token",
        client_id: cognitoConfig.clientId,
        refresh_token: tokens.refreshToken,
      })
    );
    return true;
  } catch {
    clearTokens();
    setState({ status: "signed-out", user: null });
    return false;
  }
}

const EXPIRY_SKEW_MS = 30_000;

/**
 * Current access token, silently refreshing first if it's expired or about
 * to expire. Returns `undefined` when Cognito isn't configured (callers
 * fall back to `VITE_DEV_TOKEN`) or when there is no session yet.
 */
export async function getAccessToken(): Promise<string | undefined> {
  if (!cognitoConfig) return undefined;
  if (tokens && tokens.expiresAt - EXPIRY_SKEW_MS > Date.now()) {
    return tokens.accessToken;
  }
  const refreshed = await refresh();
  return refreshed ? tokens?.accessToken : undefined;
}

/**
 * Called by `client.ts` on a 401: try one silent refresh; on failure,
 * redirect to the Hosted UI sign-in (never leaves the caller on a blank
 * screen or looping retries).
 */
export async function recoverFromUnauthorized(): Promise<string | undefined> {
  if (!cognitoConfig) return undefined;
  const refreshed = await refresh();
  if (refreshed) return tokens?.accessToken;
  await signIn();
  return undefined;
}
