/**
 * Shared error-copy mapper for the native-WebAuthn auth pages (LoginPage,
 * JoinPage, SettingsPage). Keeping one mapper avoids drift between the
 * sign-in ceremonies and the passkey-management screen, and means Cognito's
 * raw exception text/messages are never shown to the user.
 */
import * as cognito from "./cognito";

export function friendlyError(err: unknown): string {
  if (err instanceof Error && err.name === "NotAllowedError") {
    return "That was cancelled or timed out - try again.";
  }
  const cognitoType = err instanceof cognito.CognitoError ? err.cognitoType : undefined;
  if (cognitoType === "UserNotFoundException") {
    return "No account found for that email. Ask an admin for an invite link.";
  }
  if (cognitoType === "NotAuthorizedException") {
    return "That passkey didn't match this account - try again, or email yourself a code instead.";
  }
  if (cognitoType === "CodeMismatchException") {
    return "That code is incorrect - try again.";
  }
  if (cognitoType === "ExpiredCodeException") {
    return "That code expired - request a new one.";
  }
  if (cognitoType === "UsernameExistsException") {
    return "An account with that email already exists.";
  }
  return err instanceof Error ? err.message : "Something went wrong - please try again.";
}
