/**
 * Tests for the batched fan-out head poll (UI-DELTA-10, `syncMultiHead`).
 *
 * The load-bearing guarantee is ONLY-ADVANCED-DELTA-FETCH: one `getProjectsHeads`
 * request decides which projects moved, and the injected per-project delta-fetch
 * runs for exactly those — never for idle projects. Also covers cold-start (no
 * checkpoints), checkpoint advancement, and that absent-from-map projects keep
 * their prior cursor.
 *
 * Uses a fake `MultiHeadApi` (call-counting) so it runs under vitest's default
 * node environment without jsdom or network.
 */
import { describe, expect, it, vi } from "vitest";

import type { ChangesHead } from "../api/types";
import {
  selectAdvancedProjects,
  syncMultiHead,
  type MultiHeadApi,
} from "./multiHeadSync";

function head(cursor: number, minRetained = 0): ChangesHead {
  return { cursor, min_retained_seq: minRetained };
}

function apiReturning(map: Record<string, ChangesHead>): {
  api: MultiHeadApi;
  calls: () => number;
} {
  const fn = vi.fn(async () => map);
  return { api: { getProjectsHeads: fn }, calls: () => fn.mock.calls.length };
}

describe("selectAdvancedProjects", () => {
  it("returns only slugs whose head cursor exceeds the checkpoint", () => {
    const heads = { a: head(5), b: head(2), c: head(0) };
    const checkpoints = { a: 3, b: 2 }; // a advanced (5>3); b unchanged (2==2)
    expect(selectAdvancedProjects(heads, checkpoints)).toEqual(["a"]);
  });

  it("treats a missing checkpoint as 0 (cold start): head>0 counts as advanced", () => {
    const heads = { a: head(4), b: head(0) };
    // No checkpoints at all: a advances (4>0), b does not (0>0 is false).
    expect(selectAdvancedProjects(heads, {})).toEqual(["a"]);
  });

  it("drops projects absent from the freshly-polled head map", () => {
    const heads = { a: head(9) };
    const checkpoints = { a: 1, gone: 1 };
    expect(selectAdvancedProjects(heads, checkpoints)).toEqual(["a"]);
  });
});

describe("syncMultiHead", () => {
  it("delta-fetches ONLY the advanced projects — one head request, no idle fetches", async () => {
    const { api, calls } = apiReturning({ a: head(5), b: head(2), c: head(7) });
    const fetched: string[] = [];
    const onAdvanced = vi.fn(async (slug: string) => {
      fetched.push(slug);
    });

    // b is caught up (2), a and c advanced.
    const result = await syncMultiHead({ a: 3, b: 2, c: 4 }, api, onAdvanced);

    // Exactly ONE batched head request for the whole fan-out.
    expect(calls()).toBe(1);
    // Only advanced projects were delta-fetched — b (idle) was skipped.
    expect(onAdvanced).toHaveBeenCalledTimes(2);
    expect(new Set(fetched)).toEqual(new Set(["a", "c"]));
    expect(fetched).not.toContain("b");
    expect(new Set(result.advanced)).toEqual(new Set(["a", "c"]));
  });

  it("passes each advanced project its fresh head to the delta-fetch", async () => {
    const { api } = apiReturning({ a: head(5, 1) });
    const seen: Record<string, ChangesHead> = {};
    await syncMultiHead({ a: 1 }, api, async (slug, h) => {
      seen[slug] = h;
    });
    expect(seen.a).toEqual(head(5, 1));
  });

  it("fetches nothing when no project advanced (the common idle tick)", async () => {
    const { api } = apiReturning({ a: head(3), b: head(3) });
    const onAdvanced = vi.fn(async () => {});
    const result = await syncMultiHead({ a: 3, b: 3 }, api, onAdvanced);
    expect(onAdvanced).not.toHaveBeenCalled();
    expect(result.advanced).toEqual([]);
  });

  it("advances checkpoints to the polled heads, keeping absent projects' cursors", async () => {
    const { api } = apiReturning({ a: head(5), b: head(2) });
    const result = await syncMultiHead({ a: 3, b: 2, gone: 9 }, api, async () => {});
    // a and b move to their polled heads; `gone` (absent from the map) is retained.
    expect(result.checkpoints).toEqual({ a: 5, b: 2, gone: 9 });
  });

  it("does not mutate the previous checkpoint object", async () => {
    const { api } = apiReturning({ a: head(5) });
    const prev = { a: 1 };
    await syncMultiHead(prev, api, async () => {});
    expect(prev).toEqual({ a: 1 });
  });
});
