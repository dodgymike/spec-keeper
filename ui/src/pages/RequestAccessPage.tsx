import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { requestAccess } from "../api/client";
import "./RequestAccessPage.css";

/**
 * Cloudflare Turnstile site key. When set, a widget placeholder is rendered and
 * its token (the injected `cf-turnstile-response` field) is forwarded to the
 * intake. DEPLOY FOLLOW-UP: the Turnstile challenge script itself
 * (https://challenges.cloudflare.com/turnstile/v0/api.js) is NOT loaded here -
 * the SPA's CSP must be widened to allow that origin (script-src + frame-src)
 * before the widget actually renders. Left unset here so the build stays
 * CSP-clean with no third-party script.
 */
function turnstileSiteKey(): string | undefined {
  const value = import.meta.env.VITE_TURNSTILE_SITE_KEY;
  return value && value.trim() !== "" ? value.trim() : undefined;
}

/**
 * Public "request access" page (HA-7 intake, route `/request`, UNAUTHENTICATED).
 * POSTs email + optional display name to `/api/v1/signup`. The server always
 * answers with a uniform 202, so this page ALWAYS shows the same neutral
 * confirmation on success and never reveals whether the address is known or
 * eligible.
 */
export function RequestAccessPage() {
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  // Honeypot: a real user never sees or fills this; a non-empty value is
  // silently dropped as a bot by the intake (backend field `hp_website`).
  const [honeypot, setHoneypot] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState("");

  const headingRef = useRef<HTMLHeadingElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const siteKey = turnstileSiteKey();

  useEffect(() => {
    headingRef.current?.focus();
  }, [submitted]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    const trimmedEmail = email.trim();
    if (!trimmedEmail) {
      setError("An email address is required.");
      return;
    }
    // The Turnstile script (when loaded) injects a hidden `cf-turnstile-response`
    // field into the form; read it if present, otherwise send nothing.
    const tokenField = formRef.current?.querySelector<HTMLInputElement>('[name="cf-turnstile-response"]');
    const turnstileToken = tokenField?.value || undefined;

    setBusy(true);
    try {
      await requestAccess({
        email: trimmedEmail,
        display_name: displayName.trim() || undefined,
        turnstile_token: turnstileToken,
        hp_website: honeypot,
      });
      // Uniform outcome: never branch on the response body.
      setSubmitted(true);
    } catch {
      // A genuine transport error (the intake always answers 202 otherwise) -
      // keep the message generic so nothing is revealed.
      setError("Could not submit your request. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="request-access">
      <div className="request-access__card">
        <h1 className="request-access__title">Request access</h1>

        {submitted ? (
          <div role="status" aria-live="polite">
            <h2 className="request-access__heading" tabIndex={-1} ref={headingRef}>
              Request received
            </h2>
            <p className="request-access__subtitle">
              If your request is eligible, you will receive an email with next steps.
            </p>
            <Link to="/" className="request-access__link-button">
              Back to sign in
            </Link>
          </div>
        ) : (
          <>
            <h2 className="request-access__heading" tabIndex={-1} ref={headingRef}>
              Ask an admin for an invite
            </h2>
            <p className="request-access__subtitle">
              Access is invite-only. Submit your email and an admin will review it.
            </p>
            {error ? (
              <p className="request-access__error" role="alert">
                {error}
              </p>
            ) : null}
            <form ref={formRef} onSubmit={(event) => void handleSubmit(event)}>
              <label htmlFor="request-email" className="request-access__label">
                Email
              </label>
              <input
                id="request-email"
                type="email"
                required
                autoComplete="email"
                className="request-access__input"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />

              <label htmlFor="request-name" className="request-access__label">
                Display name (optional)
              </label>
              <input
                id="request-name"
                type="text"
                autoComplete="name"
                className="request-access__input"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
              />

              {/* Honeypot: hidden from users and assistive tech; bots that
                  auto-fill it get silently dropped by the intake. */}
              <div className="request-access__honeypot" aria-hidden="true">
                <label htmlFor="request-hp">Leave this field empty</label>
                <input
                  id="request-hp"
                  name="hp_website"
                  type="text"
                  tabIndex={-1}
                  autoComplete="off"
                  value={honeypot}
                  onChange={(event) => setHoneypot(event.target.value)}
                />
              </div>

              {siteKey ? (
                <div className="cf-turnstile request-access__turnstile" data-sitekey={siteKey} />
              ) : null}

              <button type="submit" className="request-access__button" aria-busy={busy}>
                Request access
              </button>
            </form>
            <Link to="/" className="request-access__link-button">
              Back to sign in
            </Link>
          </>
        )}
      </div>
    </div>
  );
}
