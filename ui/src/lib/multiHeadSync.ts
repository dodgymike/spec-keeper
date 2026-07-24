/**
 * Batched fan-out head poll (UI-DELTA-10).
 *
 * A dashboard that shows MANY projects would otherwise poll `/changes/head` once
 * per project every tick (an N-request fan-out). This folds that into ONE request:
 * `getProjectsHeads` returns the head cursor for every visible project, and this
 * module decides — purely — which projects actually advanced past their last-seen
 * checkpoint, so ONLY those get a follow-up per-project delta-fetch. Idle projects
 * (head unchanged) cost zero further requests.
 *
 * Kept framework-free (no React, no direct `fetch`) so the "only advanced projects
 * are delta-fetched" guarantee is unit-testable with a fake `MultiHeadApi`. The
 * single-project steady-state tick stays `syncDelta` (`./deltaSync`); this is only
 * the multi-project fan-out on top of it. It does NOT touch the full-resync
 * fallback (UI-DELTA-9).
 */
import type { ChangesHead } from "../api/types";

/** The one batched read a fan-out tick needs, injectable for tests. Mirrors
 *  `getProjectsHeads` in `../api/client.ts`: `slug -> head`. */
export interface MultiHeadApi {
  getProjectsHeads(): Promise<Record<string, ChangesHead>>;
}

/**
 * Given the freshly-polled per-project heads and the last-seen cursor per project,
 * return the slugs whose head advanced (cursor strictly greater than the recorded
 * checkpoint). A project with no prior checkpoint is treated as last-seen 0, so it
 * counts as advanced iff its head is > 0 (something to fetch); an untouched project
 * (head 0, no checkpoint) is a no-op. Projects absent from `heads` (e.g. no longer
 * visible to the caller) are dropped. Pure and order-preserving over `heads`.
 */
export function selectAdvancedProjects(
  heads: Record<string, ChangesHead>,
  checkpoints: Record<string, number>,
): string[] {
  const advanced: string[] = [];
  for (const [slug, head] of Object.entries(heads)) {
    const last = checkpoints[slug] ?? 0;
    if (head.cursor > last) advanced.push(slug);
  }
  return advanced;
}

/** The outcome of one fan-out tick. */
export interface MultiHeadSyncResult {
  /** Slugs whose head advanced this tick — exactly those `onAdvanced` was run for. */
  advanced: string[];
  /** Updated per-project checkpoints: every slug present in the polled map is set
   *  to its current head cursor (advanced or not); slugs absent from the map keep
   *  their prior checkpoint so a transient visibility blip never loses their cursor. */
  checkpoints: Record<string, number>;
}

/**
 * Run one fan-out tick: poll the batched head map ONCE, work out which projects
 * advanced past their checkpoint, and delta-fetch ONLY those via `onAdvanced`
 * (invoked once per advanced slug with its fresh head). Returns the advanced set
 * plus the updated checkpoints to persist for the next tick. Projects that didn't
 * move cost zero fetches — the single biggest fan-out win.
 *
 * Pure w.r.t. its inputs (never mutates `prev`); side effects are only whatever
 * `onAdvanced` performs.
 */
export async function syncMultiHead(
  prev: Record<string, number>,
  api: MultiHeadApi,
  onAdvanced: (slug: string, head: ChangesHead) => Promise<void>,
): Promise<MultiHeadSyncResult> {
  const heads = await api.getProjectsHeads();
  const advanced = selectAdvancedProjects(heads, prev);
  await Promise.all(advanced.map((slug) => onAdvanced(slug, heads[slug])));

  const checkpoints: Record<string, number> = { ...prev };
  for (const [slug, head] of Object.entries(heads)) {
    checkpoints[slug] = head.cursor;
  }
  return { advanced, checkpoints };
}
