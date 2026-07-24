import { Link, useParams } from "react-router-dom";
import { Card } from "../components/Card";
import { EpicProgressChart } from "../components/EpicProgressChart";
import { ThroughputChart } from "../components/ThroughputChart";
import { useDeltaRefresh } from "../hooks/useDeltaRefresh";
import { formatRelativeTime } from "../hooks/useLiveRefresh";
import "./ProgressPage.css";

/** Same background-refresh cadence as the other project pages. */
const AUTO_REFRESH_MS = 30_000;

/**
 * Burndown/throughput view (UI-6): tasks completed over time, plus per-epic
 * progress. Both charts are hand-rolled inline SVG (see ThroughputChart /
 * EpicProgressChart) - no charting library, since the app ships under a CSP
 * with no `unsafe-inline` for styles.
 *
 * Live data comes from the incremental change feed (UI-DELTA-8): a cold start
 * hydrates the cache with one full REST fetch, then each tick polls the head
 * cursor and only re-fetches deltas when it moves, folding them into a
 * normalized cache the charts render from — no more full list refetch per tick.
 */
export function ProgressPage() {
  const { slug = "" } = useParams<{ slug: string }>();
  const { tasks, epics, status, error, lastUpdated, now, refresh } = useDeltaRefresh(
    slug,
    AUTO_REFRESH_MS,
  );

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

      {status === "loading" && <ProgressSkeleton />}

      {status === "error" && (
        <Card className="progress-page__error">
          <p>Could not load progress data from the Spec Server API.</p>
          <p className="progress-page__error-detail">{error?.message}</p>
          <button type="button" className="progress-page__retry-button" onClick={refresh}>
            Retry
          </button>
        </Card>
      )}

      {status === "ready" && (
        <div aria-live="polite">
          <section className="progress-page__section" aria-label="Throughput">
            <h2 className="progress-page__section-title">Throughput</h2>
            {tasks.length === 0 ? (
              <Card>
                <p>Not enough data yet.</p>
              </Card>
            ) : (
              <Card>
                <ThroughputChart tasks={tasks} />
              </Card>
            )}
          </section>

          <section className="progress-page__section" aria-label="Per-epic progress">
            <h2 className="progress-page__section-title">Per-epic progress</h2>
            {epics.length === 0 && tasks.length === 0 ? (
              <Card>
                <p>Not enough data yet.</p>
              </Card>
            ) : (
              <Card>
                <EpicProgressChart epics={epics} tasks={tasks} />
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
