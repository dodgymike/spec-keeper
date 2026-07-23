/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the Spec Server API, e.g. http://localhost:8080 */
  readonly VITE_API_BASE?: string;
  /**
   * Dev-only bearer token, used when Cognito is NOT configured (see
   * `VITE_COGNITO_CLIENT_ID` below). Never set this in a production build.
   */
  readonly VITE_DEV_TOKEN?: string;
  /**
   * AWS region hosting the Cognito user pool, e.g. `eu-west-1`. Defaults to
   * `eu-west-1` when unset. The app talks DIRECTLY to the regional
   * Cognito-IDP JSON endpoint (`https://cognito-idp.<region>.amazonaws.com/`)
   * - no Hosted UI, no OAuth/PKCE redirect, no Amplify.
   */
  readonly VITE_COGNITO_REGION?: string;
  /**
   * Cognito user pool ID, e.g. `eu-west-1_AbCdEfGhI`. Unset (together with
   * `VITE_COGNITO_CLIENT_ID`) -> auth is disabled and the app falls back to
   * `VITE_DEV_TOKEN`/no auth.
   */
  readonly VITE_COGNITO_USER_POOL_ID?: string;
  /** Public native (non-Hosted-UI) app client ID used directly against the Cognito-IDP JSON API. */
  readonly VITE_COGNITO_CLIENT_ID?: string;
  /**
   * Public Cloudflare Turnstile site key for the `/request` access page. When
   * set, the Turnstile script is loaded and a challenge widget is rendered; its
   * token is forwarded to `/api/v1/signup` as `turnstile_token`. Unset (dev)
   * disables the widget and leaves the intake flow unchanged.
   */
  readonly VITE_TURNSTILE_SITE_KEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
