# Spec Server Dashboard (UI)

A React + Vite + TypeScript single-page app that visualises the state of all
Spec Server projects (projects, epics, tasks, leases). This is the scaffold
built in **UI-1**: an app shell, a design-token theme (light + dark), a typed
API client, and one working screen (Projects overview). Later tasks (UI-2..8)
add the remaining views (project detail, epic/task boards, lease/claim
status, charts, etc.) as nested routes under `src/App.tsx`.

## Requirements

- Node.js 20+ and npm.

## Running locally

```bash
cd ui
npm install
npm run dev
```

This starts the Vite dev server (default `http://localhost:5173`).

## Configuration

Copy `.env.example` to `.env.local` and adjust as needed:

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_API_BASE` | `http://localhost:8080` | Base URL of the running Spec Server API. |
| `VITE_DEV_TOKEN` | unset | Optional dev-only bearer token, sent as `Authorization: Bearer <token>`. Only used when Cognito (below) is not configured; only needed if the API is deployed with `API_KEYS` configured. |
| `VITE_COGNITO_DOMAIN` | unset | Cognito Hosted-UI domain host (no scheme), e.g. `spec-server-auth-ab12cd.auth.us-east-1.amazoncognito.com`. Leave unset to disable the login flow entirely (local-dev fallback below). |
| `VITE_COGNITO_CLIENT_ID` | unset | Public SPA app client ID (Authorization Code + PKCE, no secret). |
| `VITE_COGNITO_REDIRECT_URI` | `http://localhost:5173/callback` | OAuth `redirect_uri`; must exactly match a callback URL allowed on the Cognito app client. |
| `VITE_COGNITO_LOGOUT_URI` | `http://localhost:5173/` | Cognito `logout_uri`; must exactly match a logout URL allowed on the Cognito app client. |
| `VITE_COGNITO_SCOPES` | `openid email profile` | Space-separated OAuth scopes requested at sign-in. |

### Human sign-in (UI-7 / AUTH-5)

When `VITE_COGNITO_DOMAIN` and `VITE_COGNITO_CLIENT_ID` are both set, the app
requires sign-in via the Cognito Hosted UI (Authorization Code + PKCE,
`src/auth/`):

- `src/auth/pkce.ts` - hand-rolled PKCE (`crypto.subtle` SHA-256 for the
  `S256` code_challenge, `crypto.getRandomValues` for the verifier/state).
  No OIDC library, no CDN script - CSP-clean by construction.
- `src/auth/session.ts` - a framework-agnostic singleton that builds the
  authorize/logout URLs, exchanges the code for tokens at
  `https://<domain>/oauth2/token`, and holds the access/id/refresh tokens
  **in memory only** (never written to local/session storage - the PKCE
  `code_verifier` and OAuth `state` are the only things briefly stashed in
  `sessionStorage`, cleared as soon as the callback completes). Refresh
  happens silently ahead of expiry, and again on any API 401
  (`recoverFromUnauthorized()`); if refresh fails, it redirects to sign-in.
- `src/auth/AuthContext.tsx` - a small React context wrapping that singleton
  for the header user chip and the sign-in screen.
- `src/pages/SignInPage.tsx` / `src/pages/CallbackPage.tsx` - the minimal
  sign-in screen and the `/callback` redirect handler (spinner, focus moved
  to the view on mount, `aria-live` status, then a router redirect back to
  wherever the user was).

**Local-dev fallback:** leaving `VITE_COGNITO_DOMAIN`/`VITE_COGNITO_CLIENT_ID`
unset disables this entirely - no login screen, no redirect - and
`client.ts`'s `getToken()` falls back to `VITE_DEV_TOKEN` (or no auth), so the
dashboard runs against a local open server exactly as before.

**CSP note (for INFRA-5's CloudFront config):** the SPA's only additional
network origin beyond the API is the Cognito Hosted-UI domain
(`VITE_COGNITO_DOMAIN`) - `connect-src` needs
`https://<VITE_COGNITO_DOMAIN>` (the app calls `/oauth2/token` there), and
top-level navigation (not fetch) goes to `https://<VITE_COGNITO_DOMAIN>/oauth2/authorize`
and `/logout`, which CSP `connect-src`/`frame-src` don't need to allow but
`form-action`/navigation policy should permit if enforced.

## Building

```bash
npm run build      # tsc --noEmit, then vite build -> dist/
npm run preview    # serve the production build locally
npm run typecheck  # tsc --noEmit only
```

## Backend dependency: CORS

The dev server runs on a different origin (`http://localhost:5173`) than the
API (`http://localhost:8080`), so browser requests from this UI to the API
are cross-origin. **The Spec Server backend does not currently send CORS
headers** (see `app/__init__.py` / `app/blueprints/`) - this UI's fetch calls
will fail with a CORS error against a real API until that is added.

This is **not** addressed in UI-1 (out of scope: "do not change the backend
here"). It is a dependency for AUTH/INFRA work and should allow, at minimum:

- Origin: the Vite dev origin (`http://localhost:5173`) and whatever origin
  the built dashboard is eventually served from.
- Headers: `Authorization`, `Content-Type`, `If-Match` (used by task PATCH
  endpoints for optimistic locking).
- Methods: `GET, POST, PATCH, DELETE`.

## Design system

- `src/styles/tokens.css` - CSS custom properties (colour, space, type) with
  light and dark themes. Dark mode activates via `:root[data-theme="dark"]`
  or automatically via `prefers-color-scheme: dark` when no explicit theme
  is set.
- `src/styles/base.css` - a minimal reset built on the tokens.
- `src/components/` - `Card`, `Badge` (status pill), `StatChip`, and
  `AppLayout` (header + nav shell). All styling is plain CSS files imported
  per-component; there are **no inline styles with dynamic values**, to stay
  compatible with a strict CSP (`script-src 'self'; style-src 'self'`).

## API client

`src/api/client.ts` exports `listProjects()`, `getProject(slug)`,
`listEpics(slug)`, and `listTasks(slug, params)`, all going through a shared
`request()` helper (`src/api/client.ts`) that:

- Resolves URLs against `import.meta.env.VITE_API_BASE`.
- Attaches `Authorization: Bearer <token>` when `getToken()` returns a value
  - the live Cognito access token (`src/auth/session.ts`) when configured,
  falling back to `VITE_DEV_TOKEN` otherwise (see "Human sign-in" above).
- On a `401`, attempts one silent token refresh and retries once; if that
  also fails, redirects to sign-in instead of leaving a blank screen.
- Throws a typed `ApiError` (with `status` and parsed `body`) on non-2xx
  responses, so callers can render a graceful error state instead of a blank
  screen.

Types in `src/api/types.ts` mirror `app/schemas.py`'s `ProjectOut`,
`EpicOut`, and `TaskOut` (and `app/models.py`'s `TaskStatus`/`Priority`
enums) - update both sides together if the API shape changes.
