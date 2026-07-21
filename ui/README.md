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
| `VITE_COGNITO_REGION` | `eu-west-1` | AWS region hosting the Cognito user pool (the app calls the regional Cognito-IDP JSON API directly). |
| `VITE_COGNITO_USER_POOL_ID` | unset | Cognito user pool ID, e.g. `eu-west-1_AbCdEfGhI`. |
| `VITE_COGNITO_CLIENT_ID` | unset | Public **native** app client ID for the human `ui` client (no secret, no PKCE/OAuth involved). |

Cognito is considered "configured" when `VITE_COGNITO_REGION`,
`VITE_COGNITO_CLIENT_ID`, and `VITE_COGNITO_USER_POOL_ID` are all set;
otherwise the login flow is disabled entirely (local-dev fallback below).

### Human sign-in (UI-7 / AUTH-5 / HA-4)

When Cognito is configured (see above), the app requires sign-in via
**native Cognito WebAuthn passkeys** - it talks directly to the regional
Cognito-IDP JSON API over `fetch`; there is no Amplify, no Hosted UI, and no
OAuth/PKCE redirect. The auth code lives in `src/auth/`:

- `src/auth/cognito.ts` - the Cognito-IDP JSON API client and WebAuthn
  credential marshalling (`InitiateAuth`/`RespondToAuthChallenge` with the
  `USER_AUTH` and `WEB_AUTHN` challenge flows, `navigator.credentials`
  encoding/decoding). No OIDC library, no CDN script - CSP-clean by
  construction.
- `src/auth/session.ts` - a framework-agnostic singleton holding the
  sign-in/sign-up/recovery flows and the access/id/refresh tokens **in
  memory only** - never written to local/session storage. A page reload
  requires re-authentication.
- `src/auth/config.ts` / `src/auth/errors.ts` - env-driven config
  (region/user pool/client id) and typed Cognito error mapping.
- `src/auth/AuthContext.tsx` - a small React context wrapping that singleton
  for the header user chip and the auth pages.
- `src/pages/LoginPage.tsx` - native passkey sign-in (`USER_AUTH` +
  `WEB_AUTHN`) with an "email me a code instead" recovery path
  (`CUSTOM_AUTH` email one-time-code).
- `src/pages/JoinPage.tsx` - invite-only onboarding at `/join?code=...`:
  `SignUp` with the invite code passed via `clientMetadata`, followed by
  email-OTP verification and passkey enrolment.
- `src/pages/SettingsPage.tsx` - lists the signed-in user's registered
  passkeys and lets them add or remove one.

There is no `/callback` route - sign-in never leaves the page (no
redirect-based OAuth flow to complete).

**WebAuthn RP ID:** the relying-party ID is the site host
(`window.location.hostname`) - i.e. the CloudFront host in production.

**Local-dev fallback:** leaving Cognito unconfigured disables this entirely -
no login screen, no redirect - and `client.ts`'s `getToken()` falls back to
`VITE_DEV_TOKEN` (or no auth), so the dashboard runs against a local open
server exactly as before.

**CSP note (for INFRA-5's CloudFront config):** because the app calls the
Cognito-IDP JSON API directly via `fetch`, `connect-src` must include
`https://cognito-idp.<VITE_COGNITO_REGION>.amazonaws.com` (e.g.
`https://cognito-idp.eu-west-1.amazonaws.com`). There is no separate Hosted-UI
domain and no top-level navigation to Cognito.

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
- On a `401` with Cognito configured, attempts one silent token refresh and
  retries once; if that also fails, `session.ts` clears the session (no
  redirect) and `App.tsx` renders `LoginPage` in its place.
- Throws a typed `ApiError` (with `status` and parsed `body`) on non-2xx
  responses, so callers can render a graceful error state instead of a blank
  screen.

Types in `src/api/types.ts` mirror `app/schemas.py`'s `ProjectOut`,
`EpicOut`, and `TaskOut` (and `app/models.py`'s `TaskStatus`/`Priority`
enums) - update both sides together if the API shape changes.
