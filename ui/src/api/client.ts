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

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8080";

/**
 * Auth seam: once Cognito login is wired up, this should return the current
 * user's JWT (e.g. from an auth context / token cache). For now it only
 * reads an optional dev-only token from the environment, so local dev can
 * exercise `API_KEYS`-protected deployments without a real login flow.
 */
function getToken(): string | undefined {
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

/** Core fetch wrapper: base URL, JSON handling, auth header, error shape. */
async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", params, body, headers = {} } = options;

  const finalHeaders: Record<string, string> = {
    Accept: "application/json",
    ...headers,
  };
  if (body !== undefined) {
    finalHeaders["Content-Type"] = "application/json";
  }
  const token = getToken();
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
