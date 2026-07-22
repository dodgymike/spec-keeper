/**
 * Thin re-export of the native Cognito config surface. The config
 * accessors (`region`/`clientId`/`userPoolId`/`isCognitoConfigured`) live in
 * `cognito.ts` alongside the IDP client and WebAuthn marshalling they
 * configure; this module exists only so call sites that just need the
 * config surface (not the IDP ceremonies) don't need to import `cognito.ts`
 * directly.
 *
 * Local-dev fallback: when `VITE_COGNITO_CLIENT_ID`/`VITE_COGNITO_USER_POOL_ID`
 * are unset, `isCognitoConfigured()` is `false` and `client.ts` falls back to
 * the existing `VITE_DEV_TOKEN` seam (or no auth).
 */
export { region, clientId, userPoolId, rpId, isCognitoConfigured } from "./cognito";
