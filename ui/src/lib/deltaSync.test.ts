/**
 * Tests for the one-tick change-feed sync (UI-DELTA-8, `syncDelta`).
 *
 * Covers the three behaviours the live-refresh rewire hinges on:
 *   - IDLE SKIP: head unchanged → no `getChanges` call, cache untouched;
 *   - FOLD ON ADVANCE: head moved → deltas folded, cursor advances, selectors
 *     reflect the new entities;
 *   - CHECKPOINT PERSISTED: an advancing tick writes the new cursor to storage.
 * Plus truncated paging (catch-up) and the `full_resync_required` signal.
 *
 * Uses a fake `DeltaSyncApi` (call-counting) and the same in-memory
 * `window.localStorage` fake as `deltaCache.test.ts`, so it runs under vitest's
 * default node environment without jsdom.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChangeEntry, ChangesHead, ChangesPage, Epic, Task } from "../api/types";
import {
  createCache,
  loadCheckpoint,
  saveCheckpoint,
  selectEpics,
  selectTasks,
} from "./deltaCache";
import {
  hydrateDelta,
  resyncDelta,
  syncDelta,
  type DeltaHydrateApi,
  type DeltaResyncApi,
  type DeltaSyncApi,
} from "./deltaSync";

/** A minimal upsert entry for a task snapshot (only the selector fields matter). */
function taskUpsert(seq: number, pubid: string, position: number): ChangeEntry {
  return {
    seq,
    entity_type: "task",
    entity_pubid: pubid,
    op: "upsert",
    version: 1,
    occurred_at: "2026-07-24T00:00:00Z",
    snapshot: { public_id: pubid, display_id: pubid, position },
  };
}

function head(cursor: number, minRetained = 0): ChangesHead {
  return { cursor, min_retained_seq: minRetained };
}

function page(overrides: Partial<ChangesPage>): ChangesPage {
  return {
    cursor: 0,
    changes: [],
    truncated: false,
    full_resync_required: false,
    min_retained_seq: 0,
    ...overrides,
  };
}

describe("syncDelta", () => {
  let store: Record<string, string>;

  beforeEach(() => {
    store = {};
    const fake = {
      getItem: (k: string) => (k in store ? store[k] : null),
      setItem: (k: string, v: string) => {
        store[k] = v;
      },
      removeItem: (k: string) => {
        delete store[k];
      },
    };
    (globalThis as unknown as { window: { localStorage: typeof fake } }).window = {
      localStorage: fake,
    };
  });

  afterEach(() => {
    delete (globalThis as unknown as { window?: unknown }).window;
  });

  it("idle-skips (no getChanges) when the head has not moved", async () => {
    const cache = createCache(); // cursor 0
    const api: DeltaSyncApi = {
      getChangesHead: vi.fn(async () => head(0)),
      getChanges: vi.fn(async () => page({})),
    };

    const outcome = await syncDelta("proj", cache, api);

    expect(api.getChangesHead).toHaveBeenCalledTimes(1);
    expect(api.getChanges).not.toHaveBeenCalled();
    expect(outcome.advanced).toBe(false);
    expect(outcome.fullResyncRequired).toBe(false);
    expect(outcome.cache).toBe(cache); // same reference, untouched
  });

  it("folds the delta and advances the cursor when the head moved", async () => {
    const cache = createCache();
    const api: DeltaSyncApi = {
      getChangesHead: vi.fn(async () => head(2)),
      getChanges: vi.fn(async (_slug: string, since: number) =>
        page({
          cursor: 2,
          changes: [taskUpsert(1, "T-1", 1), taskUpsert(2, "T-2", 2)],
          truncated: false,
          // echo the requested cursor so the assertion below is meaningful
          min_retained_seq: since,
        }),
      ),
    };

    const outcome = await syncDelta("proj", cache, api);

    expect(api.getChanges).toHaveBeenCalledTimes(1);
    expect(api.getChanges).toHaveBeenCalledWith("proj", 0, expect.any(Number));
    expect(outcome.advanced).toBe(true);
    expect(outcome.cache.cursor).toBe(2);
    expect(selectTasks(outcome.cache).map((t) => t.display_id)).toEqual(["T-1", "T-2"]);
  });

  it("persists the new checkpoint after an advancing tick", async () => {
    const cache = createCache();
    const api: DeltaSyncApi = {
      getChangesHead: vi.fn(async () => head(5)),
      getChanges: vi.fn(async () =>
        page({ cursor: 5, changes: [taskUpsert(5, "T-5", 1)] }),
      ),
    };

    await syncDelta("proj", cache, api);

    expect(loadCheckpoint("proj")).toBe(5);
  });

  it("does not persist a checkpoint on an idle tick", async () => {
    const api: DeltaSyncApi = {
      getChangesHead: vi.fn(async () => head(0)),
      getChanges: vi.fn(async () => page({})),
    };

    await syncDelta("proj", createCache(), api);

    expect(loadCheckpoint("proj")).toBeNull();
  });

  it("pages forward while truncated until caught up to the head", async () => {
    const getChanges = vi
      .fn<DeltaSyncApi["getChanges"]>()
      .mockResolvedValueOnce(
        page({ cursor: 1, changes: [taskUpsert(1, "T-1", 1)], truncated: true }),
      )
      .mockResolvedValueOnce(
        page({ cursor: 2, changes: [taskUpsert(2, "T-2", 2)], truncated: false }),
      );
    const api: DeltaSyncApi = {
      getChangesHead: vi.fn(async () => head(2)),
      getChanges,
    };

    const outcome = await syncDelta("proj", createCache(), api, 1);

    expect(getChanges).toHaveBeenCalledTimes(2);
    expect(getChanges).toHaveBeenNthCalledWith(1, "proj", 0, 1);
    expect(getChanges).toHaveBeenNthCalledWith(2, "proj", 1, 1);
    expect(outcome.cache.cursor).toBe(2);
    expect(selectTasks(outcome.cache)).toHaveLength(2);
  });

  it("signals full_resync_required without mutating the cache", async () => {
    const cache = createCache();
    const api: DeltaSyncApi = {
      getChangesHead: vi.fn(async () => head(10, 5)),
      getChanges: vi.fn(async () =>
        page({ cursor: 10, full_resync_required: true, min_retained_seq: 5 }),
      ),
    };

    const outcome = await syncDelta("proj", cache, api);

    expect(outcome.fullResyncRequired).toBe(true);
    expect(outcome.advanced).toBe(false);
    expect(outcome.cache).toBe(cache);
    expect(loadCheckpoint("proj")).toBeNull();
  });
});

/** A minimal REST task DTO (only selector + key fields matter here). */
function task(pubid: string, position: number): Task {
  return { public_id: pubid, display_id: pubid, position } as Task;
}
/** A minimal REST epic DTO. */
function epic(pubid: string, position: number): Epic {
  return { public_id: pubid, key: pubid, position } as Epic;
}

describe("hydrateDelta (cold start)", () => {
  let store: Record<string, string>;

  beforeEach(() => {
    store = {};
    const fake = {
      getItem: (k: string) => (k in store ? store[k] : null),
      setItem: (k: string, v: string) => {
        store[k] = v;
      },
      removeItem: (k: string) => {
        delete store[k];
      },
    };
    (globalThis as unknown as { window: { localStorage: typeof fake } }).window = {
      localStorage: fake,
    };
  });

  afterEach(() => {
    delete (globalThis as unknown as { window?: unknown }).window;
  });

  it("full-fetches tasks+epics over REST and pins the cursor to head (NOT a since=0 delta)", async () => {
    const api: DeltaHydrateApi = {
      getChangesHead: vi.fn(async () => head(7)),
      listTasks: vi.fn(async () => [task("T-2", 2), task("T-1", 1)]),
      listEpics: vi.fn(async () => [epic("E-1", 1)]),
    };

    const cache = await hydrateDelta("proj", api);

    // Bootstrap uses the REST list path, not getChanges(since=0).
    expect(api.getChangesHead).toHaveBeenCalledTimes(1);
    expect(api.listTasks).toHaveBeenCalledTimes(1);
    expect(api.listEpics).toHaveBeenCalledTimes(1);
    // Pre-existing entities are present, sorted by the selectors.
    expect(selectTasks(cache).map((t) => t.display_id)).toEqual(["T-1", "T-2"]);
    expect(selectEpics(cache).map((e) => e.key)).toEqual(["E-1"]);
    // Cursor pinned to the head captured around the fetch, and persisted.
    expect(cache.cursor).toBe(7);
    expect(loadCheckpoint("proj")).toBe(7);
  });

  it("a subsequent delta folds onto the hydrated cache and replaces by public_id", async () => {
    const hydrateApi: DeltaHydrateApi = {
      getChangesHead: vi.fn(async () => head(7)),
      listTasks: vi.fn(async () => [task("T-1", 1)]),
      listEpics: vi.fn(async () => []),
    };
    const cache = await hydrateDelta("proj", hydrateApi);

    const syncApi: DeltaSyncApi = {
      getChangesHead: vi.fn(async () => head(8)),
      getChanges: vi.fn(async (_slug: string, since: number) =>
        page({ cursor: 8, changes: [taskUpsert(8, "T-1", 9)], min_retained_seq: since }),
      ),
    };
    const outcome = await syncDelta("proj", cache, syncApi);

    // Same public_id → replaced (not duplicated); cursor advanced past head.
    const tasks = selectTasks(outcome.cache);
    expect(tasks).toHaveLength(1);
    expect(tasks[0].position).toBe(9);
    expect(outcome.cache.cursor).toBe(8);
  });
});

describe("resyncDelta (full-resync fallback)", () => {
  let store: Record<string, string>;

  beforeEach(() => {
    store = {};
    const fake = {
      getItem: (k: string) => (k in store ? store[k] : null),
      setItem: (k: string, v: string) => {
        store[k] = v;
      },
      removeItem: (k: string) => {
        delete store[k];
      },
    };
    (globalThis as unknown as { window: { localStorage: typeof fake } }).window = {
      localStorage: fake,
    };
  });

  afterEach(() => {
    delete (globalThis as unknown as { window?: unknown }).window;
  });

  it("drops the stale checkpoint, hydrates from REST, and replays deltas since the captured head", async () => {
    // A stale checkpoint from the offline window predates the retained tail.
    saveCheckpoint("proj", 3);

    const api: DeltaResyncApi = {
      // Head captured BEFORE the hydrate list-fetch = 10; the replay poll sees no
      // further movement (head still 10 == hydrated cursor → idle, no getChanges).
      getChangesHead: vi.fn(async () => head(10)),
      getChanges: vi.fn(async () => page({})),
      listTasks: vi.fn(async () => [task("T-1", 1)]),
      listEpics: vi.fn(async () => [epic("E-1", 1)]),
    };

    const outcome = await resyncDelta("proj", api);

    // Rebuilt from REST (drop + hydrate), not from the stale since=3 cursor.
    expect(api.listTasks).toHaveBeenCalledTimes(1);
    expect(api.listEpics).toHaveBeenCalledTimes(1);
    expect(api.getChanges).not.toHaveBeenCalled(); // replay was idle at head
    expect(selectTasks(outcome.cache).map((t) => t.display_id)).toEqual(["T-1"]);
    expect(selectEpics(outcome.cache).map((e) => e.key)).toEqual(["E-1"]);
    // Checkpoint ends at the captured head, never the stale 3.
    expect(outcome.advanced).toBe(true);
    expect(outcome.cache.cursor).toBe(10);
    expect(loadCheckpoint("proj")).toBe(10);
  });

  it("does not lose a delta that lands DURING the resync (captured-head replay)", async () => {
    // Head is captured at 10 before the (slow, multi-page-capable) REST hydrate.
    // A mutation lands mid-hydrate → the head has moved to 11 by the replay poll,
    // and getChanges(since=10) carries that new task, which must survive.
    const getChangesHead = vi
      .fn<DeltaResyncApi["getChangesHead"]>()
      .mockResolvedValueOnce(head(10)) // captured by hydrateDelta (before fetch)
      .mockResolvedValue(head(11)); // replay poll sees the mid-resync mutation
    const api: DeltaResyncApi = {
      getChangesHead,
      getChanges: vi.fn(async (_slug: string, since: number) =>
        page({ cursor: 11, changes: [taskUpsert(11, "T-NEW", 5)], min_retained_seq: since }),
      ),
      listTasks: vi.fn(async () => [task("T-1", 1)]),
      listEpics: vi.fn(async () => []),
    };

    const outcome = await resyncDelta("proj", api);

    // The replay pulled deltas since the CAPTURED head (10), not since 0/11.
    expect(api.getChanges).toHaveBeenCalledWith("proj", 10, expect.any(Number));
    // Both the hydrated task and the mid-resync task are present — nothing lost.
    expect(selectTasks(outcome.cache).map((t) => t.display_id)).toEqual(["T-1", "T-NEW"]);
    // Checkpoint ends at the TRUE head after replay.
    expect(outcome.cache.cursor).toBe(11);
    expect(loadCheckpoint("proj")).toBe(11);
    expect(outcome.fullResyncRequired).toBe(false);
  });

  it("bubbles a full_resync_required that recurs on the replay (retention pruned again)", async () => {
    const getChangesHead = vi
      .fn<DeltaResyncApi["getChangesHead"]>()
      .mockResolvedValueOnce(head(10))
      .mockResolvedValue(head(20, 15));
    const api: DeltaResyncApi = {
      getChangesHead,
      getChanges: vi.fn(async () =>
        page({ cursor: 20, full_resync_required: true, min_retained_seq: 15 }),
      ),
      listTasks: vi.fn(async () => [task("T-1", 1)]),
      listEpics: vi.fn(async () => []),
    };

    const outcome = await resyncDelta("proj", api);

    expect(outcome.fullResyncRequired).toBe(true);
    // Still returns the freshly hydrated cache (never a partial/empty view).
    expect(selectTasks(outcome.cache).map((t) => t.display_id)).toEqual(["T-1"]);
  });
});
