/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the Spec Server API, e.g. http://localhost:8080 */
  readonly VITE_API_BASE?: string;
  /**
   * Dev-only bearer token, used when Cognito is NOT configured (see
   * `VITE_COGNITO_DOMAIN` below). Never set this in a production build.
   */
  readonly VITE_DEV_TOKEN?: string;
  /**
   * Cognito Hosted-UI domain host (no scheme), e.g.
   * `spec-server-auth-ab12cd.auth.us-east-1.amazoncognito.com`. Unset ->
   * auth is disabled and the app falls back to `VITE_DEV_TOKEN`/no auth.
   */
  readonly VITE_COGNITO_DOMAIN?: string;
  /** Public SPA app client ID (Authorization Code + PKCE, no secret). */
  readonly VITE_COGNITO_CLIENT_ID?: string;
  /** OAuth redirect_uri; must exactly match a callback URL configured on the Cognito app client. Default: http://localhost:5173/callback */
  readonly VITE_COGNITO_REDIRECT_URI?: string;
  /** Cognito logout redirect (logout_uri); must exactly match a configured logout URL. Default: http://localhost:5173/ */
  readonly VITE_COGNITO_LOGOUT_URI?: string;
  /** Space-separated OAuth scopes. Default: "openid email profile" */
  readonly VITE_COGNITO_SCOPES?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
