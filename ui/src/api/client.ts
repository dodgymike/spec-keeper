import type {
  ChainRun,
  Counter,
  Decision,
  Epic,
  EventListParams,
  Project,
  ProjectEvent,
  ProjectNote,
  ProjectNoteListParams,
  Task,
  TaskListParams,
} from "./types";

import { getAccessToken, isCognitoConfigured, recoverFromUnauthorized } from "../auth/session";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8080";

/**
 * Auth seam (UI-7/AUTH-5): when Cognito is configured (`VITE_COGNITO_DOMAIN`
 * + `VITE_COGNITO_CLIENT_ID`), returns the live access token from
 * `auth/session.ts` (refreshing first if it's near expiry). Otherwise falls
 * back to the dev-only `VITE_DEV_TOKEN` env var - the local-dev ergonomics
 * this project has always relied on, unchanged when Cognito isn't set up.
 */
async function getToken(): Promise<string | undefined> {
  if (isCognitoConfigured()) {
    return getAccessToken();
  }
  return import.meta.env.VITE_DEV_TOKEN || undefined;
}

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

interface RequestOptions {
  method?: string;
  // `object` (not `Record<string, ...>`) so any params interface (e.g.
  // TaskListParams) is assignable without needing its own index signature.
  params?: object;
  body?: unknown;
  headers?: Record<string, string>;
}

function buildUrl(path: string, params?: object): string {
  const url = new URL(path.replace(/^\//, ""), API_BASE.replace(/\/?$/, "/"));
  if (params) {
    for (const [key, value] of Object.entries(params as Record<string, unknown>)) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/**
 * Core fetch wrapper: base URL, JSON handling, auth header, error shape.
 *
 * On a 401 with Cognito configured, tries one silent token refresh and
 * retries the request once; if the refresh also fails, `session.ts`
 * redirects to the Hosted UI sign-in (so callers here just see the
 * original 401 surfaced as an `ApiError`, never a blank screen or a retry
 * loop - `_retried` bounds this to a single attempt).
 */
async function request<T>(path: string, options: RequestOptions = {}, _retried = false): Promise<T> {
  const { method = "GET", params, body, headers = {} } = options;

  const finalHeaders: Record<string, string> = {
    Accept: "application/json",
    ...headers,
  };
  if (body !== undefined) {
    finalHeaders["Content-Type"] = "application/json";
  }
  const token = await getToken();
  if (token) {
    finalHeaders["Authorization"] = `Bearer ${token}`;
  }

  let response: Response;
  try {
    response = await fetch(buildUrl(path, params), {
      method,
      headers: finalHeaders,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (cause) {
    throw new ApiError(0, "Network error contacting the Spec Server API.", cause);
  }

  if (response.status === 401 && !_retried && isCognitoConfigured()) {
    const refreshedToken = await recoverFromUnauthorized();
    if (refreshedToken) {
      return request<T>(path, options, true);
    }
  }

  const text = await response.text();
  const data = text ? JSON.parse(text) : undefined;

  if (!response.ok) {
    const message =
      (data && typeof data === "object" && "message" in data && String((data as { message: unknown }).message)) ||
      `Request failed with status ${response.status}`;
    throw new ApiError(response.status, message, data);
  }

  return data as T;
}

export function listProjects(): Promise<Project[]> {
  return request<Project[]>("/api/v1/projects");
}

export function getProject(slug: string): Promise<Project> {
  return request<Project>(`/api/v1/projects/${encodeURIComponent(slug)}`);
}

export function listEpics(slug: string): Promise<Epic[]> {
  return request<Epic[]>(`/api/v1/projects/${encodeURIComponent(slug)}/epics`);
}

export function listTasks(slug: string, params?: TaskListParams): Promise<Task[]> {
  return request<Task[]>(`/api/v1/projects/${encodeURIComponent(slug)}/tasks`, {
    params,
  });
}

/** Current reservation-counter value per namespace (e.g. `dynamo-gsi -> 5`). */
export function listCounters(slug: string): Promise<Counter[]> {
  return request<Counter[]>(`/api/v1/projects/${encodeURIComponent(slug)}/counters`);
}

/** Project-wide notes feed (`app/blueprints/log.py`), newest first, tagged by scope. */
export function listProjectNotes(slug: string, params?: ProjectNoteListParams): Promise<ProjectNote[]> {
  return request<ProjectNote[]>(`/api/v1/projects/${encodeURIComponent(slug)}/notes`, { params });
}

/** Append-only event stream (`app/blueprints/log.py`), newest first. */
export function listEvents(slug: string, params?: EventListParams): Promise<ProjectEvent[]> {
  return request<ProjectEvent[]>(`/api/v1/projects/${encodeURIComponent(slug)}/events`, { params });
}

/** ADR-style decision records (`app/blueprints/log.py`), newest first. */
export function listDecisions(slug: string): Promise<Decision[]> {
  return request<Decision[]>(`/api/v1/projects/${encodeURIComponent(slug)}/decisions`);
}

/**
 * Chain runs for one task (`app/blueprints/chains.py`). NOTE: as of this
 * writing the backend only exposes `POST .../tasks/{ident}/chain-runs`
 * (start a run) - there is no `GET` list route, so this call currently
 * 404/405s. Kept so the activity feed picks it up automatically once the
 * backend gains the list endpoint; callers must treat failures as "no
 * chain-run data available" rather than a hard error.
 */
export function listChainRuns(slug: string, taskIdent: string): Promise<ChainRun[]> {
  return request<ChainRun[]>(
    `/api/v1/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(taskIdent)}/chain-runs`
  );
}

/** A single chain run (and its steps), by run public_id. */
export function getChainRun(slug: string, runId: string): Promise<ChainRun> {
  return request<ChainRun>(
    `/api/v1/projects/${encodeURIComponent(slug)}/chain-runs/${encodeURIComponent(runId)}`
  );
}
