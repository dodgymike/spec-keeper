import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { requestAccess } from "../api/client";
import { loadTurnstile } from "../lib/turnstile";
import "./RequestAccessPage.css";

/**
 * Cloudflare Turnstile site key. When set, the Turnstile challenge script
 * (https://challenges.cloudflare.com/turnstile/v0/api.js — an origin the SPA's
 * CSP allows in script-src + frame-src) is loaded, a widget is rendered, and
 * its response token is forwarded to the intake as `turnstile_token`. Left
 * unset for local/dev keeps the flow CSP-clean with no third-party script and
 * no bot-gate.
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
  // Turnstile response token, populated by the widget's success callback.
  const [turnstileToken, setTurnstileToken] = useState("");
  // True when the Turnstile script could not load (blocked/offline), so submit
  // can offer a distinct "reload" recovery instead of an impossible "complete
  // the challenge" prompt against an empty container.
  const [turnstileLoadFailed, setTurnstileLoadFailed] = useState(false);

  const headingRef = useRef<HTMLHeadingElement>(null);
  const turnstileRef = useRef<HTMLDivElement>(null);
  const widgetIdRef = useRef<string | null>(null);
  const siteKey = turnstileSiteKey();

  useEffect(() => {
    headingRef.current?.focus();
  }, [submitted]);

  // Load the Turnstile script once and render the widget on this page only.
  // No-op when the site key is unset (dev) or after a successful submit (the
  // form — and its container — is gone).
  useEffect(() => {
    if (!siteKey || submitted) return;
    let cancelled = false;
    // Match the widget theme to the app's forced theme (not the OS default).
    const appTheme = document.documentElement.getAttribute("data-theme");
    const theme = appTheme === "dark" || appTheme === "light" ? appTheme : "auto";
    loadTurnstile()
      .then((turnstile) => {
        const container = turnstileRef.current;
        if (cancelled || !container || widgetIdRef.current !== null) return;
        widgetIdRef.current = turnstile.render(container, {
          sitekey: siteKey,
          theme,
          callback: (token) => setTurnstileToken(token),
          "expired-callback": () => setTurnstileToken(""),
          "error-callback": () => setTurnstileToken(""),
        });
      })
      .catch(() => {
        // Script blocked or failed to load: no challenge can be completed, so
        // flag it and let submit surface a reload affordance (fail-closed — we
        // still never post without a token).
        if (!cancelled) setTurnstileLoadFailed(true);
      });
    return () => {
      cancelled = true;
      // Tear the widget down so a remount renders a fresh one (symmetry with render).
      if (widgetIdRef.current !== null) {
        window.turnstile?.remove(widgetIdRef.current);
        widgetIdRef.current = null;
      }
    };
  }, [siteKey, submitted]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    const trimmedEmail = email.trim();
    if (!trimmedEmail) {
      setError("An email address is required.");
      return;
    }
    // When the bot-gate is active, require a completed challenge before posting.
    if (siteKey && turnstileLoadFailed) {
      setError("The verification widget failed to load. Please reload the page and try again.");
      return;
    }
    if (siteKey && !turnstileToken) {
      setError("Please complete the verification challenge.");
      if (widgetIdRef.current !== null) window.turnstile?.reset(widgetIdRef.current);
      return;
    }

    setBusy(true);
    try {
      await requestAccess({
        email: trimmedEmail,
        display_name: displayName.trim() || undefined,
        turnstile_token: turnstileToken || undefined,
        hp_website: honeypot,
      });
      // Uniform outcome: never branch on the response body.
      setSubmitted(true);
    } catch {
      // A genuine transport error (the intake always answers 202 otherwise) -
      // keep the message generic so nothing is revealed. A Turnstile token is
      // single-use, so reset the widget for a fresh challenge before retrying.
      setError("Could not submit your request. Please try again.");
      if (siteKey && widgetIdRef.current !== null) {
        window.turnstile?.reset(widgetIdRef.current);
        setTurnstileToken("");
      }
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
            <form onSubmit={(event) => void handleSubmit(event)}>
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
                <div ref={turnstileRef} className="request-access__turnstile" />
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
