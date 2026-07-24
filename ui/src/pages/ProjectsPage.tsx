import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, listEpics, listProjects, listTasks } from "../api/client";
import type { Project, Task, TaskStatus } from "../api/types";
import { Badge } from "../components/Badge";
import { Card } from "../components/Card";
import { StatChip } from "../components/StatChip";
import { formatRelativeTime, useLiveRefresh } from "../hooks/useLiveRefresh";
import "./ProjectsPage.css";

type LoadState =
  | { status: "loading" }
  | { status: "error"; error: ApiError | Error }
  | { status: "ready"; projects: Project[] };

/** Per-project epic/task rollup, fetched after the project list resolves. */
type RollupState =
  | { status: "loading" }
  | { status: "error"; error: ApiError | Error }
  | { status: "ready"; epicCount: number; counts: StatusCounts; stalledCount: number };

interface StatusCounts {
  todo: number;
  in_progress: number;
  done: number;
  other: number;
  total: number;
}

const TASK_FETCH_LIMIT = 1000;

function emptyCounts(): StatusCounts {
  return { todo: 0, in_progress: 0, done: 0, other: 0, total: 0 };
}

const PRIMARY_STATUSES: ReadonlySet<TaskStatus> = new Set(["todo", "in_progress", "done"]);

function countByStatus(tasks: Task[]): StatusCounts {
  const counts = emptyCounts();
  for (const task of tasks) {
    counts.total += 1;
    if (PRIMARY_STATUSES.has(task.status)) {
      counts[task.status as "todo" | "in_progress" | "done"] += 1;
    } else {
      counts.other += 1;
    }
  }
  return counts;
}

/**
 * Health heuristic: among tasks currently `in_progress`, how many have a
 * lease that has already expired? A task can be "in progress" without ever
 * having been claimed (no `lease_expires_at`), so only an *expired* lease on
 * an in-progress task is treated as a signal that an agent grabbed it and
 * went silent - it is the one honest, directly observable "stalled" signal
 * the API exposes today (no polling/heartbeat data exists to say more).
 */
function countStalled(tasks: Task[]): number {
  const now = Date.now();
  return tasks.filter(
    (task) =>
      task.status === "in_progress" &&
      task.lease_expires_at !== null &&
      new Date(task.lease_expires_at).getTime() < now
  ).length;
}

/** How often the project list auto-refreshes in the background (page default). */
const AUTO_REFRESH_MS = 30_000;

export function ProjectsPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [rollups, setRollups] = useState<Record<string, RollupState>>({});
  // `reload` re-runs the fetch effect below - it backs both the manual
  // Refresh/Retry controls and the background auto-refresh (cadence set by the
  // header Auto-refresh control; "Off" stops the poll but Refresh still works).
  const { reload, refresh, lastUpdated, markUpdated, now } = useLiveRefresh(AUTO_REFRESH_MS);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    listProjects()
      .then((projects) => {
        if (cancelled) return;
        setState({ status: "ready", projects });
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
  }, [reload]);

  useEffect(() => {
    if (state.status !== "ready") return;
    let cancelled = false;
    const initial: Record<string, RollupState> = {};
    for (const project of state.projects) {
      initial[project.public_id] = { status: "loading" };
    }
    setRollups(initial);

    // Fetch every project's rollup in parallel; each project's failure is
    // isolated so one bad fetch never blanks the rest of the page.
    void Promise.all(
      state.projects.map(async (project) => {
        try {
          const [epics, tasks] = await Promise.all([
            listEpics(project.slug),
            listTasks(project.slug, { limit: TASK_FETCH_LIMIT }),
          ]);
          if (cancelled) return;
          setRollups((prev) => ({
            ...prev,
            [project.public_id]: {
              status: "ready",
              epicCount: epics.length,
              counts: countByStatus(tasks),
              stalledCount: countStalled(tasks),
            },
          }));
        } catch (error) {
          if (cancelled) return;
          setRollups((prev) => ({
            ...prev,
            [project.public_id]: {
              status: "error",
              error: error instanceof Error ? error : new Error(String(error)),
            },
          }));
        }
      })
    );

    return () => {
      cancelled = true;
    };
  }, [state]);

  return (
    <section className="projects-page">
      <header className="projects-page__header">
        <h1 className="projects-page__title">Projects</h1>
        <div className="projects-page__header-controls">
          {lastUpdated !== null && (
            <span className="projects-page__updated">
              Updated {formatRelativeTime(now - lastUpdated)}
            </span>
          )}
          <button
            type="button"
            className="projects-page__refresh-button"
            onClick={refresh}
          >
            Refresh
          </button>
        </div>
      </header>

      {state.status === "loading" && <ProjectsSkeleton />}

      {state.status === "error" && (
        <Card className="projects-page__error">
          <p>Could not load projects from the Spec Server API.</p>
          <p className="projects-page__error-detail">{state.error.message}</p>
          <button
            type="button"
            className="projects-page__retry-button"
            onClick={refresh}
          >
            Retry
          </button>
        </Card>
      )}

      {state.status === "ready" && state.projects.length === 0 && (
        <Card>
          <p>No projects.</p>
        </Card>
      )}

      {state.status === "ready" && state.projects.length > 0 && (
        <div className="projects-page__grid" aria-live="polite">
          {state.projects.map((project) => (
            <ProjectCard key={project.public_id} project={project} rollup={rollups[project.public_id]} />
          ))}
        </div>
      )}
    </section>
  );
}

interface ProjectCardProps {
  project: Project;
  rollup: RollupState | undefined;
}

function ProjectCard({ project, rollup }: ProjectCardProps) {
  return (
    <Link to={`/projects/${encodeURIComponent(project.slug)}`} className="project-card-link">
      <Card className="project-card">
        <span className="sr-only">Open project </span>
        <h2 className="project-card__name">{project.name}</h2>
        <p className="project-card__slug">{project.slug}</p>
        {project.description && <p className="project-card__description">{project.description}</p>}

        <div className="project-card__rollup" aria-live="polite">
          {(!rollup || rollup.status === "loading") && (
            <p className="project-card__rollup-status" aria-busy="true">
              Loading epics &amp; tasks&hellip;
            </p>
          )}

          {rollup?.status === "error" && (
            <Badge label="Rollup unavailable" status="blocked" />
          )}

          {rollup?.status === "ready" && <ProjectRollup epicCount={rollup.epicCount} counts={rollup.counts} stalledCount={rollup.stalledCount} />}
        </div>
      </Card>
    </Link>
  );
}

interface ProjectRollupProps {
  epicCount: number;
  counts: StatusCounts;
  stalledCount: number;
}

function ProjectRollup({ epicCount, counts, stalledCount }: ProjectRollupProps) {
  const completionPct = counts.total === 0 ? null : Math.round((counts.done / counts.total) * 100);
  const activeInProgress = counts.in_progress - stalledCount;

  return (
    <>
      <p className="project-card__summary">
        <span className="sr-only">Task breakdown: </span>
        {counts.total === 0
          ? "No tasks yet"
          : `${counts.done} done · ${counts.in_progress} in progress · ${counts.todo} todo${
              counts.other > 0 ? ` · ${counts.other} other` : ""
            }`}
      </p>

      <div className="project-card__stats">
        <StatChip label="done" value={counts.done} status="done" />
        <StatChip label="in progress" value={counts.in_progress} status="in_progress" />
        <StatChip label="todo" value={counts.todo} status="todo" />
        <StatChip label="epics" value={epicCount} />
        <StatChip label="% done" value={completionPct === null ? "—" : `${completionPct}%`} />
      </div>

      <p className="project-card__health">
        {stalledCount > 0 ? (
          <Badge
            label={`${stalledCount} stalled (lease expired)`}
            status="blocked"
          />
        ) : counts.in_progress > 0 ? (
          <Badge label={`${activeInProgress} in progress, none stalled`} status="in_progress" />
        ) : (
          <Badge label="no in-progress work" status="todo" />
        )}
      </p>
    </>
  );
}

function ProjectsSkeleton() {
  return (
    <div className="projects-page__grid" aria-busy="true" aria-label="Loading projects">
      {[0, 1, 2].map((i) => (
        <Card key={i} className="project-card project-card--skeleton">
          <div className="skeleton-line skeleton-line--title" />
          <div className="skeleton-line skeleton-line--slug" />
          <div className="skeleton-line skeleton-line--body" />
        </Card>
      ))}
    </div>
  );
}
