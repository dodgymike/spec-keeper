import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, redeemEnrollment } from "../api/client";
import type { EnrollRedeemOut } from "../api/types";
import "./EnrollPage.css";

/**
 * Public agent-onboarding landing page (ONBOARD-5, route `/enroll`,
 * UNAUTHENTICATED). App.tsx routes here before the signed-in/out gate — a
 * brand-new agent/operator opens the single-use URL `…/enroll#token=<token>`
 * holding nothing but that token, so there is no session to read.
 *
 * The token is BURNED on redeem (single-use). We therefore NEVER auto-redeem on
 * load: a link-preview/prefetch or an accidental refresh must not spend the
 * token. Redemption requires an explicit button click, and the UI says so. The
 * credentials + one-time password live in component state only — never
 * localStorage/sessionStorage, never logged.
 */

function tokenFromHash(): string {
  // Matches the `enrollment_url` the mint builds: `{ENROLL_BASE_URL}/enroll#token={token}`.
  const hash = window.location.hash.replace(/^#/, "");
  return (new URLSearchParams(hash).get("token") ?? "").trim();
}

/** A single read-only credential with a copy-to-clipboard affordance. */
function CopyField({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable (e.g. insecure context) — the value is still
      // visible for manual selection; nothing to surface.
    }
  }

  return (
    <div className="enroll__field">
      <span className="enroll__field-label">{label}</span>
      <code className="enroll__field-value">{value}</code>
      <button
        type="button"
        className="enroll__copy"
        onClick={() => void copy()}
        aria-label={`Copy ${label}`}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

/** Turn a recipe key (`1_mint_token`) into a readable step heading. */
function recipeHeading(key: string): string {
  const withoutOrder = key.replace(/^\d+_/, "");
  return withoutOrder.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export function EnrollPage() {
  const [token] = useState(tokenFromHash);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<EnrollRedeemOut | null>(null);

  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, [result]);

  async function handleRedeem() {
    if (busy || !token) return;
    setError("");
    setBusy(true);
    try {
      const res = await redeemEnrollment(token);
      setResult(res);
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        setError("Too many attempts right now — please wait a moment and try again.");
      } else {
        // Generic on purpose: bad / already-used / expired all look the same
        // (the server gives no enumeration oracle), so neither do we.
        setError(
          "This enrollment link is invalid, already used, or expired — ask your admin for a new one."
        );
      }
    } finally {
      setBusy(false);
    }
  }

  // Ordered recipe steps (keys are `1_…`, `2_…`, `3_…`). Sort by the leading
  // integer so a future `10_…` step still lands after `2_…` (not before it,
  // as a lexicographic sort would).
  const recipeSteps = result
    ? Object.entries(result.recipe).sort(
        ([a], [b]) => (parseInt(a, 10) || 0) - (parseInt(b, 10) || 0)
      )
    : [];

  return (
    <div className="enroll">
      <div className="enroll__card">
        <h1 className="enroll__title">Agent enrollment</h1>

        {result ? (
          <div>
            <h2 className="enroll__step-heading" tabIndex={-1} ref={headingRef}>
              You&apos;re enrolled
            </h2>

            <p className="enroll__warning" role="alert">
              These credentials are shown once and cannot be recovered — save them
              now. If you lose the password, ask your admin to mint a fresh
              enrollment token.
            </p>

            <div className="enroll__fields" role="status" aria-live="polite">
              <CopyField label="Username" value={result.username} />
              <CopyField label="Password" value={result.password} />
              <CopyField label="API base" value={result.api_base} />
              {result.region ? <CopyField label="Region" value={result.region} /> : null}
              {result.client_id ? (
                <CopyField label="Client ID" value={result.client_id} />
              ) : null}
              <CopyField label="Project" value={result.project_slug} />
              <CopyField label="Role" value={result.role} />
            </div>

            <h3 className="enroll__recipe-title">Set up &amp; migrate</h3>
            <p className="enroll__subtitle">
              Follow these steps to mint a token, make your first authenticated
              call, and move a local backlog into the cloud project. API calls
              must send a real <code>User-Agent</code> header — Cloudflare blocks
              the default python-urllib agent.
            </p>
            {recipeSteps.map(([key, value]) => (
              <section key={key} className="enroll__recipe-step">
                <h4 className="enroll__recipe-step-heading">{recipeHeading(key)}</h4>
                <pre className="enroll__code">
                  <code>{value}</code>
                </pre>
              </section>
            ))}

            <Link to="/" className="enroll__link-button">
              Go to the dashboard
            </Link>
          </div>
        ) : (
          <div>
            <h2 className="enroll__step-heading" tabIndex={-1} ref={headingRef}>
              Redeem your enrollment link
            </h2>

            {token ? (
              <>
                <p className="enroll__subtitle">
                  This link activates your agent and reveals its credentials
                  exactly once. It can only be used a single time, so redeem it
                  only when you&apos;re ready to save the credentials. It is not
                  redeemed until you click the button below.
                </p>
                {error ? (
                  <p className="enroll__error" role="alert">
                    {error}
                  </p>
                ) : null}
                <button
                  type="button"
                  className="enroll__button"
                  aria-busy={busy}
                  disabled={busy}
                  onClick={() => void handleRedeem()}
                >
                  {busy ? "Activating…" : "Redeem / activate"}
                </button>
              </>
            ) : (
              <p className="enroll__error" role="alert">
                No enrollment token found in this link. Open the full single-use
                URL your admin sent you (it ends with <code>#token=…</code>), or
                ask for a new one.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
