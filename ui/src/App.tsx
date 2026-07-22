import { useEffect, useRef, useState } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { AppLayout } from "./components/AppLayout";
import { ActivityPage } from "./pages/ActivityPage";
import { AdminPage } from "./pages/AdminPage";
import { CoordinationPage } from "./pages/CoordinationPage";
import { JoinPage } from "./pages/JoinPage";
import { LoginPage } from "./pages/LoginPage";
import { ProgressPage } from "./pages/ProgressPage";
import { ProjectDetailPage } from "./pages/ProjectDetailPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { RequestAccessPage } from "./pages/RequestAccessPage";
import { SettingsPage } from "./pages/SettingsPage";
import { useAuth } from "./auth/AuthContext";
import * as cognito from "./auth/cognito";
import * as session from "./auth/session";
import { friendlyError } from "./auth/errors";
import "./pages/LoginPage.css";

/**
 * One-shot "add a passkey?" offer shown right after an OTP/recovery-style
 * sign-in (`session.pendingPasskeyOffer`). This can't live inside
 * `LoginPage`/`JoinPage` themselves: `adoptTokens()` flips auth `status` to
 * "signed-in" the instant the ceremony completes, and `App` only renders
 * those pages while signed-out (or on `/join*`) - they'd unmount in the same
 * React batch before any post-sign-in UI of their own could paint. Reuses
 * `LoginPage.css`'s `.login` card styling.
 */
function PasskeyOfferScreen() {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  async function handleAdd() {
    if (busy) return;
    setError("");
    setBusy(true);
    setStatus("Follow your device's prompt…");
    try {
      await session.enrolPasskey();
      session.dismissPasskeyOffer();
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
        <h2 className="login__step-heading" tabIndex={-1} ref={headingRef}>
          Add a passkey
        </h2>
        <p className="login__subtitle">Add a passkey for faster sign-in (optional).</p>
        <p role="status" aria-live="polite" className="login__status">
          {status}
        </p>
        {error ? (
          <p className="login__error" role="alert">
            {error}
          </p>
        ) : null}
        <button
          type="button"
          className="login__button"
          aria-busy={busy}
          disabled={!cognito.webAuthnSupported()}
          onClick={() => void handleAdd()}
        >
          Add a passkey
        </button>
        <button type="button" className="login__link-button" onClick={() => session.dismissPasskeyOffer()}>
          Not now
        </button>
      </div>
    </div>
  );
}

/**
 * Top-level route table. UI-3..8 add further nested routes here, e.g.
 * `/projects/:slug/epics/:key`, etc. `/projects/:slug` is a stub
 * (`ProjectDetailPage`) until UI-3 fills it in.
 *
 * Auth gate: `/join` (invite-only onboarding) renders regardless of auth
 * status - a brand new human has no session yet. Otherwise `LoginPage`
 * (native WebAuthn passkey + email-OTP sign-in) replaces the app when
 * Cognito is configured and there is no session (`status ===
 * "signed-out"`). When Cognito is not configured, `status` is `"disabled"`
 * and the app behaves exactly as before (local-dev fallback). Right after an
 * OTP/recovery sign-in, `pendingPasskeyOffer` interposes `PasskeyOfferScreen`
 * before the routed app.
 */
export default function App() {
  const location = useLocation();
  const { status, pendingPasskeyOffer } = useAuth();

  if (location.pathname.startsWith("/join")) {
    return <JoinPage />;
  }

  // `/request` is the public HA-7 access-request page: like `/join` it renders
  // regardless of auth status (a prospective user has no session), outside the
  // signed-in app shell.
  if (location.pathname.startsWith("/request")) {
    return <RequestAccessPage />;
  }

  return (
    <AppLayout>
      {status === "signed-out" ? (
        <LoginPage />
      ) : status === "signed-in" && pendingPasskeyOffer ? (
        <PasskeyOfferScreen />
      ) : (
        <Routes>
          <Route path="/" element={<ProjectsPage />} />
          <Route path="/projects/:slug" element={<ProjectDetailPage />} />
          <Route path="/projects/:slug/activity" element={<ActivityPage />} />
          <Route path="/projects/:slug/progress" element={<ProgressPage />} />
          <Route path="/coordination" element={<CoordinationPage />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      )}
    </AppLayout>
  );
}
