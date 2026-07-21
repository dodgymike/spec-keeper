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
| `VITE_DEV_TOKEN` | unset | Optional dev-only bearer token, sent as `Authorization: Bearer <token>` until Cognito login is wired up. Only needed if the API is deployed with `API_KEYS` configured. |

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
  (currently a seam that reads `VITE_DEV_TOKEN`; will read a real Cognito
  JWT once auth is wired up).
- Throws a typed `ApiError` (with `status` and parsed `body`) on non-2xx
  responses, so callers can render a graceful error state instead of a blank
  screen.

Types in `src/api/types.ts` mirror `app/schemas.py`'s `ProjectOut`,
`EpicOut`, and `TaskOut` (and `app/models.py`'s `TaskStatus`/`Priority`
enums) - update both sides together if the API shape changes.
