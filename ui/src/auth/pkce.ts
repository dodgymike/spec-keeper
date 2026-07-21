/**
 * Hand-rolled Authorization Code + PKCE primitives using Web Crypto only
 * (`crypto.getRandomValues` / `crypto.subtle.digest`) - no OIDC library, no
 * CDN script, CSP-safe (no `unsafe-inline`, no third-party origin needed for
 * these calls).
 */

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** RFC 7636 recommends 43-128 chars; 32 random bytes -> 43 base64url chars. */
export function generateRandomToken(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes);
}

/** PKCE `S256` code_challenge: base64url(SHA-256(ascii(verifier))). */
export async function deriveCodeChallenge(verifier: string): Promise<string> {
  const data = new TextEncoder().encode(verifier);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return base64UrlEncode(new Uint8Array(digest));
}
