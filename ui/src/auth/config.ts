/**
 * Cognito Hosted-UI configuration, read from env at build time (see
 * `.env.example`). Mirrors the human SPA client in
 * `infra/terraform/cognito.tf` (`aws_cognito_user_pool_client.ui`):
 * Authorization Code + PKCE, public client, openid/email/profile scopes.
 *
 * Local-dev fallback (the precedence the backend mirrors): when
 * `VITE_COGNITO_DOMAIN`/`VITE_COGNITO_CLIENT_ID` are unset, `cognitoConfig`
 * is `undefined` and the app runs with no login flow at all - `client.ts`
 * falls back to the existing `VITE_DEV_TOKEN` seam (or no auth).
 */
export interface CognitoConfig {
  /** Hosted-UI domain host, no scheme/trailing slash, e.g. `foo.auth.us-east-1.amazoncognito.com`. */
  domain: string;
  clientId: string;
  redirectUri: string;
  logoutUri: string;
  /** Space-separated OAuth scopes, e.g. `openid email profile`. */
  scopes: string;
}

function readEnv(name: string): string | undefined {
  const value = (import.meta.env as Record<string, string | undefined>)[name];
  return value && value.trim() !== "" ? value.trim() : undefined;
}

const domain = readEnv("VITE_COGNITO_DOMAIN");
const clientId = readEnv("VITE_COGNITO_CLIENT_ID");

export const cognitoConfig: CognitoConfig | undefined =
  domain && clientId
    ? {
        domain: domain.replace(/^https?:\/\//, "").replace(/\/$/, ""),
        clientId,
        redirectUri: readEnv("VITE_COGNITO_REDIRECT_URI") ?? "http://localhost:5173/callback",
        logoutUri: readEnv("VITE_COGNITO_LOGOUT_URI") ?? "http://localhost:5173/",
        scopes: readEnv("VITE_COGNITO_SCOPES") ?? "openid email profile",
      }
    : undefined;

/**
 * Route path(s) the SPA treats as the OAuth redirect target: the pathname of
 * the configured `VITE_COGNITO_REDIRECT_URI`, plus the Terraform default
 * (`/auth/callback`, see `ui_callback_urls` in `infra/terraform/cognito.tf`)
 * and this module's own default (`/callback`) - so the route works
 * out-of-the-box against either default without editing `App.tsx`.
 */
export const CALLBACK_PATHS: string[] = Array.from(
  new Set([
    "/callback",
    "/auth/callback",
    ...(cognitoConfig ? [new URL(cognitoConfig.redirectUri).pathname] : []),
  ])
);
