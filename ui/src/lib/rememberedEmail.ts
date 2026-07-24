/**
 * Remembered-login-email store (opt-in "Remember my email").
 *
 * A single, guarded `localStorage` slot holding ONLY the email address the
 * operator last signed in with — nothing else, never a token or password. It
 * powers the login screen's returning-visitor shortcut: when an email is
 * remembered, `LoginPage` hides the email input and shows only the passkey
 * button for that address (the user still clicks it — no auto-submit), and a
 * "Change email" control calls `clearRememberedEmail()` to fully forget it.
 *
 * Storing your OWN email on your OWN device is the point; it is therefore
 * strictly OPT-IN (the checkbox defaults off) and fully clearable, so a shared
 * device never leaks one operator's email to the next without an explicit
 * "Remember me" tick.
 *
 * Every `window.localStorage` access is wrapped in try/catch — mirroring the
 * checkpoint helpers in `deltaCache.ts` — so private-mode / disabled-storage
 * degrades to "nothing remembered" instead of throwing. Persistence here is a
 * convenience, never a correctness dependency.
 */

/** localStorage key for the remembered login email (single global slot). */
const REMEMBERED_EMAIL_KEY = "spec.login.email";

/**
 * Read the remembered email, or `null` when none is stored, the stored value
 * is blank, or storage is unavailable. Never throws.
 */
export function getRememberedEmail(): string | null {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(REMEMBERED_EMAIL_KEY);
  } catch {
    return null; // storage unavailable — behave as if nothing is remembered.
  }
  if (raw === null) return null;
  const trimmed = raw.trim();
  return trimmed === "" ? null : trimmed;
}

/**
 * Remember `email` for next time. A blank/whitespace value is treated as a
 * clear (nothing worth remembering). Never throws.
 */
export function setRememberedEmail(email: string): void {
  const trimmed = email.trim();
  if (trimmed === "") {
    clearRememberedEmail();
    return;
  }
  try {
    window.localStorage.setItem(REMEMBERED_EMAIL_KEY, trimmed);
  } catch {
    /* storage unavailable (private mode / disabled) — skip persistence. */
  }
}

/** Forget the remembered email (the "Change email" / opt-out path). Never throws. */
export function clearRememberedEmail(): void {
  try {
    window.localStorage.removeItem(REMEMBERED_EMAIL_KEY);
  } catch {
    /* storage unavailable — nothing to clear. */
  }
}
