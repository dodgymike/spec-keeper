import { useCallback, useEffect, useRef, useState } from "react";
import { getChanges, getChangesHead, listEpics, listTasks } from "../api/client";
import type { Epic, ProjectNote, Task } from "../api/types";
import {
  clearCheckpoint,
  createCache,
  selectEpics,
  selectNotes,
  selectTasks,
  type DeltaCache,
} from "../lib/deltaCache";
import {
  hydrateDelta,
  syncDelta,
  type DeltaHydrateApi,
  type DeltaSyncApi,
} from "../lib/deltaSync";
import { resolveAutoRefreshMs, useAutoRefreshPreference } from "./autoRefresh";

/** How often the "Updated Ns ago" indicator re-renders to stay current. */
const RELATIVE_TIME_TICK_MS = 1000;

/** The real change-feed reads, injected into the pure `syncDelta` tick. */
const deltaApi: DeltaSyncApi = { getChangesHead, getChanges };

/** The real reads for a full cold-start / resync hydrate. */
const hydrateApi: DeltaHydrateApi = { getChangesHead, listTasks, listEpics };

export type DeltaRefreshStatus = "loading" | "ready" | "error";

/**
 * Live-refresh via the incremental change feed (UI-DELTA-8), replacing the
 * "refetch every list every tick" pattern of `useLiveRefresh`.
 *
 * COLD START (empty cache / no usable checkpoint): a FULL REST fetch hydrates
 * the cache and the cursor is pinned to the change-feed head — because the
 * change log only records mutations forward from when it went live, so a
 * `since=0` delta would omit every pre-existing task/epic. STEADY STATE: each
 * tick does the cheap thing — poll the head cursor and, only if it moved, pull
 * the delta page(s) since the cursor, fold them into the normalized cache,
 * persist the checkpoint, and re-render from the selectors. The idle case (head
 * unchanged) fetches no data at all.
 *
 * The poll cadence is the shared dashboard auto-refresh setting: "Off" stops
 * background polling entirely, an explicit interval replaces `autoRefreshMs`,
 * and "Default" keeps this page's cadence. `refresh()` always ticks on demand.
 *
 * NOTE: only tasks + epics are REST-hydrated (their DTOs carry the `public_id`
 * the feed keys on); notes arrive via deltas only until the note DTO exposes it.
 * A real full-resync policy is UI-DELTA-9 — but its mechanism (re-hydrate from
 * REST) is shared here: on a `full_resync_required` signal we re-hydrate.
 */
export function useDeltaRefresh(slug: string, autoRefreshMs: number) {
  const { preference } = useAutoRefreshPreference();
  const effectiveMs = resolveAutoRefreshMs(preference, autoRefreshMs);

  const [cache, setCache] = useState<DeltaCache>(createCache);
  const [status, setStatus] = useState<DeltaRefreshStatus>("loading");
  const [error, setError] = useState<Error | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  // The tick reads/advances the cache through a ref so one stable callback can
  // fold successive pages without being re-created on every render.
  const cacheRef = useRef(cache);
  cacheRef.current = cache;

  // Bumped on slug change / unmount so an in-flight tick's late result is
  // dropped instead of polluting the (possibly new) project's cache.
  const epochRef = useRef(0);

  const commit = useCallback((epoch: number, next: DeltaCache) => {
    if (epoch !== epochRef.current) return;
    cacheRef.current = next;
    setCache(next);
    setLastUpdated(Date.now());
  }, []);

  // Full REST hydrate — cold start and the full_resync_required fallback.
  const hydrate = useCallback(
    async (epoch: number) => {
      const fresh = await hydrateDelta(slug, hydrateApi);
      commit(epoch, fresh);
    },
    [slug, commit],
  );

  // One steady-state tick: cheap head-poll, delta-fetch + fold only on advance.
  const runTick = useCallback(async () => {
    const epoch = epochRef.current;
    try {
      const outcome = await syncDelta(slug, cacheRef.current, deltaApi);
      if (outcome.fullResyncRequired) {
        // TODO(UI-DELTA-9): a real full-resync policy. Safe fallback: the cursor
        // predates the retained window, so drop the checkpoint and re-hydrate
        // the whole cache from REST (the shared bootstrap mechanism).
        clearCheckpoint(slug);
        await hydrate(epoch);
      } else if (outcome.advanced) {
        commit(epoch, outcome.cache);
      }
      if (epoch !== epochRef.current) return;
      setStatus("ready");
      setError(null);
    } catch (err) {
      if (epoch !== epochRef.current) return;
      setError(err instanceof Error ? err : new Error(String(err)));
      // Keep showing the last-good cache on a transient poll failure; only the
      // very first load (nothing rendered yet) surfaces the error state.
      setStatus((prev) => (prev === "ready" ? prev : "error"));
    }
  }, [slug, hydrate, commit]);

  // Manual refresh + reset-and-hydrate whenever the project changes.
  const refresh = useCallback(async () => {
    const epoch = epochRef.current;
    try {
      await hydrate(epoch);
      if (epoch !== epochRef.current) return;
      setStatus("ready");
      setError(null);
    } catch (err) {
      if (epoch !== epochRef.current) return;
      setError(err instanceof Error ? err : new Error(String(err)));
      setStatus((prev) => (prev === "ready" ? prev : "error"));
    }
  }, [hydrate]);

  useEffect(() => {
    epochRef.current += 1;
    const fresh = createCache();
    cacheRef.current = fresh;
    setCache(fresh);
    setStatus("loading");
    setError(null);
    setLastUpdated(null);
    void refresh();
    return () => {
      epochRef.current += 1; // invalidate any in-flight tick on unmount / slug change
    };
  }, [slug, refresh]);

  // Background poll cadence (driven by the shared auto-refresh preference).
  useEffect(() => {
    if (effectiveMs <= 0) return; // Off: no background polling.
    const id = setInterval(() => void runTick(), effectiveMs);
    return () => clearInterval(id);
  }, [effectiveMs, runTick]);

  // Keep relative-time strings live without callers wiring their own interval.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), RELATIVE_TIME_TICK_MS);
    return () => clearInterval(id);
  }, []);

  const tasks: Task[] = selectTasks(cache);
  const epics: Epic[] = selectEpics(cache);
  const notes: ProjectNote[] = selectNotes(cache);

  return { cache, tasks, epics, notes, status, error, lastUpdated, now, refresh };
}
