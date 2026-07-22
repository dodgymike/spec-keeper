import { useEffect, useRef, useState, type FormEvent } from "react";
import * as cognito from "../auth/cognito";
import * as session from "../auth/session";
import { friendlyError } from "../auth/errors";
import "./LoginPage.css";

type Mode = "passkey" | "otp-email" | "otp-code";

const STEP_HEADINGS: Record<Mode, string> = {
  passkey: "Sign in",
  "otp-email": "Sign in by email",
  "otp-code": "Enter your code",
};

/**
 * Signed-out gate (App.tsx). Default: passkey sign-in (native WebAuthn via
 * `cognito.signInWithPasskey`). "Email me a code instead" drives a
 * CUSTOM_AUTH email-OTP round trip for recovery/no-passkey sign-in. A
 * passkey enrolment offer after that is optional, never required - and it
 * lives in `App.tsx`'s `PasskeyOfferScreen`, not here: `adoptTokens()` flips
 * auth status to "signed-in" the instant the OTP completes, and App.tsx only
 * renders `LoginPage` while `status === "signed-out"`, so this component
 * would already have unmounted before any post-sign-in UI of its own could
 * paint.
 */
export function LoginPage() {
  const [mode, setMode] = useState<Mode>("passkey");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [otpSession, setOtpSession] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, [mode]);

  async function handlePasskeySignIn(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    setStatus("Follow your device's prompt…");
    try {
      const result = await cognito.signInWithPasskey(email.trim());
      session.adoptTokens(result);
      setStatus("");
    } catch (err) {
      setError(friendlyError(err));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  async function handleRequestCode(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    setStatus("Sending code…");
    try {
      const step = await cognito.startCustomAuth(email.trim(), { mode: "recovery" });
      if (step.done && step.authResult) {
        session.adoptTokens(step.authResult, { offerPasskey: true });
        setStatus("");
        return;
      }
      setOtpSession(step.session ?? "");
      setMode("otp-code");
      setStatus("");
    } catch (err) {
      setError(friendlyError(err));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  async function handleVerifyCode(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    setStatus("Verifying…");
    try {
      const step = await cognito.respondCustomAuth(email.trim(), otpSession, code.trim(), { mode: "recovery" });
      if (!step.done || !step.authResult) {
        throw new Error(`Unexpected sign-in step: ${step.challengeType ?? "unknown"}.`);
      }
      session.adoptTokens(step.authResult, { offerPasskey: true });
      setStatus("");
    } catch (err) {
      setError(friendlyError(err));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <div className="login__card">
        <h1 className="login__title">Spec Server</h1>
        <h2 className="login__step-heading" tabIndex={-1} ref={headingRef}>
          {STEP_HEADINGS[mode]}
        </h2>
        <p className="login__subtitle">Sign in to view the dashboard.</p>
        {!cognito.webAuthnSupported() ? (
          <p className="login__notice" role="status">
            This browser doesn&apos;t support passkeys. Use email sign-in below.
          </p>
        ) : null}
        <p role="status" aria-live="polite" className="login__status">
          {status}
        </p>
        {error ? (
          <p className="login__error" role="alert">
            {error}
          </p>
        ) : null}

        {mode === "passkey" ? (
          <form onSubmit={(event) => void handlePasskeySignIn(event)}>
            <label htmlFor="login-email" className="login__label">
              Email
            </label>
            <input
              id="login-email"
              type="email"
              autoComplete="username webauthn"
              required
              className="login__input"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
            <button type="submit" className="login__button" aria-busy={busy}>
              Sign in with a passkey
            </button>
          </form>
        ) : null}

        {mode === "otp-email" ? (
          <form onSubmit={(event) => void handleRequestCode(event)}>
            <label htmlFor="login-otp-email" className="login__label">
              Email
            </label>
            <input
              id="login-otp-email"
              type="email"
              autoComplete="username"
              required
              className="login__input"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
            <button type="submit" className="login__button" aria-busy={busy}>
              Send code
            </button>
          </form>
        ) : null}

        {mode === "otp-code" ? (
          <form onSubmit={(event) => void handleVerifyCode(event)}>
            <p className="login__subtitle">Enter the code sent to {email}.</p>
            <label htmlFor="login-otp-code" className="login__label">
              Verification code
            </label>
            <input
              id="login-otp-code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              required
              className="login__input"
              value={code}
              onChange={(event) => setCode(event.target.value)}
            />
            <button type="submit" className="login__button" aria-busy={busy}>
              Verify
            </button>
          </form>
        ) : null}

        {mode !== "otp-code" ? (
          <button
            type="button"
            className="login__link-button"
            onClick={() => {
              setError("");
              setMode(mode === "passkey" ? "otp-email" : "passkey");
            }}
          >
            {mode === "passkey" ? "Email me a code instead" : "Back to passkey sign-in"}
          </button>
        ) : null}
      </div>
    </div>
  );
}
