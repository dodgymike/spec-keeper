import { useCallback, useEffect, useRef, useState } from "react";
import { getChanges, getChangesHead, listEpics, listTasks } from "../api/client";
import type { Epic, ProjectNote, Task } from "../api/types";
import {
  clearCheckpoint,
  createCache,
  markSchemaCurrent,
  schemaVersionStale,
  selectEpics,
  selectNotes,
  selectTasks,
  type DeltaCache,
} from "../lib/deltaCache";
import {
  hydrateDelta,
  resyncDelta,
  syncDelta,
  type DeltaResyncApi,
} from "../lib/deltaSync";
import { resolveAutoRefreshMs, useAutoRefreshPreference } from "./autoRefresh";

/** How often the "Updated Ns ago" indicator re-renders to stay current. */
const RELATIVE_TIME_TICK_MS = 1000;

/** The real reads for every path — the cheap `syncDelta` tick, the cold-start
 *  `hydrateDelta`, and the `resyncDelta` self-heal (which needs both sets). */
const deltaApi: DeltaResyncApi = { getChangesHead, getChanges, listTasks, listEpics };

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
 *
 * FULL-RESYNC (UI-DELTA-9): when a tick reports `full_resync_required` (the
 * checkpoint predates the server's retained window) the hook self-heals via
 * `resyncDelta` — capture head → drop cache → REST hydrate → replay deltas since
 * the captured head — so no change is lost and stale/empty data is never shown.
 * A cache-schema bump (`schemaVersionStale`) is treated the same way: the stale
 * checkpoint is dropped on mount before the fresh hydrate.
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

  // Full REST hydrate — cold start bootstrap (checkpoint pinned to head).
  const hydrate = useCallback(
    async (epoch: number) => {
      const fresh = await hydrateDelta(slug, deltaApi);
      commit(epoch, fresh);
    },
    [slug, commit],
  );

  // Full-resync self-heal (UI-DELTA-9): drop the stale checkpoint, re-hydrate
  // from REST, then replay any deltas that landed during the hydrate. Used when a
  // tick reports `full_resync_required` (cursor predates the retained window).
  const resync = useCallback(
    async (epoch: number) => {
      const healed = await resyncDelta(slug, deltaApi);
      commit(epoch, healed.cache);
    },
    [slug, commit],
  );

  // One steady-state tick: cheap head-poll, delta-fetch + fold only on advance.
  const runTick = useCallback(async () => {
    const epoch = epochRef.current;
    try {
      const outcome = await syncDelta(slug, cacheRef.current, deltaApi);
      if (outcome.fullResyncRequired) {
        // The cursor predates the retained window; deltas cannot rebuild the
        // cache. Self-heal by dropping the checkpoint and rebuilding from REST,
        // replaying any change that lands during the resync (capture-head-first).
        await resync(epoch);
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
  }, [slug, resync, commit]);

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
    // A cache-schema bump invalidates any persisted checkpoint from an older
    // client — drop it (and record the current version) before the fresh hydrate
    // so a stale cursor is never reused as a delta `since` (UI-DELTA-9 §5.2).
    if (schemaVersionStale()) {
      clearCheckpoint(slug);
      markSchemaCurrent();
    }
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
