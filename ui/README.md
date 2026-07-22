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
| `VITE_ADMIN_GROUP` | `spec-admins` | Cognito group that gates the admin console (nav link + `/admin` route). Override to match a pool that renamed `AUTH_GROUP_ADMIN`; the server re-checks the group on every admin call regardless of this setting. |
| `VITE_AGENT_EMAIL_DOMAIN` | `agents.spec-server.internal` | DNS-style domain used to synthesize agent usernames/emails (`<slug>@<domain>`), matching Terraform's `agent_username_domain`. Used by the admin console's Agents tab to tell agent users apart from humans. |
| `VITE_TURNSTILE_SITE_KEY` | unset | Cloudflare Turnstile site key. When set, a widget placeholder renders on the public `/request` access-request form. Loading the Turnstile script itself is a deploy follow-up (see below). |

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

### Admin console (UI-9 / HA-5-UI)

`/admin` is a signed-in-only route, gated client-side on the `VITE_ADMIN_GROUP`
(default `spec-admins`) claim read from the decoded ID token's
`cognito:groups` (`src/auth/session.ts`'s `isAdminUser`/`adminGroup`) - the
nav link (`src/components/AppLayout.tsx`) and the page itself
(`src/pages/AdminPage.tsx`) both hide/refuse when the signed-in user isn't a
member. This is a UX convenience only: the server re-checks the group on
every `/api/v1/admin/*` call regardless of what the client shows.

Four tabs:

- **Users** - list/filter (pending-only) human users; per-row approve /
  block / unblock / promote / demote / delete, calling `/api/v1/admin/*`.
  Surfaces the backend's 409 self/last-admin guardrail messages (e.g.
  refusing self-demote or demoting the last remaining admin) inline.
- **Agents** - agent Cognito users, identified by the synthetic
  `<slug>@<VITE_AGENT_EMAIL_DOMAIN>` email, with client-side stats: active
  (owner-held tasks), completed (done tasks the agent noted), token usage
  (summed from model-usage notes), and last-active. Block/unblock/delete
  reuse the same `/api/v1/admin/users/*` endpoints as the Users tab.
- **Enrollments** (ONBOARD-4) - mint a single-use agent-enrollment token
  (project, agent name, role reader/writer/admin, optional TTL); the
  one-time enrollment URL is shown once for the admin to copy. The list of
  active enrollments shows only the `token_hash`, never the plaintext
  token, with a per-row Revoke. Also lists/removes the selected project's
  members.
- **Invites** - mint a single-use invite (email optional, TTL, pre-approved
  toggle); the one-time plaintext code + join URL are shown once for the
  admin to copy. The list of active invites shows only the code hash,
  status, and expiry - the plaintext code is never listed or retrievable
  again.

### Public access request (HA-7)

`/request` is a public, **unauthenticated** route (`src/pages/RequestAccessPage.tsx`)
rendered outside the signed-in app shell, alongside `/join`. It's a simple
form - email, optional display name, plus a hidden honeypot field and an
optional Cloudflare Turnstile widget (rendered only when
`VITE_TURNSTILE_SITE_KEY` is set) - that POSTs to `/api/v1/signup`. The page
always shows the same neutral "if eligible, you'll get an email"
confirmation regardless of the outcome (the backend answers a uniform 202 to
avoid revealing whether an address is eligible/already known); a genuine
transport error is the only case that surfaces a retry affordance.

**Deploy follow-up:** loading the actual Turnstile challenge script requires
widening the SPA's CSP (`script-src`/`frame-src` to allow
`https://challenges.cloudflare.com`) before the widget can render for real;
until then, setting `VITE_TURNSTILE_SITE_KEY` only renders the placeholder.

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
`listEpics(slug)`, `listTasks(slug, params)`, and (UI-9) the admin/invite/signup
functions consumed by `/admin` and `/request`: `listAdminUsers(params)`,
`approveUser(username, body?)`, `blockUser(username)`, `unblockUser(username)`,
`promoteUser(username)`, `demoteUser(username)`, `deleteUser(username)`,
`listInvites()`, `mintInvite(body)`, and `requestAccess(body)` (unauthenticated).
(ONBOARD-4) `listEnrollments(projectSlug?)`, `mintEnrollment(body)`,
`revokeEnrollment(tokenHash)`, `listMembers(slug)`, and
`removeMember(slug, principalSub)`.
All go through a shared `request()` helper (`src/api/client.ts`) that:

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
