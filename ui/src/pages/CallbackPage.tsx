import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { handleCallback } from "../auth/session";
import "./CallbackPage.css";

/**
 * OAuth redirect target (`VITE_COGNITO_REDIRECT_URI`): exchanges the `code`
 * for tokens (PKCE, public client - no secret) then returns the user to
 * wherever `signIn()` sent them from. Rendered outside `<AppLayout>` (see
 * `App.tsx`) so it never flashes the signed-out app shell.
 */
export function CallbackPage() {
  const navigate = useNavigate();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [message, setMessage] = useState("Signing you in…");

  // Focus management: move focus onto this view on route change so screen
  // readers announce it instead of leaving focus on a now-unmounted control.
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const returnTo = await handleCallback(new URLSearchParams(window.location.search));
        if (!cancelled) navigate(returnTo, { replace: true });
      } catch {
        if (!cancelled) {
          setMessage("Sign-in failed. Returning to the sign-in screen…");
          window.setTimeout(() => {
            if (!cancelled) navigate("/", { replace: true });
          }, 1500);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [navigate]);

  return (
    <div className="callback">
      <h1 ref={headingRef} tabIndex={-1} className="callback__heading">
        Spec Server
      </h1>
      <p role="status" aria-live="polite" className="callback__status">
        {message}
      </p>
    </div>
  );
}
