/**
 * Types mirroring app/schemas.py (ProjectOut, EpicOut, TaskOut) and
 * app/models.py (TaskStatus, Priority). Keep these in sync by hand until
 * the OpenAPI document is used for codegen (see `/openapi.json`).
 */

// app.models.TaskStatus
export type TaskStatus =
  | "todo"
  | "in_progress"
  | "blocked"
  | "deferred"
  | "done"
  | "superseded"
  | "cancelled";

// app.models.Priority
export type Priority = "P0" | "P1" | "P2" | "P3";

export interface Project {
  public_id: string;
  slug: string;
  name: string;
  description: string | null;
  default_branch: string;
  created_at: string;
  updated_at: string;
}

export interface Epic {
  public_id: string;
  key: string;
  title: string;
  description: string | null;
  section: "backlog" | "to_do" | "in_progress" | "completed";
  position: number;
}

export interface CommitRef {
  sha: string;
  repo: string | null;
  test_summary: string | null;
  created_at: string;
}

export interface Note {
  author: string | null;
  body: string;
  created_at: string;
}

export interface Task {
  public_id: string;
  display_id: string;
  key: string | null;
  epic_key: string | null;
  title: string;
  description: string | null;
  status: TaskStatus;
  priority: Priority | null;
  component: string | null;
  proof_cmd: string | null;
  status_note: string | null;
  section: string;
  owner: string | null;
  lease_expires_at: string | null;
  position: number;
  /** Optimistic-lock token; send back as `If-Match: "v<version>"`. */
  version: number;
  tags: string[];
  commits: CommitRef[];
  notes: Note[];
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface Counter {
  namespace: string;
  current_value: number;
}

export interface TaskListParams {
  status?: TaskStatus;
  owner?: string;
  epic?: string;
  priority?: Priority;
  tag?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

// app.schemas.ProjectNoteOut - a note in the project-wide feed (LOG endpoints).
export interface ProjectNote {
  scope: "task" | "epic";
  task: string | null;
  epic: string | null;
  author: string | null;
  body: string;
  created_at: string;
}

export interface ProjectNoteListParams {
  scope?: "task" | "epic" | "all";
  author?: string;
  task?: string;
  epic?: string;
  since?: string;
  limit?: number;
  offset?: number;
}

// app.schemas.EventOut - the append-only event log.
export interface ProjectEvent {
  event_type: string;
  agent: string | null;
  task_id: number | null;
  message: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface EventListParams {
  event_type?: string;
  agent?: string;
  task?: string;
  limit?: number;
  offset?: number;
}

// app.schemas.DecisionOut
export interface Decision {
  public_id: string;
  key: string | null;
  title: string;
  decision: string;
  context: string | null;
  consequences: string | null;
  agent: string | null;
  created_at: string;
}

// app.schemas.STEP_STATUS_VALUES / ChainStepOut
export type ChainStepStatus = "pending" | "running" | "passed" | "failed" | "skipped";

export interface ChainStep {
  step_name: string;
  step_order: number;
  agent: string | null;
  status: ChainStepStatus;
  skip_justification: string | null;
  output_ref: string | null;
}

// app.schemas.RUN_STATUS_VALUES / ChainRunOut
export type ChainRunStatus = "running" | "passed" | "failed" | "aborted";

export interface ChainRun {
  public_id: string;
  status: ChainRunStatus;
  started_by: string | null;
  started_at: string;
  finished_at: string | null;
  steps: ChainStep[];
}
