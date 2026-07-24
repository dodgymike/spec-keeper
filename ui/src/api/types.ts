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
  // Stable, cross-backend pointer to the related task (its public_id), or null.
  task_pubid: string | null;
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

// ---- Admin console (HA-5-UI / UI-9) ------------------------------------
// Mirrors app/schemas.py: AdminUserOut, InviteOut, InviteMintOut, InviteIn,
// AdminApproveIn. The admin endpoints live under /api/v1/admin and are gated
// server-side on the spec-admins group; the UI mirrors that gate for nav.

/** Derived lifecycle status: `pending` = in no spec-* group; `active` = in one. */
export type AdminUserStatus = "pending" | "active";

// app.schemas.AdminUserOut — a listed pool user (human OR agent). No secrets.
export interface AdminUser {
  username: string;
  email: string | null;
  enabled: boolean;
  status: AdminUserStatus;
  groups: string[];
  created_at: string | null;
}

export interface AdminUsersQuery {
  status?: AdminUserStatus;
}

/** Group granted on approval (admin promotion is a separate /promote action). */
export type ApproveGroup = "spec-readers" | "spec-writers";

// app.schemas.AdminApproveIn
export interface AdminApproveIn {
  group?: ApproveGroup;
}

// app.schemas.InviteIn — mint a single-use invite.
export interface InviteIn {
  email?: string | null;
  ttl_days?: number | null;
  approved?: boolean;
}

// app.schemas.InviteOut — a listed invite (hashes/status/expiry ONLY).
export interface Invite {
  code_hash: string;
  status: string;
  created_at: number | null;
  expires_at: number | null;
  email_bound: boolean;
  approved: boolean;
}

// app.schemas.InviteMintOut — the ONLY place a plaintext code is emitted.
export interface InviteMint {
  code: string;
  join_url: string;
  code_hash: string;
  expires_at: number;
  email_bound: boolean;
  approved: boolean;
}

// ---- Agent enrollment + membership (ONBOARD-4) -------------------------
// Mirrors app/schemas.py: EnrollmentIn, EnrollmentMintOut, EnrollmentOut,
// MemberIn, MemberOut. All under /api/v1/admin (enrollments) and
// /api/v1/projects/<slug>/members; gated server-side on the spec-admins group.

/** Project-membership role granted on enrolment / assigned to a member. */
export type MemberRole = "reader" | "writer" | "admin";

// app.schemas.EnrollmentIn — mint a single-use agent-enrollment token.
export interface EnrollmentIn {
  project_slug: string;
  agent_name: string;
  role: MemberRole;
  /** Override the token validity window in seconds (default server ENROLL_TTL_SECONDS). */
  ttl_seconds?: number | null;
  /**
   * Name for the project when `project_slug` names a NOT-YET-EXISTING project:
   * the mint endpoint creates the project on the fly (ONBOARD-7). Ignored when
   * the project already exists; the server is authoritative.
   */
  project_name?: string | null;
}

// app.schemas.EnrollmentOut — a listed enrollment. `token_hash` is the SHA-256
// revocation handle (the DELETE key), NOT the plaintext token, which is never listed.
export interface Enrollment {
  token_hash: string;
  project_slug: string;
  agent_name: string;
  role: string;
  created_by: string | null;
  created_at: number | null;
  expires_at: number | null;
  status: string;
}

// app.schemas.EnrollmentMintOut — the ONLY place the plaintext token is emitted.
export interface EnrollmentMint {
  enrollment_url: string;
  token: string;
  project_slug: string;
  role: string;
  agent_name: string;
  expires_at: number;
  /**
   * True when the mint CREATED `project_slug` (it did not exist before) rather
   * than enrolling onto an existing project (ONBOARD-7).
   */
  project_created: boolean;
}

// app.schemas.MemberOut — a listed project member.
export interface Member {
  project_slug: string;
  principal_sub: string;
  principal_name: string | null;
  role: string;
  created_at: string;
}

// ---- Public agent-enrollment REDEEM (ONBOARD-3 / ONBOARD-5) ------------
// Mirrors app/schemas.py: EnrollRedeemIn / EnrollRedeemOut. This is the ONE
// PUBLIC (no-bearer) enrollment call: a brand-new agent posts the single-use
// token it was handed and the server burns it + provisions a real Cognito
// credential, returning working creds + a setup recipe EXACTLY ONCE.

// app.schemas.EnrollRedeemIn — the single-use token to burn.
export interface EnrollRedeemIn {
  token: string;
}

// app.schemas.EnrollRedeemOut — the redeem response. `password` is emitted
// ONCE and never stored/logged; a lost password means minting a fresh token.
// `recipe` is a small ordered guide keyed `1_mint_token` / `2_first_call` /
// `3_migrate_local_backlog`, each a copy-paste string.
export interface EnrollRedeemOut {
  username: string;
  password: string;
  api_base: string;
  region: string | null;
  client_id: string | null;
  project_slug: string;
  role: string;
  recipe: Record<string, string>;
}

// ---- Delta change feed (UI-DELTA-*) -----------------------------------
// Mirrors the pinned server contract for the incremental change feed:
//   GET /api/v1/projects/<slug>/changes/head
//   GET /api/v1/projects/<slug>/changes?since=<int>&limit=<int>
// The feed lets the dashboard sync incrementally (a per-project cursor) instead
// of re-fetching whole lists. This module only defines the wire types + the
// client calls + the pure cache; the useLiveRefresh rewire (UI-DELTA-8) and
// full-resync orchestration (UI-DELTA-9) consume them later.

/** The entity kinds a change entry can describe. */
export type ChangeEntityType = "task" | "epic" | "note" | "commit" | "relation";

/** upsert = create-or-replace the snapshot; delete = evict the entity. */
export type ChangeOp = "upsert" | "delete";

/**
 * One entry in the change feed. Entries are strictly ascending by `seq`.
 * `snapshot` is the full server-side representation of the entity for an
 * `upsert` (e.g. a `Task` for `entity_type: "task"`); it may be `null` on a
 * `delete`. It is typed `unknown` here because it is polymorphic over
 * `entity_type`; cache selectors narrow it when reading a specific bucket.
 */
export interface ChangeEntry {
  seq: number;
  entity_type: ChangeEntityType;
  entity_pubid: string;
  op: ChangeOp;
  version: number;
  occurred_at: string;
  snapshot: unknown;
}

/** `GET .../changes/head` — the current tip + the oldest still-retained seq. */
export interface ChangesHead {
  /** Highest `seq` currently available (the tip cursor). */
  cursor: number;
  /**
   * Oldest `seq` the server still retains. A local checkpoint older than this
   * cannot be caught up incrementally → a full resync is required (UI-DELTA-9).
   */
  min_retained_seq: number;
}

/**
 * `GET /projects/heads` — the batched fan-out head map (UI-DELTA-10). One request
 * carries the change-log head for each of the caller's visible projects, keyed by
 * slug, so a multi-project dashboard decides which projects advanced without an
 * N-request per-project `/changes/head` fan-out. Isolation-scoped server-side.
 */
export interface ProjectHeads {
  heads: Record<string, ChangesHead>;
}

/** `GET .../changes?since=&limit=` — one ascending page of changes. */
export interface ChangesPage {
  /** Highest `seq` in this page (advance the checkpoint to this after applying). */
  cursor: number;
  /** Entries in ascending `seq` order. */
  changes: ChangeEntry[];
  /** True when more pages remain past this one (page hit `limit`). */
  truncated: boolean;
  /**
   * True when `since` is older than `min_retained_seq`, so the caller must drop
   * its cache and re-fetch from scratch rather than apply this page.
   */
  full_resync_required: boolean;
  /** Oldest `seq` the server still retains (mirrors the head value). */
  min_retained_seq: number;
}

// HA-7 public access-request intake. The body is deliberately minimal; the
// server always answers with a uniform 202 (never reveals whether the address
// is known/eligible).
export interface SignupRequestIn {
  email: string;
  display_name?: string;
  turnstile_token?: string;
  /** Honeypot — must stay empty; a non-empty value is silently dropped as a bot. */
  hp_website?: string;
}
