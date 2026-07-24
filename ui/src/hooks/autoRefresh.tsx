import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

/**
 * User-configurable dashboard auto-refresh cadence, persisted in localStorage so
 * the choice survives reloads.
 *
 *   - `null`         -> "Default": each page keeps its built-in cadence (today's
 *                        behaviour, i.e. no stored preference).
 *   - `0`            -> "Off": no background polling anywhere; only the manual
 *                        Refresh buttons fetch.
 *   - positive `ms`  -> that interval, applied to every auto-refresh loop.
 */
export type AutoRefreshPreference = number | null;

/** localStorage key. Absent -> Default; "0" -> Off; "<ms>" -> explicit interval. */
export const AUTO_REFRESH_STORAGE_KEY = "spec:autoRefreshMs";

/** The selectable cadences shown in the header control (default listed first). */
export const AUTO_REFRESH_OPTIONS: ReadonlyArray<{ label: string; value: AutoRefreshPreference }> = [
  { label: "Default", value: null },
  { label: "Off", value: 0 },
  { label: "10s", value: 10_000 },
  { label: "30s", value: 30_000 },
  { label: "1m", value: 60_000 },
  { label: "5m", value: 300_000 },
];

/** Reads the stored preference, tolerating disabled/absent localStorage. */
function readStored(): AutoRefreshPreference {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY);
  } catch {
    return null; // localStorage unavailable (private mode / disabled) -> Default.
  }
  if (raw === null) return null;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 0) return null; // corrupt value -> Default.
  return parsed;
}

/** Persists the preference; `null` clears the key so startup falls back to Default. */
function writeStored(value: AutoRefreshPreference): void {
  try {
    if (value === null) {
      window.localStorage.removeItem(AUTO_REFRESH_STORAGE_KEY);
    } else {
      window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, String(value));
    }
  } catch {
    /* localStorage unavailable - keep the in-memory choice for this session. */
  }
}

interface AutoRefreshContextValue {
  preference: AutoRefreshPreference;
  setPreference: (value: AutoRefreshPreference) => void;
}

const AutoRefreshContext = createContext<AutoRefreshContextValue | undefined>(undefined);

/** Provides the persisted auto-refresh preference to the header control and every polling hook. */
export function AutoRefreshProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<AutoRefreshPreference>(readStored);

  const setPreference = useCallback((value: AutoRefreshPreference) => {
    setPreferenceState(value);
    writeStored(value);
  }, []);

  // Keep multiple tabs consistent: adopt a change written by another tab.
  useEffect(() => {
    function onStorage(event: StorageEvent) {
      if (event.key === AUTO_REFRESH_STORAGE_KEY) setPreferenceState(readStored());
    }
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  return (
    <AutoRefreshContext.Provider value={{ preference, setPreference }}>
      {children}
    </AutoRefreshContext.Provider>
  );
}

/**
 * The user's auto-refresh preference + setter. Falls back to a render-safe
 * "Default" (no-op setter) outside a provider so a page can render standalone.
 */
export function useAutoRefreshPreference(): AutoRefreshContextValue {
  const ctx = useContext(AutoRefreshContext);
  if (!ctx) {
    return { preference: null, setPreference: () => {} };
  }
  return ctx;
}

/**
 * Resolves the effective polling interval for a page given its built-in default:
 * `null` -> the page default (today's behaviour); `0` -> off; else the chosen
 * interval. A return value of `0` means "do not poll".
 */
export function resolveAutoRefreshMs(
  preference: AutoRefreshPreference,
  pageDefaultMs: number
): number {
  return preference === null ? pageDefaultMs : preference;
}
