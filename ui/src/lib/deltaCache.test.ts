/**
 * Unit tests for the pure delta-cache logic (UI-DELTA-7).
 *
 * Focus: apply / evict / coalesce / out-of-order guard / cursor advance /
 * selector ordering / checkpoint persistence. These are the failure modes
 * where a cache silently serves stale data, so they are exercised directly.
 *
 * The checkpoint tests install a tiny in-memory `window.localStorage` fake so
 * the suite runs under vitest's default (node) environment without pulling in
 * jsdom — deltaCache only touches `window.localStorage`, nothing else DOM.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ChangeEntry } from "../api/types";
import {
  applyChanges,
  CACHE_SCHEMA_VERSION,
  clearCheckpoint,
  createCache,
  loadCheckpoint,
  markSchemaCurrent,
  saveCheckpoint,
  schemaVersionStale,
  selectEpics,
  selectNotes,
  selectTasks,
} from "./deltaCache";

/** Build a change entry with sensible defaults; override what a test cares about. */
function change(partial: Partial<ChangeEntry> & Pick<ChangeEntry, "seq" | "entity_pubid">): ChangeEntry {
  return {
    entity_type: "task",
    op: "upsert",
    version: 1,
    occurred_at: "2026-07-24T00:00:00Z",
    snapshot: { pubid: partial.entity_pubid },
    ...partial,
  };
}

describe("applyChanges", () => {
  it("upserts a snapshot into the right normalized bucket", () => {
    const cache = applyChanges(createCache(), [
      change({ seq: 1, entity_type: "task", entity_pubid: "t1", snapshot: { title: "A" } }),
    ]);
    expect(cache.entities.task["t1"]).toEqual({ title: "A" });
    expect(cache.cursor).toBe(1);
  });

  it("delete evicts the entity from the cache", () => {
    let cache = applyChanges(createCache(), [
      change({ seq: 1, entity_pubid: "t1", snapshot: { title: "A" } }),
    ]);
    cache = applyChanges(cache, [change({ seq: 2, entity_pubid: "t1", op: "delete" })]);
    expect("t1" in cache.entities.task).toBe(false);
    expect(cache.cursor).toBe(2);
  });

  it("coalesces multiple entries for one entity to the latest (highest seq wins)", () => {
    const cache = applyChanges(createCache(), [
      change({ seq: 1, entity_pubid: "t1", snapshot: { title: "v1" } }),
      change({ seq: 2, entity_pubid: "t1", snapshot: { title: "v2" } }),
      change({ seq: 3, entity_pubid: "t1", snapshot: { title: "v3" } }),
    ]);
    expect(cache.entities.task["t1"]).toEqual({ title: "v3" });
    expect(cache.cursor).toBe(3);
  });

  it("guards ordering: a lower seq after a higher one is skipped (never regresses)", () => {
    let cache = applyChanges(createCache(), [
      change({ seq: 5, entity_pubid: "t1", snapshot: { title: "new" } }),
    ]);
    // Replay a stale, lower-seq entry for the same entity.
    cache = applyChanges(cache, [
      change({ seq: 3, entity_pubid: "t1", snapshot: { title: "stale" } }),
    ]);
    expect(cache.entities.task["t1"]).toEqual({ title: "new" });
    expect(cache.cursor).toBe(5);
  });

  it("is idempotent: re-applying the same page is a no-op", () => {
    const page = [
      change({ seq: 1, entity_pubid: "t1", snapshot: { title: "A" } }),
      change({ seq: 2, entity_pubid: "t2", op: "delete" }),
    ];
    const once = applyChanges(createCache(), page);
    const twice = applyChanges(once, page);
    expect(twice.entities.task).toEqual(once.entities.task);
    expect(twice.cursor).toBe(once.cursor);
  });

  it("a stale delete does not evict a newer upsert", () => {
    let cache = applyChanges(createCache(), [
      change({ seq: 4, entity_pubid: "t1", snapshot: { title: "current" } }),
    ]);
    cache = applyChanges(cache, [change({ seq: 2, entity_pubid: "t1", op: "delete" })]);
    expect(cache.entities.task["t1"]).toEqual({ title: "current" });
  });

  it("does not mutate the input cache (pure)", () => {
    const base = createCache();
    const next = applyChanges(base, [change({ seq: 1, entity_pubid: "t1" })]);
    expect(base.cursor).toBe(0);
    expect(Object.keys(base.entities.task)).toHaveLength(0);
    expect(next).not.toBe(base);
  });

  it("returns the same reference for an empty page", () => {
    const base = createCache();
    expect(applyChanges(base, [])).toBe(base);
  });

  it("routes entries to independent buckets by entity_type", () => {
    const cache = applyChanges(createCache(), [
      change({ seq: 1, entity_type: "task", entity_pubid: "x", snapshot: { k: "task" } }),
      change({ seq: 2, entity_type: "epic", entity_pubid: "x", snapshot: { k: "epic" } }),
      change({ seq: 3, entity_type: "note", entity_pubid: "x", snapshot: { k: "note" } }),
    ]);
    // Same pubid across types must not collide.
    expect(cache.entities.task["x"]).toEqual({ k: "task" });
    expect(cache.entities.epic["x"]).toEqual({ k: "epic" });
    expect(cache.entities.note["x"]).toEqual({ k: "note" });
  });
});

describe("selectors", () => {
  it("selectTasks sorts by position then display_id", () => {
    const cache = applyChanges(createCache(), [
      change({ seq: 1, entity_pubid: "a", snapshot: { position: 2, display_id: "A-2" } }),
      change({ seq: 2, entity_pubid: "b", snapshot: { position: 1, display_id: "A-1" } }),
      change({ seq: 3, entity_pubid: "c", snapshot: { position: 1, display_id: "A-0" } }),
    ]);
    expect(selectTasks(cache).map((t) => t.display_id)).toEqual(["A-0", "A-1", "A-2"]);
  });

  it("selectEpics sorts by position then key", () => {
    const cache = applyChanges(createCache(), [
      change({ seq: 1, entity_type: "epic", entity_pubid: "a", snapshot: { position: 5, key: "E5" } }),
      change({ seq: 2, entity_type: "epic", entity_pubid: "b", snapshot: { position: 1, key: "E1" } }),
    ]);
    expect(selectEpics(cache).map((e) => e.key)).toEqual(["E1", "E5"]);
  });

  it("selectNotes sorts newest-first by created_at", () => {
    const cache = applyChanges(createCache(), [
      change({ seq: 1, entity_type: "note", entity_pubid: "a", snapshot: { created_at: "2026-01-01T00:00:00Z", body: "old" } }),
      change({ seq: 2, entity_type: "note", entity_pubid: "b", snapshot: { created_at: "2026-06-01T00:00:00Z", body: "new" } }),
    ]);
    expect(selectNotes(cache).map((n) => n.body)).toEqual(["new", "old"]);
  });
});

describe("checkpoint store", () => {
  let store: Record<string, string>;

  beforeEach(() => {
    store = {};
    // Minimal Storage-shaped fake; only the methods deltaCache uses.
    const fake = {
      getItem: (k: string) => (k in store ? store[k] : null),
      setItem: (k: string, v: string) => {
        store[k] = v;
      },
      removeItem: (k: string) => {
        delete store[k];
      },
    };
    (globalThis as unknown as { window: { localStorage: typeof fake } }).window = { localStorage: fake };
  });

  afterEach(() => {
    delete (globalThis as unknown as { window?: unknown }).window;
  });

  it("round-trips a cursor per slug", () => {
    saveCheckpoint("proj-a", 42);
    saveCheckpoint("proj-b", 7);
    expect(loadCheckpoint("proj-a")).toBe(42);
    expect(loadCheckpoint("proj-b")).toBe(7);
  });

  it("returns null when no checkpoint exists", () => {
    expect(loadCheckpoint("never-saved")).toBeNull();
  });

  it("refuses to persist non-integer or negative cursors", () => {
    saveCheckpoint("p", -1);
    saveCheckpoint("p", 1.5);
    saveCheckpoint("p", NaN);
    expect(loadCheckpoint("p")).toBeNull();
  });

  it("treats a corrupt stored value as absent", () => {
    store["spec.delta.cursor.p"] = "not-a-number";
    expect(loadCheckpoint("p")).toBeNull();
  });

  it("clearCheckpoint removes the persisted cursor", () => {
    saveCheckpoint("p", 9);
    clearCheckpoint("p");
    expect(loadCheckpoint("p")).toBeNull();
  });

  it("never throws when storage is unavailable (private mode)", () => {
    const throwing = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    };
    (globalThis as unknown as { window: { localStorage: typeof throwing } }).window = { localStorage: throwing };
    expect(() => saveCheckpoint("p", 1)).not.toThrow();
    expect(loadCheckpoint("p")).toBeNull();
    expect(() => clearCheckpoint("p")).not.toThrow();
  });
});

describe("cache-schema version (full-resync trigger)", () => {
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
    (globalThis as unknown as { window: { localStorage: typeof fake } }).window = { localStorage: fake };
  });

  afterEach(() => {
    delete (globalThis as unknown as { window?: unknown }).window;
  });

  it("reports stale when no version was ever recorded (pre-versioning client)", () => {
    expect(schemaVersionStale()).toBe(true);
  });

  it("is not stale after markSchemaCurrent records the current version", () => {
    markSchemaCurrent();
    expect(schemaVersionStale()).toBe(false);
  });

  it("reports stale when the recorded version differs from the current build", () => {
    store["spec.delta.schema"] = String(CACHE_SCHEMA_VERSION + 1);
    expect(schemaVersionStale()).toBe(true);
  });

  it("treats unavailable storage as stale (safe → resync)", () => {
    const throwing = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {},
    };
    (globalThis as unknown as { window: { localStorage: typeof throwing } }).window = { localStorage: throwing };
    expect(schemaVersionStale()).toBe(true);
    expect(() => markSchemaCurrent()).not.toThrow();
  });
});
