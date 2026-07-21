import type { Counter, Epic, Project, Task, TaskListParams } from "./types";

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
