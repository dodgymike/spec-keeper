/**
 * Client-side delta cache + checkpoint store (UI-DELTA-7).
 *
 * A per-project, NORMALIZED store of the entities the dashboard renders,
 * built by folding the server's ascending change feed (`ChangeEntry[]`, see
 * `../api/types.ts`) into a set of `pubid -> snapshot` maps. Paired with a
 * tiny `{ slug -> cursor }` checkpoint persisted to `localStorage` so a reload
 * can resume incrementally instead of re-fetching every list.
 *
 * This module is deliberately FRAMEWORK-LIGHT and PURE (no React, no fetch):
 *   - `applyChanges` returns a NEW cache and never mutates its input, so a
 *     hook (UI-DELTA-8) can hold it in React state and get referential change
 *     detection for free.
 *   - the apply/evict/coalesce/ordering logic is unit-tested in
 *     `deltaCache.test.ts` — this is exactly where cache bugs hide.
 *
 * NOT in scope here: the useLiveRefresh rewire (UI-DELTA-8) and the
 * full-resync orchestration (UI-DELTA-9). This module is not wired into any
 * page yet.
 */

import type {
  ChangeEntityType,
  ChangeEntry,
  Epic,
  ProjectNote,
  Task,
} from "../api/types";

/**
 * The normalized cache. Each entity kind is a `pubid -> snapshot` map; the
 * snapshots are stored `unknown` because the feed is polymorphic over
 * `entity_type` — the typed selectors below narrow each bucket when reading.
 *
 * `seqByEntity` records the highest `seq` applied per entity (keyed
 * `"<type>:<pubid>"`); it is the ordering guard that makes `applyChanges`
 * idempotent and safe against duplicate/out-of-order entries. `cursor` is the
 * highest `seq` folded in so far (what a checkpoint should be saved as).
 */
export interface DeltaCache {
  entities: Record<ChangeEntityType, Record<string, unknown>>;
  seqByEntity: Record<string, number>;
  cursor: number;
}

/** A fresh, empty cache (cursor 0 = "have seen nothing"). */
export function createCache(): DeltaCache {
  return {
    entities: { task: {}, epic: {}, note: {}, commit: {}, relation: {} },
    seqByEntity: {},
    cursor: 0,
  };
}

function entityKey(type: ChangeEntityType, pubid: string): string {
  return `${type}:${pubid}`;
}

/**
 * Fold an ascending page of changes into the cache and return a NEW cache
 * (the input is never mutated).
 *
 * Rules (per the pinned contract):
 *   - process entries in the order given (expected ascending by `seq`);
 *   - `op="upsert"`  → `cache[type][pubid] = snapshot`;
 *   - `op="delete"`  → EVICT `cache[type][pubid]`;
 *   - COALESCE multiple entries for the same entity to the LATEST — highest
 *     `seq` wins (processing ascending + overwrite achieves this naturally);
 *   - ORDERING GUARD: NEVER apply a `seq` <= the highest already applied for
 *     that entity. A duplicate or out-of-order (lower) entry is skipped, so a
 *     replayed page can never regress the cache to an older snapshot.
 *
 * `cursor` advances to the highest `seq` seen. Note it only moves forward and,
 * because entries are per-entity guarded, re-applying the same page is a no-op.
 */
export function applyChanges(cache: DeltaCache, changes: readonly ChangeEntry[]): DeltaCache {
  if (changes.length === 0) return cache;

  // Shallow-copy only what we touch so the input cache stays untouched (pure).
  const entities: Record<ChangeEntityType, Record<string, unknown>> = {
    task: { ...cache.entities.task },
    epic: { ...cache.entities.epic },
    note: { ...cache.entities.note },
    commit: { ...cache.entities.commit },
    relation: { ...cache.entities.relation },
  };
  const seqByEntity = { ...cache.seqByEntity };
  let cursor = cache.cursor;

  for (const entry of changes) {
    const key = entityKey(entry.entity_type, entry.entity_pubid);
    const lastSeq = seqByEntity[key];
    if (lastSeq !== undefined && entry.seq <= lastSeq) {
      // Ordering guard: a higher-or-equal seq already applied for this entity.
      // Never regress — skip this stale/duplicate entry.
      continue;
    }
    seqByEntity[key] = entry.seq;

    const bucket = entities[entry.entity_type];
    if (entry.op === "delete") {
      delete bucket[entry.entity_pubid];
    } else {
      bucket[entry.entity_pubid] = entry.snapshot;
    }

    if (entry.seq > cursor) cursor = entry.seq;
  }

  return { entities, seqByEntity, cursor };
}

// ---- Selectors ---------------------------------------------------------
// Read typed, sorted lists out of the normalized cache. Sort keys mirror what
// the list pages expect: tasks/epics by `position` ascending (as
// ProjectDetailPage sorts), notes newest-first by `created_at` (as the notes
// feed returns). Selectors are pure and allocate a fresh array each call.

/** Tasks, sorted by `position` asc then `display_id` for a stable order. */
export function selectTasks(cache: DeltaCache): Task[] {
  const tasks = Object.values(cache.entities.task) as Task[];
  return tasks.sort(
    (a, b) => a.position - b.position || a.display_id.localeCompare(b.display_id),
  );
}

/** Epics, sorted by `position` asc then `key` for a stable order. */
export function selectEpics(cache: DeltaCache): Epic[] {
  const epics = Object.values(cache.entities.epic) as Epic[];
  return epics.sort((a, b) => a.position - b.position || a.key.localeCompare(b.key));
}

/** Notes, newest first (by `created_at` desc), matching the notes feed. */
export function selectNotes(cache: DeltaCache): ProjectNote[] {
  const notes = Object.values(cache.entities.note) as ProjectNote[];
  return notes.sort((a, b) => b.created_at.localeCompare(a.created_at));
}

// ---- Checkpoint store --------------------------------------------------
// A per-project `{ slug -> cursor }` checkpoint (a single small non-negative
// integer per project) persisted in localStorage. All access is guarded with
// try/catch so private-mode / disabled-storage never throws — persistence is a
// best-effort optimization, not a correctness dependency.

/** localStorage key prefix for a project's delta cursor. */
const CHECKPOINT_PREFIX = "spec.delta.cursor.";

function checkpointKey(slug: string): string {
  return `${CHECKPOINT_PREFIX}${slug}`;
}

/**
 * Persist `cursor` for `slug`. Only non-negative integers are stored (the
 * feed's `seq` domain); anything else is ignored. Never throws.
 */
export function saveCheckpoint(slug: string, cursor: number): void {
  if (!Number.isInteger(cursor) || cursor < 0) return;
  try {
    window.localStorage.setItem(checkpointKey(slug), String(cursor));
  } catch {
    /* localStorage unavailable (private mode / disabled) — skip persistence. */
  }
}

/**
 * Read the persisted cursor for `slug`, or `null` when absent, corrupt, or
 * storage is unavailable. A `null` return means "resync from 0". Never throws.
 */
export function loadCheckpoint(slug: string): number | null {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(checkpointKey(slug));
  } catch {
    return null; // storage unavailable — behave as if no checkpoint exists.
  }
  if (raw === null) return null;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed < 0) return null; // corrupt value.
  return parsed;
}

/** Drop the persisted cursor for `slug` (e.g. before a full resync). Never throws. */
export function clearCheckpoint(slug: string): void {
  try {
    window.localStorage.removeItem(checkpointKey(slug));
  } catch {
    /* storage unavailable — nothing to clear. */
  }
}
