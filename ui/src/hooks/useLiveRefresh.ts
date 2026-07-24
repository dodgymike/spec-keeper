import { useEffect, useState } from "react";
import { resolveAutoRefreshMs, useAutoRefreshPreference } from "./autoRefresh";

/** How often the "Updated Ns ago" indicator re-renders to stay current. */
const RELATIVE_TIME_TICK_MS = 1000;

/** Formats a millisecond duration as a short "Ns ago" / "Nm ago" string. */
export function formatRelativeTime(elapsedMs: number): string {
  const seconds = Math.max(0, Math.round(elapsedMs / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  return `${minutes}m ago`;
}

/**
 * Shared "Refresh button + Updated Ns ago + background auto-refresh"
 * plumbing, factored out of ProjectsPage so ProjectDetailPage (and future
 * pages) can reuse the exact same pattern instead of re-implementing it.
 *
 * Bump `reload` (via `refresh()`) to re-run a fetch effect that depends on
 * it; call `markUpdated()` once that fetch resolves so the "Updated Ns ago"
 * clock resets. `now` ticks every second so relative-time strings computed
 * from it stay live without extra intervals in the caller.
 *
 * `autoRefreshMs` is the page's built-in cadence; the user's dashboard-wide
 * auto-refresh preference (header control, persisted in localStorage) overrides
 * it: "Off" (0) stops background polling entirely, an explicit interval replaces
 * it, and "Default" keeps this page cadence. The manual `refresh()` always works.
 */
export function useLiveRefresh(autoRefreshMs: number) {
  const { preference } = useAutoRefreshPreference();
  const effectiveMs = resolveAutoRefreshMs(preference, autoRefreshMs);
  const [reload, setReload] = useState(0);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), RELATIVE_TIME_TICK_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (effectiveMs <= 0) return; // Off: do not schedule any background poll.
    const id = setInterval(() => setReload((r) => r + 1), effectiveMs);
    return () => clearInterval(id);
  }, [effectiveMs]);

  return {
    reload,
    refresh: () => setReload((r) => r + 1),
    lastUpdated,
    markUpdated: () => setLastUpdated(Date.now()),
    now,
  };
}
