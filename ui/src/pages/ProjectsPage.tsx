import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, getProjectsHeads, listEpics, listProjects, listTasks } from "../api/client";
import type { ChangesHead, Project, Task, TaskStatus } from "../api/types";
import { Badge } from "../components/Badge";
import { Card } from "../components/Card";
import { StatChip } from "../components/StatChip";
import { resolveAutoRefreshMs, useAutoRefreshPreference } from "../hooks/autoRefresh";
import { formatRelativeTime } from "../hooks/useLiveRefresh";
import { syncMultiHead } from "../lib/multiHeadSync";
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

/** Fetch one project's epic/task rollup, folding any failure into an error state
 *  so a single bad project never blanks the rest of the grid. */
async function fetchRollup(slug: string): Promise<RollupState> {
  try {
    const [epics, tasks] = await Promise.all([
      listEpics(slug),
      listTasks(slug, { limit: TASK_FETCH_LIMIT }),
    ]);
    return {
      status: "ready",
      epicCount: epics.length,
      counts: countByStatus(tasks),
      stalledCount: countStalled(tasks),
    };
  } catch (error) {
    return {
      status: "error",
      error: error instanceof Error ? error : new Error(String(error)),
    };
  }
}

/** How often the project list auto-refreshes in the background (page default). */
const AUTO_REFRESH_MS = 30_000;

/** How often the "Updated Ns ago" indicator re-renders to stay current. */
const RELATIVE_TIME_TICK_MS = 1000;

export function ProjectsPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [rollups, setRollups] = useState<Record<string, RollupState>>({});
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const { preference } = useAutoRefreshPreference();
  const effectiveMs = resolveAutoRefreshMs(preference, AUTO_REFRESH_MS);

  // The background fan-out tick advances per-project cursors here (one batched
  // `/projects/heads` poll per tick) and resolves an advanced slug -> its card
  // (rollups are keyed by `public_id`). Refs so the stable tick callback reads the
  // latest without re-subscribing the interval each render.
  const checkpointsRef = useRef<Record<string, number>>({});
  const bySlugRef = useRef<Record<string, Project>>({});
  // Bumped on manual refresh / unmount so an in-flight load's late writes are
  // dropped instead of clobbering a newer load.
  const epochRef = useRef(0);

  // FULL load — mount, manual Refresh, and Retry: list the projects, capture the
  // batched head map as the checkpoint baseline (BEFORE the rollups, so any change
  // landing during the fetch is caught by the next tick), then fetch every rollup.
  const loadAll = useCallback(async () => {
    const epoch = epochRef.current;
    setState({ status: "loading" });
    try {
      const projects = await listProjects();
      if (epoch !== epochRef.current) return;
      setState({ status: "ready", projects });
      bySlugRef.current = Object.fromEntries(projects.map((p) => [p.slug, p]));
      setRollups(
        Object.fromEntries(projects.map((p) => [p.public_id, { status: "loading" as const }])),
      );

      // Baseline the fan-out cursors from ONE batched head request.
      const heads = await getProjectsHeads().catch(
        () => ({}) as Record<string, ChangesHead>,
      );
      if (epoch !== epochRef.current) return;
      checkpointsRef.current = Object.fromEntries(
        Object.entries(heads).map(([slug, h]) => [slug, h.cursor]),
      );

      await Promise.all(
        projects.map(async (project) => {
          const rollup = await fetchRollup(project.slug);
          if (epoch !== epochRef.current) return;
          setRollups((prev) => ({ ...prev, [project.public_id]: rollup }));
        }),
      );
      if (epoch !== epochRef.current) return;
      setLastUpdated(Date.now());
    } catch (error) {
      if (epoch !== epochRef.current) return;
      setState({
        status: "error",
        error: error instanceof Error ? error : new Error(String(error)),
      });
    }
  }, []);

  // BACKGROUND tick: ONE `/projects/heads` request decides which projects advanced
  // past their checkpoint; ONLY those get their rollup refetched. Idle projects
  // (head unchanged) cost no further request — the fan-out win. New projects are
  // picked up by the next full load, so the tick only refetches shown cards.
  const tick = useCallback(async () => {
    const epoch = epochRef.current;
    try {
      const { checkpoints, advanced } = await syncMultiHead(
        checkpointsRef.current,
        { getProjectsHeads },
        async (slug) => {
          const project = bySlugRef.current[slug];
          if (!project) return; // not currently on the page; folded in on next full load
          const rollup = await fetchRollup(slug);
          if (epoch !== epochRef.current) return;
          setRollups((prev) => ({ ...prev, [project.public_id]: rollup }));
        },
      );
      if (epoch !== epochRef.current) return;
      checkpointsRef.current = checkpoints;
      if (advanced.length > 0) setLastUpdated(Date.now());
    } catch {
      // Transient poll failure: keep showing the last-good rollups (no state change).
    }
  }, []);

  const refresh = useCallback(() => {
    epochRef.current += 1;
    void loadAll();
  }, [loadAll]);

  // Mount / remount: run a full load; invalidate any in-flight work on unmount.
  useEffect(() => {
    epochRef.current += 1;
    void loadAll();
    return () => {
      epochRef.current += 1;
    };
  }, [loadAll]);

  // Background poll cadence (shared dashboard auto-refresh preference; "Off" stops it).
  useEffect(() => {
    if (effectiveMs <= 0) return;
    const id = setInterval(() => void tick(), effectiveMs);
    return () => clearInterval(id);
  }, [effectiveMs, tick]);

  // Keep the "Updated Ns ago" string live without callers wiring their own interval.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), RELATIVE_TIME_TICK_MS);
    return () => clearInterval(id);
  }, []);

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
