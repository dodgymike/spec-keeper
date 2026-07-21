/**
 * Stateless Cognito-IDP JSON API client + WebAuthn marshalling, ported
 * faithfully from the bird-song-visualisation reference
 * (`upload-platform/web/auth.js`). Talks DIRECTLY to the regional
 * Cognito-IDP endpoint - no Amplify, no Hosted UI, no OAuth/PKCE redirect.
 *
 * This module holds NO app state and stores NO tokens - every function here
 * takes what it needs (access token, session, etc.) as an argument and
 * returns the raw result. `auth/session.ts` is the only place that keeps
 * tokens (in memory) and reacts to these results.
 */

// ---- config -----------------------------------------------------------

function readEnv(name: string): string | undefined {
  const value = (import.meta.env as Record<string, string | undefined>)[name];
  return value && value.trim() !== "" ? value.trim() : undefined;
}

export function region(): string {
  return readEnv("VITE_COGNITO_REGION") ?? "eu-west-1";
}

export function clientId(): string {
  return readEnv("VITE_COGNITO_CLIENT_ID") ?? "";
}

export function userPoolId(): string {
  return readEnv("VITE_COGNITO_USER_POOL_ID") ?? "";
}

/** The relying party ID is the CloudFront host the SPA is served from. */
export function rpId(): string {
  return window.location.hostname;
}

export function idpEndpoint(): string {
  return `https://cognito-idp.${region()}.amazonaws.com/`;
}

export function isCognitoConfigured(): boolean {
  return Boolean(region() && clientId() && userPoolId());
}

// ---- Cognito-IDP JSON call ---------------------------------------------

/** Cognito errors are `{ "__type": "Namespace#Exception", "message": "..." }`. */
export class CognitoError extends Error {
  cognitoType: string;
  status: number;

  constructor(message: string, cognitoType: string, status: number) {
    super(message);
    this.name = "CognitoError";
    this.cognitoType = cognitoType;
    this.status = status;
  }
}

/**
 * POST to the Cognito-IDP regional JSON 1.1 endpoint. Unauthenticated
 * actions (SignUp, InitiateAuth, ...) need no SigV4 - the app client id is
 * the credential; user-scoped actions carry the caller's AccessToken in the
 * JSON body, still no SigV4.
 */
async function idp<T = Record<string, unknown>>(action: string, body: Record<string, unknown>): Promise<T> {
  const response = await fetch(idpEndpoint(), {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${action}`,
    },
    body: JSON.stringify(body),
  });

  const text = await response.text();
  let json: Record<string, unknown> | null = null;
  try {
    json = text ? (JSON.parse(text) as Record<string, unknown>) : null;
  } catch {
    json = null;
  }

  if (!response.ok) {
    const rawType = (json && (json.__type as string | undefined)) || `HTTP ${response.status}`;
    const message =
      (json && ((json.message as string | undefined) ?? (json.Message as string | undefined))) ||
      text ||
      `HTTP ${response.status}`;
    throw new CognitoError(message, String(rawType).split("#").pop() ?? String(rawType), response.status);
  }

  return (json ?? {}) as T;
}

// ---- base64url <-> bytes -------------------------------------------------

export function bytesToBase64Url(buffer: ArrayBuffer | Uint8Array): string {
  const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function base64UrlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const base64 = String(value).replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const binary = atob(padded);
  const bytes: Uint8Array<ArrayBuffer> = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** JWT payload decode (base64url) WITHOUT verifying - display only. */
export function decodeJwtPayload(token: string): Record<string, unknown> {
  const segment = token.split(".")[1];
  if (!segment) return {};
  const normalized = segment.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  try {
    return JSON.parse(atob(padded)) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/** Cognito requires a password meeting the pool policy even though native
 * users never see or re-enter it (they authenticate via passkey/email-OTP
 * afterwards); mix character classes so any default policy is satisfied. */
export function randomSecret(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return `Aa1!${bytesToBase64Url(bytes)}`;
}

/** True if this browser exposes a WebAuthn API at all. */
export function webAuthnSupported(): boolean {
  return Boolean(
    typeof window !== "undefined" && "PublicKeyCredential" in window && "credentials" in navigator
  );
}

// ---- WebAuthn option <-> JSON marshalling --------------------------------
// Cognito hands PublicKeyCredential{Creation,Request}Options with binary
// fields (challenge, user.id, {allow,exclude}Credentials[].id) as base64url
// strings; navigator.credentials.* needs them as BufferSource. These
// convert both directions, faithfully mirroring auth.js's
// decodeCreationOptions/decodeRequestOptions/encodeAttestation/encodeAssertion.

interface RawCredentialDescriptor {
  id: string;
  type?: string;
  transports?: string[];
}

interface RawCreationOptions {
  rp?: { id?: string; name?: string };
  user: { id: string; name?: string; displayName?: string };
  challenge: string;
  pubKeyCredParams: PublicKeyCredentialParameters[];
  timeout?: number;
  excludeCredentials?: RawCredentialDescriptor[];
  authenticatorSelection?: AuthenticatorSelectionCriteria;
  attestation?: AttestationConveyancePreference;
  extensions?: AuthenticationExtensionsClientInputs;
}

interface RawRequestOptions {
  rpId?: string;
  challenge: string;
  timeout?: number;
  allowCredentials?: RawCredentialDescriptor[];
  userVerification?: UserVerificationRequirement;
  extensions?: AuthenticationExtensionsClientInputs;
}

/** Cognito sometimes wraps the options under a `publicKey` key. */
function unwrapPublicKey(payload: unknown): Record<string, unknown> {
  if (payload && typeof payload === "object" && "publicKey" in (payload as Record<string, unknown>)) {
    return (payload as { publicKey: Record<string, unknown> }).publicKey;
  }
  return payload as Record<string, unknown>;
}

export function decodeCreationOptions(payload: unknown): PublicKeyCredentialCreationOptions {
  const raw = unwrapPublicKey(payload) as unknown as RawCreationOptions;
  const rp = raw.rp ?? {};
  return {
    rp: { name: rp.name ?? "", id: rp.id ?? rpId() },
    user: {
      id: base64UrlToBytes(raw.user.id),
      name: raw.user.name ?? "",
      displayName: raw.user.displayName ?? "",
    },
    challenge: base64UrlToBytes(raw.challenge),
    pubKeyCredParams: raw.pubKeyCredParams,
    timeout: raw.timeout,
    excludeCredentials: (raw.excludeCredentials ?? []).map((c) => ({
      id: base64UrlToBytes(c.id),
      type: "public-key" as const,
      transports: c.transports as AuthenticatorTransport[] | undefined,
    })),
    authenticatorSelection: raw.authenticatorSelection,
    attestation: raw.attestation,
    extensions: raw.extensions,
  };
}

export function decodeRequestOptions(payload: unknown): PublicKeyCredentialRequestOptions {
  const raw = unwrapPublicKey(payload) as unknown as RawRequestOptions;
  return {
    challenge: base64UrlToBytes(raw.challenge),
    rpId: raw.rpId ?? rpId(),
    timeout: raw.timeout,
    allowCredentials: (raw.allowCredentials ?? []).map((c) => ({
      id: base64UrlToBytes(c.id),
      type: "public-key" as const,
      transports: c.transports as AuthenticatorTransport[] | undefined,
    })),
    userVerification: raw.userVerification,
    extensions: raw.extensions,
  };
}

/** Serialise a `navigator.credentials.create()` result for CompleteWebAuthnRegistration. */
export function encodeAttestation(credential: PublicKeyCredential): Record<string, unknown> {
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: bytesToBase64Url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {},
    response: {
      clientDataJSON: bytesToBase64Url(response.clientDataJSON),
      attestationObject: bytesToBase64Url(response.attestationObject),
      transports: response.getTransports ? response.getTransports() : [],
    },
  };
}

/** Serialise a `navigator.credentials.get()` result for the WEB_AUTHN challenge. */
export function encodeAssertion(credential: PublicKeyCredential): Record<string, unknown> {
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: bytesToBase64Url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {},
    response: {
      clientDataJSON: bytesToBase64Url(response.clientDataJSON),
      authenticatorData: bytesToBase64Url(response.authenticatorData),
      signature: bytesToBase64Url(response.signature),
      userHandle: response.userHandle ? bytesToBase64Url(response.userHandle) : null,
    },
  };
}

// ---- shared response shapes ------------------------------------------------

/** Cognito's `AuthenticationResult` shape, verbatim field casing. */
export interface AuthenticationResult {
  AccessToken: string;
  IdToken: string;
  RefreshToken?: string;
  ExpiresIn?: number;
}

/** Normalised shape for every step of a CUSTOM_AUTH chain. */
export interface CustomStep {
  done: boolean;
  session?: string;
  challengeType?: string;
  authResult?: AuthenticationResult;
}

interface AuthChallengeResponse {
  ChallengeName?: string;
  Session?: string;
  ChallengeParameters?: Record<string, string>;
  AuthenticationResult?: AuthenticationResult;
}

// =========================================================================
// SIGN-UP - passwordless native user, invite-gated
// =========================================================================

export interface SignUpResult {
  UserConfirmed?: boolean;
  UserSub?: string;
  [key: string]: unknown;
}

export function signUp(email: string, inviteCode: string): Promise<SignUpResult> {
  return idp<SignUpResult>("SignUp", {
    ClientId: clientId(),
    Username: email,
    Password: randomSecret(),
    UserAttributes: [{ Name: "email", Value: email }],
    ClientMetadata: { invite_code: inviteCode },
  });
}

// =========================================================================
// DAILY SIGN-IN - passkey (USER_AUTH -> WEB_AUTHN, SELECT_CHALLENGE fallback)
// =========================================================================

/** Finish a WEB_AUTHN challenge (reached directly or via SELECT_CHALLENGE). */
async function continueWebAuthn(resp: AuthChallengeResponse, username: string): Promise<AuthenticationResult> {
  if (resp.AuthenticationResult) {
    // Some pools complete in one round-trip.
    return resp.AuthenticationResult;
  }
  if (resp.ChallengeName !== "WEB_AUTHN") {
    throw new Error(`Unexpected challenge: ${resp.ChallengeName ?? "none"}`);
  }
  const cp = resp.ChallengeParameters ?? {};
  const optionsJson = cp.CREDENTIAL_REQUEST_OPTIONS ?? cp.PUBLIC_KEY_CREDENTIAL_REQUEST_OPTIONS;
  if (!optionsJson) {
    throw new Error("Cognito did not return WebAuthn request options.");
  }
  const publicKey = decodeRequestOptions(JSON.parse(optionsJson));
  const assertion = await navigator.credentials.get({ publicKey });
  if (!(assertion instanceof PublicKeyCredential)) {
    throw new Error("No passkey assertion was returned.");
  }
  const credential = encodeAssertion(assertion);
  const result = await idp<AuthChallengeResponse>("RespondToAuthChallenge", {
    ClientId: clientId(),
    ChallengeName: "WEB_AUTHN",
    Session: resp.Session,
    ChallengeResponses: {
      USERNAME: cp.USERNAME ?? username,
      CREDENTIAL: JSON.stringify(credential),
    },
  });
  // Pools with a preferred TOTP MFA return a SOFTWARE_TOKEN_MFA challenge
  // here. Login-time TOTP entry isn't built - fail clearly/recoverably
  // rather than leaving a broken session (kept from auth.js, harmless and
  // future-proof: this pool has MFA off today).
  if (!result.AuthenticationResult && result.ChallengeName === "SOFTWARE_TOKEN_MFA") {
    const err = new Error(
      "This account has an authenticator app enrolled, and code entry at sign-in isn't supported yet. Use email sign-in instead."
    );
    err.name = "SoftwareTokenMfaNotSupportedClient";
    throw err;
  }
  if (!result.AuthenticationResult) {
    throw new Error(`Unexpected challenge: ${result.ChallengeName ?? "none"}`);
  }
  return result.AuthenticationResult;
}

/**
 * InitiateAuth(USER_AUTH, PreferredChallenge=WEB_AUTHN, USERNAME) ->
 * WEB_AUTHN challenge (or SELECT_CHALLENGE first, on pools that don't honour
 * PreferredChallenge) -> navigator.credentials.get() -> RespondToAuthChallenge.
 */
export async function signInWithPasskey(username: string): Promise<AuthenticationResult> {
  const resp = await idp<AuthChallengeResponse>("InitiateAuth", {
    AuthFlow: "USER_AUTH",
    ClientId: clientId(),
    AuthParameters: { PREFERRED_CHALLENGE: "WEB_AUTHN", USERNAME: username },
  });
  if (resp.ChallengeName === "SELECT_CHALLENGE") {
    const selected = await idp<AuthChallengeResponse>("RespondToAuthChallenge", {
      ClientId: clientId(),
      ChallengeName: "SELECT_CHALLENGE",
      Session: resp.Session,
      ChallengeResponses: { USERNAME: username, ANSWER: "WEB_AUTHN" },
    });
    return continueWebAuthn(selected, username);
  }
  return continueWebAuthn(resp, username);
}

// =========================================================================
// CUSTOM_AUTH chain (onboarding + recovery): email-OTP
// =========================================================================

function normaliseCustomStep(resp: AuthChallengeResponse): CustomStep {
  if (resp.AuthenticationResult) {
    return { done: true, authResult: resp.AuthenticationResult };
  }
  const cp = resp.ChallengeParameters ?? {};
  return {
    done: false,
    session: resp.Session,
    challengeType: cp.challengeType ?? cp.challenge_type ?? "EMAIL_OTP",
  };
}

export async function startCustomAuth(email: string, clientMetadata?: Record<string, string>): Promise<CustomStep> {
  const resp = await idp<AuthChallengeResponse>("InitiateAuth", {
    AuthFlow: "CUSTOM_AUTH",
    ClientId: clientId(),
    AuthParameters: { USERNAME: email },
    ClientMetadata: clientMetadata ?? {},
  });
  return normaliseCustomStep(resp);
}

export async function respondCustomAuth(
  email: string,
  authSession: string,
  answer: string,
  clientMetadata?: Record<string, string>
): Promise<CustomStep> {
  const resp = await idp<AuthChallengeResponse>("RespondToAuthChallenge", {
    ClientId: clientId(),
    ChallengeName: "CUSTOM_CHALLENGE",
    Session: authSession,
    ChallengeResponses: { USERNAME: email, ANSWER: answer },
    ClientMetadata: clientMetadata ?? {},
  });
  return normaliseCustomStep(resp);
}

// =========================================================================
// Refresh
// =========================================================================

/** Note: RefreshToken is NOT returned on refresh - callers must keep the original. */
export async function refreshTokens(refreshToken: string): Promise<AuthenticationResult> {
  const resp = await idp<AuthChallengeResponse>("InitiateAuth", {
    AuthFlow: "REFRESH_TOKEN_AUTH",
    ClientId: clientId(),
    AuthParameters: { REFRESH_TOKEN: refreshToken },
  });
  if (!resp.AuthenticationResult) {
    throw new Error("Cognito did not return refreshed tokens.");
  }
  return resp.AuthenticationResult;
}

// =========================================================================
// Passkey enrol / list / delete (all need a signed-in AccessToken)
// =========================================================================

interface StartWebAuthnRegistrationResponse {
  CredentialCreationOptions: unknown;
}

/** StartWebAuthnRegistration -> navigator.credentials.create() -> CompleteWebAuthnRegistration. */
export async function enrolPasskey(accessToken: string): Promise<void> {
  const start = await idp<StartWebAuthnRegistrationResponse>("StartWebAuthnRegistration", {
    AccessToken: accessToken,
  });
  const publicKey = decodeCreationOptions(start.CredentialCreationOptions);
  const credential = await navigator.credentials.create({ publicKey });
  if (!(credential instanceof PublicKeyCredential)) {
    throw new Error("Passkey creation did not return a credential.");
  }
  await idp("CompleteWebAuthnRegistration", {
    AccessToken: accessToken,
    Credential: encodeAttestation(credential),
  });
}

export interface Passkey {
  credentialId: string;
  friendlyName?: string;
  relyingPartyId?: string;
  createdAt?: string | number;
}

interface ListWebAuthnCredentialsResponse {
  Credentials?: Array<{
    CredentialId: string;
    FriendlyCredentialName?: string;
    RelyingPartyId?: string;
    CreatedAt?: string | number;
  }>;
}

export async function listPasskeys(accessToken: string): Promise<Passkey[]> {
  const resp = await idp<ListWebAuthnCredentialsResponse>("ListWebAuthnCredentials", {
    AccessToken: accessToken,
    MaxResults: 20,
  });
  return (resp.Credentials ?? []).map((c) => ({
    credentialId: c.CredentialId,
    friendlyName: c.FriendlyCredentialName,
    relyingPartyId: c.RelyingPartyId,
    createdAt: c.CreatedAt,
  }));
}

export async function deletePasskey(accessToken: string, credentialId: string): Promise<void> {
  await idp("DeleteWebAuthnCredential", { AccessToken: accessToken, CredentialId: credentialId });
}

/** Best-effort: never blocks a local sign-out on a network/API error. */
export async function globalSignOut(accessToken: string): Promise<void> {
  try {
    await idp("GlobalSignOut", { AccessToken: accessToken });
  } catch {
    // best-effort - the local session is cleared regardless.
  }
}
