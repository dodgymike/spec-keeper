/**
 * Unit tests for the remembered-login-email store.
 *
 * Focus: the get/set/clear round-trip, blank-value handling, and the
 * missing-storage guard (private mode must never throw). Mirrors the
 * `deltaCache.test.ts` checkpoint suite: a tiny in-memory `window.localStorage`
 * fake so the suite runs under vitest's default (node) environment without
 * jsdom — this module only touches `window.localStorage`.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  clearRememberedEmail,
  getRememberedEmail,
  setRememberedEmail,
} from "./rememberedEmail";

const KEY = "spec.login.email";

describe("remembered email store", () => {
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

  it("round-trips a remembered email", () => {
    setRememberedEmail("dev@example.com");
    expect(getRememberedEmail()).toBe("dev@example.com");
    expect(store[KEY]).toBe("dev@example.com");
  });

  it("returns null when nothing is remembered", () => {
    expect(getRememberedEmail()).toBeNull();
  });

  it("trims surrounding whitespace on set and get", () => {
    setRememberedEmail("  dev@example.com  ");
    expect(store[KEY]).toBe("dev@example.com");
    expect(getRememberedEmail()).toBe("dev@example.com");
  });

  it("treats a blank set as a clear (stores nothing)", () => {
    setRememberedEmail("dev@example.com");
    setRememberedEmail("   ");
    expect(getRememberedEmail()).toBeNull();
    expect(KEY in store).toBe(false);
  });

  it("treats a blank stored value as absent", () => {
    store[KEY] = "   ";
    expect(getRememberedEmail()).toBeNull();
  });

  it("clearRememberedEmail removes the stored email", () => {
    setRememberedEmail("dev@example.com");
    clearRememberedEmail();
    expect(getRememberedEmail()).toBeNull();
    expect(KEY in store).toBe(false);
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
    (globalThis as unknown as { window: { localStorage: typeof throwing } }).window = {
      localStorage: throwing,
    };
    expect(() => setRememberedEmail("dev@example.com")).not.toThrow();
    expect(getRememberedEmail()).toBeNull();
    expect(() => clearRememberedEmail()).not.toThrow();
  });
});
