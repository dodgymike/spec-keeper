import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, getProject, listEpics, listTasks } from "../api/client";
import type { Epic, Project, Task } from "../api/types";
import { Badge } from "../components/Badge";
import { Card } from "../components/Card";
import { formatRelativeTime, useLiveRefresh } from "../hooks/useLiveRefresh";
import "./ProjectDetailPage.css";

type DetailState =
  | { status: "loading" }
  | { status: "error"; error: ApiError | Error }
  | { status: "ready"; project: Project; epics: Epic[]; tasks: Task[] };

/** Same fetch cap ProjectsPage uses for its rollups - the API paginates by default. */
const TASK_FETCH_LIMIT = 1000;

/** Same background-refresh cadence as ProjectsPage. */
const AUTO_REFRESH_MS = 30_000;

const EPIC_SECTIONS: ReadonlyArray<{ key: Epic["section"]; label: string }> = [
  { key: "backlog", label: "Backlog" },
  { key: "to_do", label: "To do" },
  { key: "in_progress", label: "In progress" },
  { key: "completed", label: "Completed" },
];

/** How many minutes remain before a lease expires, or "expired" if past due. */
function describeLease(leaseExpiresAt: string, now: number): { label: string; expired: boolean } {
  const remainingMs = new Date(leaseExpiresAt).getTime() - now;
  if (remainingMs <= 0) {
    return { label: "lease expired", expired: true };
  }
  const minutes = Math.max(1, Math.round(remainingMs / 60_000));
  return { label: `expires in ${minutes}m`, expired: false };
}

export function ProjectDetailPage() {
  const { slug = "" } = useParams<{ slug: string }>();
  const [state, setState] = useState<DetailState>({ status: "loading" });
  const { reload, refresh, lastUpdated, markUpdated, now } = useLiveRefresh(AUTO_REFRESH_MS);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    Promise.all([getProject(slug), listEpics(slug), listTasks(slug, { limit: TASK_FETCH_LIMIT })])
      .then(([project, epics, tasks]) => {
        if (cancelled) return;
        setState({ status: "ready", project, epics, tasks });
        markUpdated();
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({
            status: "error",
            error: error instanceof Error ? error : new Error(String(error)),
          });
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, reload]);

  return (
    <section className="project-detail-page">
      <p className="project-detail-page__back">
        <Link to="/">&larr; All projects</Link>
      </p>

      <header className="project-detail-page__header">
        <div className="project-detail-page__heading">
          {state.status === "ready" ? (
            <>
              <h1 className="project-detail-page__name">{state.project.name}</h1>
              <p className="project-detail-page__slug">{state.project.slug}</p>
              {state.project.description && (
                <p className="project-detail-page__description">{state.project.description}</p>
              )}
            </>
          ) : (
            <h1 className="project-detail-page__name project-detail-page__name--fallback">{slug}</h1>
          )}
        </div>
        <div className="project-detail-page__header-controls">
          {lastUpdated !== null && (
            <span className="project-detail-page__updated">
              Updated {formatRelativeTime(now - lastUpdated)}
            </span>
          )}
          <Link
            to={`/projects/${encodeURIComponent(slug)}/progress`}
            className="project-detail-page__activity-link"
          >
            Progress
          </Link>
          <Link
            to={`/projects/${encodeURIComponent(slug)}/activity`}
            className="project-detail-page__activity-link"
          >
            Activity
          </Link>
          <button type="button" className="project-detail-page__refresh-button" onClick={refresh}>
            Refresh
          </button>
        </div>
      </header>

      {state.status === "loading" && <DetailSkeleton />}

      {state.status === "error" && (
        <Card className="project-detail-page__error">
          <p>Could not load this project from the Spec Server API.</p>
          <p className="project-detail-page__error-detail">{state.error.message}</p>
          <button type="button" className="project-detail-page__retry-button" onClick={refresh}>
            Retry
          </button>
        </Card>
      )}

      {state.status === "ready" && (
        <div aria-live="polite">
          <EpicBoard epics={state.epics} tasks={state.tasks} />
          <TaskList epics={state.epics} tasks={state.tasks} now={now} />
        </div>
      )}
    </section>
  );
}

interface EpicBoardProps {
  epics: Epic[];
  tasks: Task[];
}

function EpicBoard({ epics, tasks }: EpicBoardProps) {
  return (
    <div className="epic-board" aria-label="Epic board">
      <h2 className="project-detail-page__section-title">Epics</h2>
      {epics.length === 0 ? (
        <Card>
          <p>No epics.</p>
        </Card>
      ) : (
        <div className="epic-board__scroll">
          <div className="epic-board__columns">
            {EPIC_SECTIONS.map((section) => (
              <EpicColumn
                key={section.key}
                label={section.label}
                epics={epics
                  .filter((epic) => epic.section === section.key)
                  .sort((a, b) => a.position - b.position)}
                tasks={tasks}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

interface EpicColumnProps {
  label: string;
  epics: Epic[];
  tasks: Task[];
}

function EpicColumn({ label, epics, tasks }: EpicColumnProps) {
  return (
    <div className="epic-board__column">
      <h3 className="epic-board__column-title">
        {label} <span className="epic-board__column-count">{epics.length}</span>
      </h3>
      <div className="epic-board__column-body">
        {epics.length === 0 ? (
          <p className="epic-board__column-empty">Empty</p>
        ) : (
          epics.map((epic) => <EpicCard key={epic.public_id} epic={epic} tasks={tasks} />)
        )}
      </div>
    </div>
  );
}

interface EpicCardProps {
  epic: Epic;
  tasks: Task[];
}

function EpicCard({ epic, tasks }: EpicCardProps) {
  const epicTasks = useMemo(() => tasks.filter((task) => task.epic_key === epic.key), [tasks, epic.key]);
  const done = epicTasks.filter((task) => task.status === "done").length;

  return (
    <Card className="epic-card">
      <p className="epic-card__key">{epic.key}</p>
      <p className="epic-card__title">{epic.title}</p>
      <p className="epic-card__rollup">
        {epicTasks.length === 0 ? "No tasks" : `${done}/${epicTasks.length} done`}
      </p>
    </Card>
  );
}

interface TaskListProps {
  epics: Epic[];
  tasks: Task[];
  now: number;
}

function TaskList({ epics, tasks, now }: TaskListProps) {
  const epicTitleByKey = useMemo(() => {
    const map = new Map<string, string>();
    for (const epic of epics) map.set(epic.key, epic.title);
    return map;
  }, [epics]);

  const groups = useMemo(() => groupTasksByEpic(tasks, epics), [tasks, epics]);

  return (
    <div className="task-list" aria-label="Tasks">
      <h2 className="project-detail-page__section-title">Tasks</h2>
      {tasks.length === 0 ? (
        <Card>
          <p>No tasks.</p>
        </Card>
      ) : (
        groups.map(([epicKey, groupTasks]) => (
          <div className="task-list__group" key={epicKey ?? "__none__"}>
            <h3 className="task-list__group-title">
              {epicKey ? `${epicKey} – ${epicTitleByKey.get(epicKey) ?? epicKey}` : "No epic"}
              <span className="task-list__group-count">{groupTasks.length}</span>
            </h3>
            <div className="task-list__scroll">
              <table className="task-list__table">
                <thead>
                  <tr>
                    <th scope="col">Key</th>
                    <th scope="col">Title</th>
                    <th scope="col">Status</th>
                    <th scope="col">Priority</th>
                    <th scope="col">Component</th>
                    <th scope="col">Owner</th>
                    <th scope="col">Lease</th>
                  </tr>
                </thead>
                <tbody>
                  {groupTasks.map((task) => (
                    <TaskRow key={task.public_id} task={task} now={now} />
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))
      )}
    </div>
  );
}

interface TaskRowProps {
  task: Task;
  now: number;
}

function TaskRow({ task, now }: TaskRowProps) {
  const lease =
    task.status === "in_progress" && task.lease_expires_at ? describeLease(task.lease_expires_at, now) : null;

  return (
    <tr>
      <td className="task-list__cell-key">{task.key ?? task.display_id}</td>
      <td className="task-list__cell-title">{task.title}</td>
      <td>
        <Badge label={task.status} status={task.status} />
      </td>
      <td>{task.priority ?? "—"}</td>
      <td>{task.component ?? "—"}</td>
      <td>{task.owner ?? "—"}</td>
      <td>
        {lease && (
          <Badge label={lease.label} status={lease.expired ? "blocked" : "in_progress"} />
        )}
        {!lease && "—"}
      </td>
    </tr>
  );
}

/** Groups tasks by `epic_key`, ordered to match the project's epic order, with an "unassigned" bucket last. */
function groupTasksByEpic(tasks: Task[], epics: Epic[]): Array<[string | null, Task[]]> {
  const byKey = new Map<string | null, Task[]>();
  for (const task of tasks) {
    const key = task.epic_key;
    const bucket = byKey.get(key);
    if (bucket) {
      bucket.push(task);
    } else {
      byKey.set(key, [task]);
    }
  }
  for (const bucket of byKey.values()) {
    bucket.sort((a, b) => a.position - b.position);
  }

  const ordered: Array<[string | null, Task[]]> = [];
  for (const epic of epics) {
    const bucket = byKey.get(epic.key);
    if (bucket) {
      ordered.push([epic.key, bucket]);
      byKey.delete(epic.key);
    }
  }
  const noEpic = byKey.get(null);
  byKey.delete(null);
  // Any remaining keys reference an epic not present in `epics` (shouldn't
  // normally happen, but keep them visible rather than silently dropping).
  for (const [key, bucket] of byKey) {
    ordered.push([key, bucket]);
  }
  if (noEpic) {
    ordered.push([null, noEpic]);
  }
  return ordered;
}

function DetailSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading project">
      <Card className="project-detail-page__skeleton">
        <div className="skeleton-line skeleton-line--title" />
        <div className="skeleton-line skeleton-line--body" />
      </Card>
    </div>
  );
}
