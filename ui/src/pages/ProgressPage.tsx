import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, getProject, listEpics, listTasks } from "../api/client";
import type { Epic, Project, Task } from "../api/types";
import { Card } from "../components/Card";
import { EpicProgressChart } from "../components/EpicProgressChart";
import { ThroughputChart } from "../components/ThroughputChart";
import { formatRelativeTime, useLiveRefresh } from "../hooks/useLiveRefresh";
import "./ProgressPage.css";

type ProgressState =
  | { status: "loading" }
  | { status: "error"; error: ApiError | Error }
  | { status: "ready"; project: Project; epics: Epic[]; tasks: Task[] };

/** Same fetch cap the other project pages use - the API paginates by default. */
const TASK_FETCH_LIMIT = 1000;

/** Same background-refresh cadence as the other project pages. */
const AUTO_REFRESH_MS = 30_000;

/**
 * Burndown/throughput view (UI-6): tasks completed over time, plus per-epic
 * progress. Both charts are hand-rolled inline SVG (see ThroughputChart /
 * EpicProgressChart) - no charting library, since the app ships under a CSP
 * with no `unsafe-inline` for styles.
 */
export function ProgressPage() {
  const { slug = "" } = useParams<{ slug: string }>();
  const [state, setState] = useState<ProgressState>({ status: "loading" });
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
    <section className="progress-page">
      <p className="progress-page__back">
        <Link to={`/projects/${encodeURIComponent(slug)}`}>&larr; {slug}</Link>
      </p>

      <header className="progress-page__header">
        <div>
          <h1 className="progress-page__title">Progress</h1>
          <p className="progress-page__subtitle">Burndown / throughput and per-epic completion.</p>
        </div>
        <div className="progress-page__header-controls">
          {lastUpdated !== null && (
            <span className="progress-page__updated">Updated {formatRelativeTime(now - lastUpdated)}</span>
          )}
          <Link
            to={`/projects/${encodeURIComponent(slug)}/activity`}
            className="progress-page__activity-link"
          >
            Activity
          </Link>
          <button type="button" className="progress-page__refresh-button" onClick={refresh}>
            Refresh
          </button>
        </div>
      </header>

      {state.status === "loading" && <ProgressSkeleton />}

      {state.status === "error" && (
        <Card className="progress-page__error">
          <p>Could not load progress data from the Spec Server API.</p>
          <p className="progress-page__error-detail">{state.error.message}</p>
          <button type="button" className="progress-page__retry-button" onClick={refresh}>
            Retry
          </button>
        </Card>
      )}

      {state.status === "ready" && (
        <div aria-live="polite">
          <section className="progress-page__section" aria-label="Throughput">
            <h2 className="progress-page__section-title">Throughput</h2>
            {state.tasks.length === 0 ? (
              <Card>
                <p>Not enough data yet.</p>
              </Card>
            ) : (
              <Card>
                <ThroughputChart tasks={state.tasks} />
              </Card>
            )}
          </section>

          <section className="progress-page__section" aria-label="Per-epic progress">
            <h2 className="progress-page__section-title">Per-epic progress</h2>
            {state.epics.length === 0 && state.tasks.length === 0 ? (
              <Card>
                <p>Not enough data yet.</p>
              </Card>
            ) : (
              <Card>
                <EpicProgressChart epics={state.epics} tasks={state.tasks} />
              </Card>
            )}
          </section>
        </div>
      )}
    </section>
  );
}

function ProgressSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading progress">
      <Card className="progress-page__skeleton">
        <div className="skeleton-line skeleton-line--title" />
        <div className="skeleton-line skeleton-line--body" />
        <div className="skeleton-line skeleton-line--body" />
      </Card>
    </div>
  );
}
