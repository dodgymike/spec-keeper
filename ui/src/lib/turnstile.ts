/**
 * Cloudflare Turnstile script loader + minimal typings.
 *
 * The only external origin touched is https://challenges.cloudflare.com, which
 * the SPA's CSP already allows in `script-src` + `frame-src`. No inline script
 * is used: the API is loaded with `?render=explicit` and the widget is rendered
 * programmatically via `window.turnstile.render(...)`.
 */

/** Minimal typing of the Cloudflare Turnstile browser global. */
export interface TurnstileApi {
  render(
    container: HTMLElement | string,
    params: {
      sitekey: string;
      callback?: (token: string) => void;
      "expired-callback"?: () => void;
      "error-callback"?: () => void;
      theme?: "auto" | "light" | "dark";
    },
  ): string;
  reset(widgetId?: string): void;
  remove(widgetId?: string): void;
  getResponse(widgetId?: string): string | undefined;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

const SCRIPT_ID = "cf-turnstile-script";
const SCRIPT_SRC =
  "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

let loadPromise: Promise<TurnstileApi> | null = null;

/**
 * Inject the Turnstile script exactly once and resolve with the `window.turnstile`
 * API when it is ready. Memoised, so repeated calls (and React StrictMode's
 * double-invoked effects) never inject the script twice.
 */
export function loadTurnstile(): Promise<TurnstileApi> {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  if (loadPromise) return loadPromise;

  loadPromise = new Promise<TurnstileApi>((resolve, reject) => {
    const onReady = () => {
      if (window.turnstile) resolve(window.turnstile);
      else reject(new Error("Turnstile loaded but window.turnstile is undefined"));
    };
    const onError = () => reject(new Error("Turnstile script failed to load"));

    const existing = document.getElementById(SCRIPT_ID) as HTMLScriptElement | null;
    if (existing) {
      existing.addEventListener("load", onReady, { once: true });
      existing.addEventListener("error", onError, { once: true });
      return;
    }

    const script = document.createElement("script");
    script.id = SCRIPT_ID;
    script.src = SCRIPT_SRC;
    script.async = true;
    script.addEventListener("load", onReady, { once: true });
    script.addEventListener("error", onError, { once: true });
    document.head.appendChild(script);
  });
  return loadPromise;
}
