import { useCallback, useEffect, useRef, useState } from "react";
import * as cognito from "../auth/cognito";
import type { Passkey } from "../auth/cognito";
import * as session from "../auth/session";
import { friendlyError } from "../auth/errors";
import "./SettingsPage.css";

/** Signed-in only: manage the caller's own WebAuthn passkeys. */
export function SettingsPage() {
  const [passkeys, setPasskeys] = useState<Passkey[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  const confirmButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const removeButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const focusRemoveIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (confirmingId) {
      confirmButtonRefs.current.get(confirmingId)?.focus();
    } else if (focusRemoveIdRef.current) {
      removeButtonRefs.current.get(focusRemoveIdRef.current)?.focus();
      focusRemoveIdRef.current = null;
    }
  }, [confirmingId]);

  const load = useCallback(async () => {
    setError("");
    try {
      const list = await session.listPasskeys();
      setPasskeys(list);
    } catch (err) {
      setError(friendlyError(err));
      setPasskeys([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleAdd() {
    if (busy) return;
    setError("");
    setBusy(true);
    setStatus("Follow your device's prompt…");
    try {
      await session.enrolPasskey();
      setStatus("Passkey added.");
      await load();
    } catch (err) {
      setError(friendlyError(err));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  async function handleRemove(credentialId: string) {
    if (busy) return;
    setError("");
    setBusy(true);
    setStatus("Removing…");
    try {
      await session.deletePasskey(credentialId);
      setStatus("Passkey removed.");
      setConfirmingId(null);
      await load();
    } catch (err) {
      setError(friendlyError(err));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  function handleCancelConfirm(credentialId: string) {
    focusRemoveIdRef.current = credentialId;
    setConfirmingId(null);
  }

  return (
    <div className="settings">
      <h1 className="settings__title">Security</h1>
      <p role="status" aria-live="polite" className="settings__status">
        {status}
      </p>
      {error ? (
        <p className="settings__error" role="alert">
          {error}
        </p>
      ) : null}

      <section className="settings__section">
        <h2 className="settings__heading">Passkeys</h2>
        {passkeys === null ? (
          <p className="settings__muted">Loading…</p>
        ) : passkeys.length === 0 ? (
          <p className="settings__muted">No passkeys.</p>
        ) : (
          <ul className="settings__list">
            {passkeys.map((passkey) => (
              <li key={passkey.credentialId} className="settings__item">
                <div className="settings__item-info">
                  <span className="settings__item-name">{passkey.friendlyName || "Passkey"}</span>
                  {passkey.createdAt ? (
                    <span className="settings__item-meta">
                      Added {new Date(Number(passkey.createdAt) * 1000).toLocaleDateString()}
                    </span>
                  ) : null}
                </div>
                {confirmingId === passkey.credentialId ? (
                  <div className="settings__confirm">
                    <span className="settings__confirm-text" role="status" aria-live="polite">
                      {passkeys.length === 1
                        ? "This is your only passkey - removing it may lock you out. Remove anyway?"
                        : "Remove this passkey?"}
                    </span>
                    <button
                      type="button"
                      className="settings__button settings__button--danger"
                      aria-busy={busy}
                      ref={(el) => {
                        if (el) confirmButtonRefs.current.set(passkey.credentialId, el);
                        else confirmButtonRefs.current.delete(passkey.credentialId);
                      }}
                      onClick={() => void handleRemove(passkey.credentialId)}
                    >
                      Confirm
                    </button>
                    <button
                      type="button"
                      className="settings__link-button"
                      onClick={() => handleCancelConfirm(passkey.credentialId)}
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button
                    type="button"
                    className="settings__button"
                    ref={(el) => {
                      if (el) removeButtonRefs.current.set(passkey.credentialId, el);
                      else removeButtonRefs.current.delete(passkey.credentialId);
                    }}
                    onClick={() => setConfirmingId(passkey.credentialId)}
                  >
                    Remove
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        <button
          type="button"
          className="settings__button settings__button--primary"
          aria-busy={busy}
          disabled={!cognito.webAuthnSupported()}
          onClick={() => void handleAdd()}
        >
          Add a passkey
        </button>
      </section>
    </div>
  );
}
