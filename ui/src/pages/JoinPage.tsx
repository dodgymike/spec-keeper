import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import * as cognito from "../auth/cognito";
import * as session from "../auth/session";
import { friendlyError } from "../auth/errors";
import "./JoinPage.css";

type Step = "email" | "otp" | "passkey" | "done";

const STEP_HEADINGS: Record<Step, string> = {
  email: "Create your account",
  otp: "Enter your code",
  passkey: "Create a passkey",
  done: "Setup complete",
};

const STEPS: { id: Step; label: string }[] = [
  { id: "email", label: "Email" },
  { id: "otp", label: "Verify" },
  { id: "passkey", label: "Passkey" },
];

/**
 * Invite-only onboarding (`/join?code=...`). Renders regardless of auth
 * status (App.tsx routes here before the signed-in/out gate) since a brand
 * new human has no session yet. Mirrors the bird reference's
 * join.inline.js step machine: SignUp (invite in ClientMetadata, tolerating
 * `UsernameExistsException` when resuming) -> CUSTOM_AUTH email-OTP ->
 * passkey enrolment.
 */
export function JoinPage() {
  const [searchParams] = useSearchParams();
  const codeFromUrl = (searchParams.get("code") ?? "").trim();

  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [inviteCode, setInviteCode] = useState(codeFromUrl);
  const [otp, setOtp] = useState("");
  const [otpSession, setOtpSession] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, [step]);

  useEffect(() => {
    // The passkey step (create or skip) already gives the user a chance to
    // decide about a passkey inline, so once onboarding reaches "done" there
    // is nothing left for App.tsx's post-sign-in passkey offer to add -
    // dismiss it so leaving /join doesn't immediately re-prompt.
    if (step === "done") session.dismissPasskeyOffer();
  }, [step]);

  async function handleEmailSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    const trimmedEmail = email.trim();
    const code = inviteCode.trim();
    if (!code) {
      setError("An invite code is required.");
      return;
    }
    setBusy(true);
    setStatus("Sending a verification code…");
    try {
      try {
        await cognito.signUp(trimmedEmail, code);
      } catch (err) {
        // Resuming an interrupted join: the user already exists, proceed to the OTP chain.
        if (!(err instanceof cognito.CognitoError && err.cognitoType === "UsernameExistsException")) {
          throw err;
        }
      }
      const result = await cognito.startCustomAuth(trimmedEmail, { invite_code: code });
      if (result.done && result.authResult) {
        session.adoptTokens(result.authResult, { offerPasskey: true });
        setStep("passkey");
      } else {
        setOtpSession(result.session ?? "");
        setStep("otp");
      }
      setStatus("");
    } catch (err) {
      setError(friendlyError(err));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  async function handleOtpSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    setStatus("Verifying…");
    try {
      const result = await cognito.respondCustomAuth(email.trim(), otpSession, otp.trim(), {
        invite_code: inviteCode.trim(),
      });
      if (!result.done || !result.authResult) {
        throw new Error(`Unexpected sign-up step: ${result.challengeType ?? "unknown"}.`);
      }
      session.adoptTokens(result.authResult, { offerPasskey: true });
      setStatus("");
      setStep("passkey");
    } catch (err) {
      setError(friendlyError(err));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  async function handleEnrolPasskey() {
    if (busy) return;
    setError("");
    setBusy(true);
    setStatus("Follow your device's prompt…");
    try {
      await session.enrolPasskey();
      setStatus("");
      setStep("done");
    } catch (err) {
      setError(friendlyError(err));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  const activeIndex = STEPS.findIndex((s) => s.id === step);

  return (
    <div className="join">
      <div className="join__card">
        <h1 className="join__title">Join Spec Server</h1>
        <ol className="join__steps">
          {STEPS.map((s, index) => (
            <li
              key={s.id}
              aria-current={s.id === step ? "step" : undefined}
              className={
                s.id === step
                  ? "join__chip join__chip--active"
                  : activeIndex > index
                    ? "join__chip join__chip--done"
                    : "join__chip"
              }
            >
              {s.label}
            </li>
          ))}
        </ol>
        <h2 className="join__step-heading" tabIndex={-1} ref={headingRef}>
          {STEP_HEADINGS[step]}
        </h2>
        <p role="status" aria-live="polite" className="join__status">
          {status}
        </p>
        {error ? (
          <p className="join__error" role="alert">
            {error}
          </p>
        ) : null}

        {step === "email" ? (
          <form onSubmit={(event) => void handleEmailSubmit(event)}>
            <label htmlFor="join-email" className="join__label">
              Email
            </label>
            <input
              id="join-email"
              type="email"
              required
              autoComplete="email"
              className="join__input"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
            {!codeFromUrl ? (
              <>
                <label htmlFor="join-code" className="join__label">
                  Invite code
                </label>
                <input
                  id="join-code"
                  type="text"
                  required
                  className="join__input"
                  value={inviteCode}
                  onChange={(event) => setInviteCode(event.target.value)}
                />
              </>
            ) : null}
            <button type="submit" className="join__button" aria-busy={busy}>
              Continue
            </button>
          </form>
        ) : null}

        {step === "otp" ? (
          <form onSubmit={(event) => void handleOtpSubmit(event)}>
            <p className="join__subtitle">Enter the code sent to {email}.</p>
            <label htmlFor="join-otp" className="join__label">
              Verification code
            </label>
            <input
              id="join-otp"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              required
              className="join__input"
              value={otp}
              onChange={(event) => setOtp(event.target.value)}
            />
            <button type="submit" className="join__button" aria-busy={busy}>
              Verify
            </button>
          </form>
        ) : null}

        {step === "passkey" ? (
          <div>
            <p className="join__subtitle">Create a passkey to finish onboarding.</p>
            {!cognito.webAuthnSupported() ? (
              <p className="join__notice" role="status">
                This browser doesn&apos;t support passkeys. Ask an admin for help signing in on this device.
              </p>
            ) : null}
            <button
              type="button"
              className="join__button"
              aria-busy={busy}
              disabled={!cognito.webAuthnSupported()}
              onClick={() => void handleEnrolPasskey()}
            >
              Create passkey
            </button>
            <Link to="/" className="join__link-button" onClick={() => session.dismissPasskeyOffer()}>
              Skip for now — go to dashboard
            </Link>
          </div>
        ) : null}

        {step === "done" ? (
          <>
            <p className="join__done">Setup complete.</p>
            <Link to="/" replace className="join__button">
              Go to dashboard
            </Link>
          </>
        ) : null}
      </div>
    </div>
  );
}
