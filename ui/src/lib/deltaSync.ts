/**
 * One live-refresh tick against the per-project change feed (UI-DELTA-8).
 *
 * This is the CHEAP-IDLE core: a tick first polls the head cursor, and when it
 * has NOT moved the tick returns immediately without fetching any changes (the
 * common steady state). Only when the head has advanced does it pull the
 * ascending delta page(s) since the cache's cursor, fold them into the cache via
 * `applyChanges`, and persist the new checkpoint.
 *
 * Kept PURE and framework-free (no React, no direct `fetch`) so it is unit
 * testable by passing a fake `DeltaSyncApi` — the api surface is exactly the two
 * client calls from `../api/client.ts`. The React wiring lives in
 * `../hooks/useDeltaRefresh.ts`.
 *
 * NOT in scope (UI-DELTA-9): rebuilding the cache from the list endpoints when
 * `full_resync_required` is reported. Here we only SIGNAL it (see
 * `DeltaSyncOutcome.fullResyncRequired`) and leave the cache untouched.
 */

import type { ChangesHead, ChangesPage, Epic, Task, TaskListParams } from "../api/types";
import {
  applyChanges,
  saveCheckpoint,
  seedCache,
  type DeltaCache,
  type SeedEntity,
} from "./deltaCache";

/** Default delta page size. The server caps `limit` at 1000; 500 keeps a busy
 *  cold-start catch-up to a couple of round-trips without a huge single page. */
export const DELTA_PAGE_LIMIT = 500;

/** Default full-fetch cap for the REST hydrate — mirrors the list pages, whose
 *  endpoints paginate by default. */
export const HYDRATE_TASK_LIMIT = 1000;

/** The two change-feed reads a tick needs, injectable for tests. Mirrors the
 *  `getChangesHead` / `getChanges` signatures in `../api/client.ts`. */
export interface DeltaSyncApi {
  getChangesHead(slug: string): Promise<ChangesHead>;
  getChanges(slug: string, since: number, limit: number): Promise<ChangesPage>;
}

/** The reads a cold-start / full-resync HYDRATE needs: the change-feed head (to
 *  pin the cursor) plus the REST list endpoints that carry pre-existing
 *  entities. Only `task`/`epic` are seeded — their REST DTOs expose the
 *  `public_id` the feed keys on; the note DTO does not, so notes remain
 *  delta-only until it does (follow-up). Injectable for tests; mirrors
 *  `../api/client.ts`. */
export interface DeltaHydrateApi {
  getChangesHead(slug: string): Promise<ChangesHead>;
  listTasks(slug: string, params?: TaskListParams): Promise<Task[]>;
  listEpics(slug: string): Promise<Epic[]>;
}

/**
 * FULL cold-start / resync hydrate (UI-DELTA-8; reused by UI-DELTA-9).
 *
 * The change log only records mutations FORWARD from when it went live, so
 * `getChanges(since=0)` would render pre-existing tasks/epics as EMPTY. Instead:
 *   1. read the change-feed head FIRST (a lower bound captured around the fetch);
 *   2. full-fetch the current tasks + epics over REST;
 *   3. seed them into a fresh cache with `cursor = head`, and persist that
 *      checkpoint — so the next delta tick catches only genuinely newer changes.
 *
 * Returns the seeded cache. `taskLimit` bounds the task list (default
 * `HYDRATE_TASK_LIMIT`).
 */
export async function hydrateDelta(
  slug: string,
  api: DeltaHydrateApi,
  taskLimit: number = HYDRATE_TASK_LIMIT,
): Promise<DeltaCache> {
  // Head captured BEFORE the list fetch: any change landing during the fetch has
  // seq > head, so the next `getChanges(since=head)` re-applies it (never lost).
  const head = await api.getChangesHead(slug);
  const [epics, tasks] = await Promise.all([
    api.listEpics(slug),
    api.listTasks(slug, { limit: taskLimit }),
  ]);

  const seed: SeedEntity[] = [
    ...epics.map((epic) => ({ type: "epic" as const, pubid: epic.public_id, snapshot: epic })),
    ...tasks.map((task) => ({ type: "task" as const, pubid: task.public_id, snapshot: task })),
  ];
  const cache = seedCache(seed, head.cursor);
  saveCheckpoint(slug, cache.cursor);
  return cache;
}

/** The result of one tick. `cache` is the (possibly unchanged) cache to render
 *  from; `advanced` is true iff the cursor moved (so the caller should re-render
 *  + reset its "updated Ns ago" clock); `fullResyncRequired` bubbles the server
 *  signal that the cursor predates the retained window (UI-DELTA-9 handles it). */
export interface DeltaSyncOutcome {
  cache: DeltaCache;
  advanced: boolean;
  fullResyncRequired: boolean;
}

/**
 * Run one refresh tick and return the next cache.
 *
 *   1. Poll `getChangesHead`. If its `cursor` equals the cache cursor, DO
 *      NOTHING — return the same cache, `advanced: false`, no `getChanges` call.
 *   2. Otherwise page `getChanges(since = cursor)` (following `truncated` until
 *      caught up), folding each ascending page into the cache.
 *   3. On any page reporting `full_resync_required`, stop and return
 *      `fullResyncRequired: true` with whatever was folded so far untouched-safe
 *      (the caller must not treat a partial page as authoritative — UI-DELTA-9).
 *   4. When the cursor advanced, persist the checkpoint before returning.
 *
 * Pure w.r.t. the cache (never mutates the input); the only side effect is the
 * best-effort `saveCheckpoint` on advance, which never throws.
 */
export async function syncDelta(
  slug: string,
  cache: DeltaCache,
  api: DeltaSyncApi,
  limit: number = DELTA_PAGE_LIMIT,
): Promise<DeltaSyncOutcome> {
  const head = await api.getChangesHead(slug);
  if (head.cursor === cache.cursor) {
    // Idle: head has not moved since our last fold — the near-free common case.
    return { cache, advanced: false, fullResyncRequired: false };
  }

  let current = cache;
  // Page forward until the feed is not truncated (caught up to head) or a page
  // fails to advance the cursor (defensive stop against a non-advancing loop).
  for (;;) {
    const page = await api.getChanges(slug, current.cursor, limit);
    if (page.full_resync_required) {
      // The cursor predates the retained window; deltas cannot rebuild the
      // cache. Signal the caller (UI-DELTA-9) without corrupting the cache.
      return {
        cache,
        advanced: false,
        fullResyncRequired: true,
      };
    }
    const next = applyChanges(current, page.changes);
    const movedForward = next.cursor > current.cursor;
    current = next;
    if (!page.truncated || !movedForward) break;
  }

  const advanced = current.cursor !== cache.cursor;
  if (advanced) saveCheckpoint(slug, current.cursor);
  return { cache: current, advanced, fullResyncRequired: false };
}
