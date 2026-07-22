import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  ApiError,
  approveUser,
  blockUser,
  deleteUser,
  demoteUser,
  listAdminUsers,
  listInvites,
  listProjectNotes,
  listProjects,
  listTasks,
  mintInvite,
  promoteUser,
  unblockUser,
} from "../api/client";
import type { AdminUser, Invite, InviteMint, ProjectNote, Task } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { adminGroup, isAdminUser } from "../auth/session";
import { Badge } from "../components/Badge";
import { Card } from "../components/Card";
import { StatChip } from "../components/StatChip";
import { formatRelativeTime, useLiveRefresh } from "../hooks/useLiveRefresh";
import "./AdminPage.css";

/** Same fetch cap the other pages use - the API paginates by default. */
const FETCH_LIMIT = 1000;
/** Admin data changes rarely; a slower background refresh than the boards. */
const AUTO_REFRESH_MS = 60_000;

type Tab = "users" | "agents" | "invites";

/**
 * DNS-style domain the pool uses to synthesise agent usernames/emails
 * (`<slug>@<domain>`), matching Terraform's `agent_username_domain`. A pool
 * that changed it sets `VITE_AGENT_EMAIL_DOMAIN`. Used to tell agent users
 * (AI coding agents - Cognito users too) apart from humans.
 */
function agentEmailDomain(): string {
  const value = import.meta.env.VITE_AGENT_EMAIL_DOMAIN;
  return value && value.trim() !== "" ? value.trim().toLowerCase() : "agents.spec-server.internal";
}

/** True when a pool user is an AI agent (synthetic `<slug>@<agent-domain>` identity). */
function isAgent(user: AdminUser): boolean {
  const suffix = `@${agentEmailDomain()}`;
  const email = (user.email ?? "").toLowerCase();
  return email.endsWith(suffix) || user.username.toLowerCase().endsWith(suffix);
}

/** An agent's task/note attribution slug - the local part of its synthetic email. */
function agentSlug(user: AdminUser): string {
  const source = user.email || user.username;
  const at = source.indexOf("@");
  return (at >= 0 ? source.slice(0, at) : source).toLowerCase();
}

/**
 * Best-effort token-usage parse for a model-usage note. Agents post `kind=model`
 * notes whose body carries a token count; the project notes API doesn't (yet)
 * surface the structured `kind`, so we tolerate both a JSON body
 * (`{tokens|total_tokens|input_tokens+output_tokens}`) and a plain "N tokens"
 * text form. A note with no recognisable count contributes 0.
 */
function parseModelTokens(note: ProjectNote): number {
  const body = note.body ?? "";
  try {
    const parsed = JSON.parse(body) as Record<string, unknown>;
    if (parsed && typeof parsed === "object") {
      const total = parsed.total_tokens ?? parsed.tokens;
      if (typeof total === "number" && Number.isFinite(total)) return total;
      const input = typeof parsed.input_tokens === "number" ? parsed.input_tokens : 0;
      const output = typeof parsed.output_tokens === "number" ? parsed.output_tokens : 0;
      if (input || output) return input + output;
    }
  } catch {
    // not JSON - fall through to the text form
  }
  const match = body.match(/([\d,]+)\s*tokens/i);
  return match ? Number(match[1].replace(/,/g, "")) : 0;
}

interface AgentStats {
  /** Tasks the agent currently holds a lease on (owner === slug). */
  active: number;
  /** Distinct DONE tasks the agent left a note on (see computeAgentStats). */
  completed: number;
  /** Sum of parsed model-usage tokens across the agent's notes. */
  tokens: number;
  /** Latest note timestamp for the agent (epoch ms), or null. */
  lastActiveMs: number | null;
}

/**
 * Client-side agent stats aggregated across every project. Because task
 * completion clears `owner` and the completion event records no agent, we
 * attribute:
 *   - `active`    from tasks the agent still owns (in-flight claims), and
 *   - `completed` from DONE tasks the agent left a note on - the honest
 *     client-side signal, mirroring the project notes API the task specifies.
 */
function computeAgentStats(tasks: Task[], notes: ProjectNote[]): Map<string, AgentStats> {
  const stats = new Map<string, AgentStats>();
  const ensure = (slug: string): AgentStats => {
    let s = stats.get(slug);
    if (!s) {
      s = { active: 0, completed: 0, tokens: 0, lastActiveMs: null };
      stats.set(slug, s);
    }
    return s;
  };

  // Match the server's ProjectNote.task, which is `key or public_id` (see
  // storage list_project_notes) - NOT display_id - so keyless done tasks still
  // correlate with their notes.
  const doneTaskKeys = new Set<string>();
  for (const task of tasks) {
    if (task.status === "done") doneTaskKeys.add(task.key ?? task.public_id);
    if (task.owner) ensure(task.owner.toLowerCase()).active += 1;
  }

  const seen = new Set<string>();
  for (const note of notes) {
    const author = note.author?.toLowerCase();
    if (!author) continue;
    const s = ensure(author);
    s.tokens += parseModelTokens(note);
    const ms = new Date(note.created_at).getTime();
    if (!Number.isNaN(ms) && (s.lastActiveMs === null || ms > s.lastActiveMs)) {
      s.lastActiveMs = ms;
    }
    if (note.task && doneTaskKeys.has(note.task)) {
      const pair = `${author} ${note.task}`;
      if (!seen.has(pair)) {
        seen.add(pair);
        s.completed += 1;
      }
    }
  }
  return stats;
}

type UsersState =
  | { status: "loading" }
  | { status: "error"; error: ApiError | Error }
  | { status: "ready"; users: AdminUser[] };

type StatsState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; error: ApiError | Error }
  | { status: "ready"; stats: Map<string, AgentStats> };

type InvitesState =
  | { status: "loading" }
  | { status: "error"; error: ApiError | Error }
  | { status: "ready"; invites: Invite[] };

const TABS: ReadonlyArray<{ id: Tab; label: string }> = [
  { id: "users", label: "Users" },
  { id: "agents", label: "Agents" },
  { id: "invites", label: "Invites" },
];

/**
 * Admin console (HA-5-UI + UI-9): user lifecycle, agent management + stats, and
 * invite minting. Gated on the signed-in user's `spec-admins` group - the nav
 * link is only shown to admins and this page refuses to render for non-admins,
 * but the server re-checks the group on every call (the real boundary).
 */
export function AdminPage() {
  const { user } = useAuth();
  const [tab, setTab] = useState<Tab>("users");
  const { reload, refresh, lastUpdated, markUpdated, now } = useLiveRefresh(AUTO_REFRESH_MS);

  // Roving-tabindex keyboard model for the tablist (WAI-ARIA APG): the active
  // tab is the single tab stop; Arrow/Home/End move focus + selection.
  const tabRefs = useRef<Partial<Record<Tab, HTMLButtonElement | null>>>({});
  function onTabKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const order = TABS.map((t) => t.id);
    const index = order.indexOf(tab);
    let next: Tab | null = null;
    if (event.key === "ArrowRight") next = order[(index + 1) % order.length];
    else if (event.key === "ArrowLeft") next = order[(index - 1 + order.length) % order.length];
    else if (event.key === "Home") next = order[0];
    else if (event.key === "End") next = order[order.length - 1];
    if (next) {
      event.preventDefault();
      setTab(next);
      tabRefs.current[next]?.focus();
    }
  }

  const [usersState, setUsersState] = useState<UsersState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setUsersState({ status: "loading" });
    listAdminUsers()
      .then((users) => {
        if (cancelled) return;
        setUsersState({ status: "ready", users });
        markUpdated();
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setUsersState({
            status: "error",
            error: error instanceof Error ? error : new Error(String(error)),
          });
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reload]);

  if (!isAdminUser(user)) {
    return (
      <section className="admin-page">
        <h1 className="admin-page__title">Admin</h1>
        <Card className="admin-page__error">
          <p>Requires the {adminGroup()} group.</p>
        </Card>
      </section>
    );
  }

  return (
    <section className="admin-page">
      <header className="admin-page__header">
        <div>
          <h1 className="admin-page__title">Admin</h1>
          <p className="admin-page__subtitle">User lifecycle, agents, and invites.</p>
        </div>
        <div className="admin-page__header-controls">
          {lastUpdated !== null && (
            <span className="admin-page__updated">Updated {formatRelativeTime(now - lastUpdated)}</span>
          )}
          <button type="button" className="admin-page__refresh-button" onClick={refresh}>
            Refresh
          </button>
        </div>
      </header>

      <div className="admin-page__tabs" role="tablist" aria-label="Admin sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`admin-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`admin-panel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            ref={(el) => {
              tabRefs.current[t.id] = el;
            }}
            className={tab === t.id ? "admin-page__tab admin-page__tab--active" : "admin-page__tab"}
            onClick={() => setTab(t.id)}
            onKeyDown={onTabKeyDown}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "users" && (
        <div role="tabpanel" id="admin-panel-users" aria-labelledby="admin-tab-users">
          <UsersPanel state={usersState} onChanged={refresh} />
        </div>
      )}
      {tab === "agents" && (
        <div role="tabpanel" id="admin-panel-agents" aria-labelledby="admin-tab-agents">
          <AgentsPanel state={usersState} reload={reload} now={now} onChanged={refresh} />
        </div>
      )}
      {tab === "invites" && (
        <div role="tabpanel" id="admin-panel-invites" aria-labelledby="admin-tab-invites">
          <InvitesPanel reload={reload} onChanged={refresh} />
        </div>
      )}
    </section>
  );
}

// -------------------------------------------------------------------------
// Shared: a row-action runner that surfaces the server's message (incl. the
// 409 self/last-admin guardrails) into an aria-live region.
// -------------------------------------------------------------------------

function useRowAction(onChanged: () => void) {
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; kind: "ok" | "error" } | null>(null);

  const run = useCallback(
    async (id: string, label: string, action: () => Promise<void>, confirmText?: string) => {
      if (confirmText && !window.confirm(confirmText)) return;
      setBusy(id);
      setMessage(null);
      try {
        await action();
        setMessage({ text: `${label}.`, kind: "ok" });
        onChanged();
      } catch (error) {
        const text =
          error instanceof ApiError
            ? error.message
            : error instanceof Error
              ? error.message
              : String(error);
        setMessage({ text, kind: "error" });
      } finally {
        setBusy(null);
      }
    },
    [onChanged]
  );

  return { busy, message, run };
}

function ActionMessage({ message }: { message: { text: string; kind: "ok" | "error" } | null }) {
  return (
    <p
      className={
        message?.kind === "error"
          ? "admin-page__action-msg admin-page__action-msg--error"
          : "admin-page__action-msg"
      }
      role="status"
      aria-live="polite"
    >
      {message?.text ?? ""}
    </p>
  );
}

function UserStatusBadge({ user }: { user: AdminUser }) {
  if (!user.enabled) return <Badge label="blocked" status="blocked" />;
  if (user.status === "active") return <Badge label="active" status="done" />;
  return <Badge label="pending" status="todo" />;
}

// -------------------------------------------------------------------------
// Users panel
// -------------------------------------------------------------------------

function UsersPanel({ state, onChanged }: { state: UsersState; onChanged: () => void }) {
  const [pendingOnly, setPendingOnly] = useState(false);
  const { busy, message, run } = useRowAction(onChanged);

  if (state.status === "loading") return <PanelSkeleton label="Loading users" />;
  if (state.status === "error") return <PanelError message={state.error.message} />;

  const humans = state.users.filter((u) => !isAgent(u));
  const shown = pendingOnly ? humans.filter((u) => u.status === "pending") : humans;

  return (
    <div className="admin-panel">
      <div className="admin-panel__toolbar">
        <label className="admin-panel__filter">
          <input type="checkbox" checked={pendingOnly} onChange={(e) => setPendingOnly(e.target.checked)} />
          Pending only
        </label>
      </div>
      <ActionMessage message={message} />
      {shown.length === 0 ? (
        <Card>
          <p>No users.</p>
        </Card>
      ) : (
        <div className="admin-table__scroll">
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">User</th>
                <th scope="col">Status</th>
                <th scope="col">Groups</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((u) => {
                const isRowBusy = busy === u.username;
                return (
                  <tr key={u.username} aria-busy={isRowBusy}>
                    <td>
                      <span className="admin-table__user">{u.email ?? u.username}</span>
                      {u.email && u.email !== u.username ? (
                        <span className="admin-table__sub">{u.username}</span>
                      ) : null}
                    </td>
                    <td>
                      <UserStatusBadge user={u} />
                    </td>
                    <td className="admin-table__groups">{u.groups.length ? u.groups.join(", ") : "-"}</td>
                    <td className="admin-table__actions">
                      {u.status === "pending" ? (
                        <button
                          type="button"
                          disabled={isRowBusy}
                          onClick={() => void run(u.username, `Approved ${u.username}`, () => approveUser(u.username))}
                        >
                          Approve
                        </button>
                      ) : null}
                      {u.enabled ? (
                        <button
                          type="button"
                          disabled={isRowBusy}
                          onClick={() =>
                            void run(
                              u.username,
                              `Blocked ${u.username}`,
                              () => blockUser(u.username),
                              `Block ${u.username}? They lose access immediately.`
                            )
                          }
                        >
                          Block
                        </button>
                      ) : (
                        <button
                          type="button"
                          disabled={isRowBusy}
                          onClick={() => void run(u.username, `Unblocked ${u.username}`, () => unblockUser(u.username))}
                        >
                          Unblock
                        </button>
                      )}
                      {u.groups.includes(adminGroup()) ? (
                        <button
                          type="button"
                          disabled={isRowBusy}
                          onClick={() => void run(u.username, `Demoted ${u.username}`, () => demoteUser(u.username))}
                        >
                          Demote
                        </button>
                      ) : (
                        <button
                          type="button"
                          disabled={isRowBusy}
                          onClick={() => void run(u.username, `Promoted ${u.username}`, () => promoteUser(u.username))}
                        >
                          Promote
                        </button>
                      )}
                      <button
                        type="button"
                        className="admin-table__danger"
                        disabled={isRowBusy}
                        onClick={() =>
                          void run(
                            u.username,
                            `Deleted ${u.username}`,
                            () => deleteUser(u.username),
                            `Delete ${u.username}? This permanently removes the account.`
                          )
                        }
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// -------------------------------------------------------------------------
// Agents panel
// -------------------------------------------------------------------------

function AgentsPanel({
  state,
  reload,
  now,
  onChanged,
}: {
  state: UsersState;
  reload: number;
  now: number;
  onChanged: () => void;
}) {
  const [statsState, setStatsState] = useState<StatsState>({ status: "idle" });
  const { busy, message, run } = useRowAction(onChanged);

  useEffect(() => {
    let cancelled = false;
    setStatsState({ status: "loading" });
    listProjects()
      .then(async (projects) => {
        const perProject = await Promise.all(
          projects.map(async (project) => {
            const [tasks, notes] = await Promise.all([
              listTasks(project.slug, { limit: FETCH_LIMIT }),
              listProjectNotes(project.slug, { limit: FETCH_LIMIT }),
            ]);
            return { tasks, notes };
          })
        );
        if (cancelled) return;
        const allTasks = perProject.flatMap((p) => p.tasks);
        const allNotes = perProject.flatMap((p) => p.notes);
        setStatsState({ status: "ready", stats: computeAgentStats(allTasks, allNotes) });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setStatsState({
            status: "error",
            error: error instanceof Error ? error : new Error(String(error)),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [reload]);

  if (state.status === "loading") return <PanelSkeleton label="Loading agents" />;
  if (state.status === "error") return <PanelError message={state.error.message} />;

  const agents = state.users.filter(isAgent);
  const stats = statsState.status === "ready" ? statsState.stats : null;

  return (
    <div className="admin-panel">
      <p className="admin-panel__note">
        Agents are Cognito users. Stats computed client-side from tasks and notes across all projects.
      </p>
      <ActionMessage message={message} />
      {statsState.status === "error" ? (
        <p className="admin-page__action-msg admin-page__action-msg--error" role="status">
          Stats unavailable: {statsState.error.message}
        </p>
      ) : null}
      {agents.length === 0 ? (
        <Card>
          <p>No agent users.</p>
        </Card>
      ) : (
        <div className="admin-agents__grid">
          {agents.map((u) => {
            const slug = agentSlug(u);
            const s = stats?.get(slug) ?? null;
            const isRowBusy = busy === u.username;
            return (
              <Card key={u.username} className="admin-agent-card">
                <div className="admin-agent-card__head">
                  <h2 className="admin-agent-card__name">{slug}</h2>
                  <UserStatusBadge user={u} />
                </div>
                <p className="admin-agent-card__groups">{u.groups.length ? u.groups.join(", ") : "no groups"}</p>
                <div className="admin-agent-card__stats">
                  <StatChip label="active" value={s ? s.active : "-"} status="in_progress" />
                  <StatChip label="completed" value={s ? s.completed : "-"} status="done" />
                  <StatChip label="tokens" value={s ? formatTokens(s.tokens) : "-"} />
                </div>
                <p className="admin-agent-card__last">
                  {s && s.lastActiveMs !== null
                    ? `Last active ${formatRelativeTime(now - s.lastActiveMs)}`
                    : statsState.status === "loading"
                      ? "Loading stats..."
                      : "No recent activity"}
                </p>
                <div className="admin-agent-card__actions">
                  {u.enabled ? (
                    <button
                      type="button"
                      disabled={isRowBusy}
                      onClick={() =>
                        void run(
                          u.username,
                          `Blocked ${slug}`,
                          () => blockUser(u.username),
                          `Block agent ${slug}? It can no longer authenticate.`
                        )
                      }
                    >
                      Block
                    </button>
                  ) : (
                    <button
                      type="button"
                      disabled={isRowBusy}
                      onClick={() => void run(u.username, `Unblocked ${slug}`, () => unblockUser(u.username))}
                    >
                      Unblock
                    </button>
                  )}
                  <button
                    type="button"
                    className="admin-table__danger"
                    disabled={isRowBusy}
                    onClick={() =>
                      void run(
                        u.username,
                        `Deleted ${slug}`,
                        () => deleteUser(u.username),
                        `Delete agent ${slug}? This permanently removes the account.`
                      )
                    }
                  >
                    Delete
                  </button>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}

function formatTokens(n: number): string {
  if (n <= 0) return "0";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

// -------------------------------------------------------------------------
// Invites panel
// -------------------------------------------------------------------------

function InvitesPanel({ reload, onChanged }: { reload: number; onChanged: () => void }) {
  const [state, setState] = useState<InvitesState>({ status: "loading" });
  const [email, setEmail] = useState("");
  const [ttlDays, setTtlDays] = useState("14");
  const [approved, setApproved] = useState(false);
  const [minting, setMinting] = useState(false);
  const [minted, setMinted] = useState<InviteMint | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    listInvites()
      .then((invites) => {
        if (!cancelled) setState({ status: "ready", invites });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({ status: "error", error: err instanceof Error ? err : new Error(String(err)) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [reload]);

  async function handleMint(event: FormEvent) {
    event.preventDefault();
    if (minting) return;
    setError("");
    setMinted(null);
    setCopied(false);
    setMinting(true);
    try {
      const ttl = ttlDays.trim() === "" ? undefined : Number(ttlDays);
      const result = await mintInvite({
        email: email.trim() || undefined,
        ttl_days: ttl,
        approved,
      });
      setMinted(result);
      setEmail("");
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : err instanceof Error ? err.message : String(err));
    } finally {
      setMinting(false);
    }
  }

  async function copyJoinUrl(mint: InviteMint) {
    try {
      await navigator.clipboard.writeText(mint.join_url);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  const sortedInvites = useMemo(() => {
    if (state.status !== "ready") return [] as Invite[];
    return [...state.invites].sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0));
  }, [state]);

  return (
    <div className="admin-panel">
      <Card className="admin-invite-mint">
        <h2 className="admin-invite-mint__title">Mint invite</h2>
        <form className="admin-invite-mint__form" onSubmit={(e) => void handleMint(e)}>
          <label className="admin-field">
            <span className="admin-field__label">Email (optional - pins the invite)</span>
            <input
              type="email"
              className="admin-field__input"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="off"
            />
          </label>
          <label className="admin-field">
            <span className="admin-field__label">TTL (days)</span>
            <input
              type="number"
              min={1}
              max={90}
              className="admin-field__input"
              value={ttlDays}
              onChange={(e) => setTtlDays(e.target.value)}
            />
          </label>
          <label className="admin-field admin-field--check">
            <input type="checkbox" checked={approved} onChange={(e) => setApproved(e.target.checked)} />
            <span className="admin-field__label">Pre-approved (lands in spec-readers)</span>
          </label>
          <button type="submit" className="admin-invite-mint__submit" aria-busy={minting}>
            Mint invite
          </button>
        </form>
        {error ? (
          <p className="admin-page__action-msg admin-page__action-msg--error" role="alert">
            {error}
          </p>
        ) : null}
        {minted ? (
          <div className="admin-invite-mint__result" role="status" aria-live="polite">
            <p className="admin-invite-mint__once">Shown once - copy it now.</p>
            <p className="admin-invite-mint__code">
              <span className="admin-invite-mint__code-label">Code</span>
              <code>{minted.code}</code>
            </p>
            <p className="admin-invite-mint__url">
              <span className="admin-invite-mint__code-label">Join URL</span>
              <code>{minted.join_url}</code>
            </p>
            <button type="button" onClick={() => void copyJoinUrl(minted)}>
              {copied ? "Copied" : "Copy join URL"}
            </button>
          </div>
        ) : null}
      </Card>

      <h2 className="admin-page__section-title">Active invites</h2>
      {state.status === "loading" ? (
        <PanelSkeleton label="Loading invites" />
      ) : state.status === "error" ? (
        <PanelError message={state.error.message} />
      ) : sortedInvites.length === 0 ? (
        <Card>
          <p>No active invites.</p>
        </Card>
      ) : (
        <div className="admin-table__scroll">
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">Code hash</th>
                <th scope="col">Status</th>
                <th scope="col">Expires</th>
                <th scope="col">Bound</th>
                <th scope="col">Approved</th>
              </tr>
            </thead>
            <tbody>
              {sortedInvites.map((inv) => (
                <tr key={inv.code_hash}>
                  <td className="admin-table__hash" title={inv.code_hash}>
                    {inv.code_hash.slice(0, 12)}
                  </td>
                  <td>
                    <Badge label={inv.status} status={inv.status === "active" ? "in_progress" : "todo"} />
                  </td>
                  <td>{inv.expires_at ? new Date(inv.expires_at * 1000).toLocaleDateString() : "-"}</td>
                  <td>{inv.email_bound ? "email" : "open"}</td>
                  <td>{inv.approved ? "yes" : "no"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// -------------------------------------------------------------------------
// Shared small pieces
// -------------------------------------------------------------------------

function PanelSkeleton({ label }: { label: string }) {
  return (
    <div aria-busy="true" aria-label={label}>
      <Card className="admin-page__skeleton">
        <div className="skeleton-line skeleton-line--title" />
        <div className="skeleton-line skeleton-line--body" />
      </Card>
    </div>
  );
}

function PanelError({ message }: { message: string }) {
  return (
    <Card className="admin-page__error">
      <p>Could not load from the Spec Server API.</p>
      <p className="admin-page__error-detail">{message}</p>
    </Card>
  );
}
