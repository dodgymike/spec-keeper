import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, listCounters, listEpics, listProjects, listTasks } from "../api/client";
import type { Project, Task } from "../api/types";
import { Badge } from "../components/Badge";
import { Card } from "../components/Card";
import { formatRelativeTime, useLiveRefresh } from "../hooks/useLiveRefresh";
import "./CoordinationPage.css";

/** Same fetch cap other pages use - the API paginates by default. */
const TASK_FETCH_LIMIT = 1000;

/** Same background-refresh cadence as ProjectDetailPage. */
const AUTO_REFRESH_MS = 30_000;

interface LeaseRow {
  project: Project;
  epicTitle: string | null;
  task: Task;
}

interface CounterRow {
  project: Project;
  namespace: string;
  currentValue: number;
}

type CoordinationState =
  | { status: "loading" }
  | { status: "error"; error: ApiError | Error }
  | { status: "ready"; leases: LeaseRow[]; counters: CounterRow[] };

/**
 * How many minutes remain before a lease expires, or "expired" if past due.
 * Mirrors ProjectDetailPage's `describeLease` (the UI-3 lease-countdown logic).
 */
function describeLease(leaseExpiresAt: string, now: number): { label: string; expired: boolean } {
  const remainingMs = new Date(leaseExpiresAt).getTime() - now;
  if (remainingMs <= 0) {
    return { label: "lease expired", expired: true };
  }
  const minutes = Math.max(1, Math.round(remainingMs / 60_000));
  return { label: `expires in ${minutes}m`, expired: false };
}

/**
 * Coordination view: the two guarantees the Spec Server exists to provide,
 * made visible across every project - active `in_progress` leases (who holds
 * what, and for how much longer) and the collision-proof reservation
 * counters by namespace.
 */
export function CoordinationPage() {
  const [state, setState] = useState<CoordinationState>({ status: "loading" });
  const { reload, refresh, lastUpdated, markUpdated, now } = useLiveRefresh(AUTO_REFRESH_MS);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });

    listProjects()
      .then(async (projects) => {
        const perProject = await Promise.all(
          projects.map(async (project) => {
            const [epics, tasks, counters] = await Promise.all([
              listEpics(project.slug),
              listTasks(project.slug, { status: "in_progress", limit: TASK_FETCH_LIMIT }),
              listCounters(project.slug),
            ]);
            const epicTitleByKey = new Map(epics.map((epic) => [epic.key, epic.title]));
            const leases: LeaseRow[] = tasks.map((task) => ({
              project,
              epicTitle: task.epic_key ? epicTitleByKey.get(task.epic_key) ?? task.epic_key : null,
              task,
            }));
            const counterRows: CounterRow[] = counters.map((counter) => ({
              project,
              namespace: counter.namespace,
              currentValue: counter.current_value,
            }));
            return { leases, counterRows };
          })
        );
        if (cancelled) return;
        setState({
          status: "ready",
          leases: perProject.flatMap((p) => p.leases),
          counters: perProject.flatMap((p) => p.counterRows),
        });
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

  return (
    <section className="coordination-page">
      <header className="coordination-page__header">
        <div>
          <h1 className="coordination-page__title">Coordination</h1>
          <p className="coordination-page__subtitle">
            Active leases and reservation counters across all projects.
          </p>
        </div>
        <div className="coordination-page__header-controls">
          {lastUpdated !== null && (
            <span className="coordination-page__updated">
              Updated {formatRelativeTime(now - lastUpdated)}
            </span>
          )}
          <button type="button" className="coordination-page__refresh-button" onClick={refresh}>
            Refresh
          </button>
        </div>
      </header>

      {state.status === "loading" && <CoordinationSkeleton />}

      {state.status === "error" && (
        <Card className="coordination-page__error">
          <p>Could not load coordination state from the Spec Server API.</p>
          <p className="coordination-page__error-detail">{state.error.message}</p>
          <button type="button" className="coordination-page__retry-button" onClick={refresh}>
            Retry
          </button>
        </Card>
      )}

      {state.status === "ready" && (
        <div aria-live="polite">
          <LeaseBoard leases={state.leases} now={now} />
          <CounterTable counters={state.counters} />
        </div>
      )}
    </section>
  );
}

interface LeaseBoardProps {
  leases: LeaseRow[];
  now: number;
}

function LeaseBoard({ leases, now }: LeaseBoardProps) {
  const groups = useMemo(() => groupLeasesByOwner(leases), [leases]);

  return (
    <div className="lease-board" aria-label="Active leases">
      <h2 className="coordination-page__section-title">Active leases</h2>
      {leases.length === 0 ? (
        <Card>
          <p>No active leases.</p>
        </Card>
      ) : (
        groups.map(([owner, ownerLeases]) => (
          <div className="lease-board__group" key={owner}>
            <h3 className="lease-board__group-title">
              {owner}
              <span className="lease-board__group-count">{ownerLeases.length}</span>
            </h3>
            <div className="lease-board__scroll">
              <table className="lease-board__table">
                <thead>
                  <tr>
                    <th scope="col">Project</th>
                    <th scope="col">Task</th>
                    <th scope="col">Title</th>
                    <th scope="col">Epic</th>
                    <th scope="col">Lease</th>
                  </tr>
                </thead>
                <tbody>
                  {ownerLeases.map(({ project, epicTitle, task }) => {
                    const lease = task.lease_expires_at ? describeLease(task.lease_expires_at, now) : null;
                    return (
                      <tr key={`${project.public_id}:${task.public_id}`}>
                        <td>
                          <Link to={`/projects/${encodeURIComponent(project.slug)}`}>{project.slug}</Link>
                        </td>
                        <td className="lease-board__cell-key">{task.key ?? task.display_id}</td>
                        <td className="lease-board__cell-title">{task.title}</td>
                        <td>{epicTitle ?? "—"}</td>
                        <td>
                          {lease ? (
                            <Badge label={lease.label} status={lease.expired ? "blocked" : "in_progress"} />
                          ) : (
                            "—"
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ))
      )}
    </div>
  );
}

/**
 * Groups leases by owner (agent slug), "Unassigned" bucket last, so it's
 * easy to see which agent holds what. Leases within a group are ordered
 * soonest-expiring first.
 */
function groupLeasesByOwner(leases: LeaseRow[]): Array<[string, LeaseRow[]]> {
  const byOwner = new Map<string, LeaseRow[]>();
  for (const lease of leases) {
    const owner = lease.task.owner ?? "Unassigned";
    const bucket = byOwner.get(owner);
    if (bucket) {
      bucket.push(lease);
    } else {
      byOwner.set(owner, [lease]);
    }
  }
  for (const bucket of byOwner.values()) {
    bucket.sort((a, b) => {
      const aTime = a.task.lease_expires_at ? new Date(a.task.lease_expires_at).getTime() : Infinity;
      const bTime = b.task.lease_expires_at ? new Date(b.task.lease_expires_at).getTime() : Infinity;
      return aTime - bTime;
    });
  }
  const owners = [...byOwner.keys()].sort((a, b) => {
    if (a === "Unassigned") return b === "Unassigned" ? 0 : 1;
    if (b === "Unassigned") return -1;
    return a.localeCompare(b);
  });
  return owners.map((owner) => [owner, byOwner.get(owner) as LeaseRow[]]);
}

interface CounterTableProps {
  counters: CounterRow[];
}

function CounterTable({ counters }: CounterTableProps) {
  const sorted = useMemo(
    () =>
      [...counters].sort(
        (a, b) => a.project.slug.localeCompare(b.project.slug) || a.namespace.localeCompare(b.namespace)
      ),
    [counters]
  );

  return (
    <div className="counter-table" aria-label="Reservation counters">
      <h2 className="coordination-page__section-title">Reservations &amp; counters</h2>
      {sorted.length === 0 ? (
        <Card>
          <p>No reservation counters.</p>
        </Card>
      ) : (
        <div className="counter-table__scroll">
          <table className="counter-table__table">
            <thead>
              <tr>
                <th scope="col">Project</th>
                <th scope="col">Namespace</th>
                <th scope="col">Current value</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((row) => (
                <tr key={`${row.project.public_id}:${row.namespace}`}>
                  <td>
                    <Link to={`/projects/${encodeURIComponent(row.project.slug)}`}>{row.project.slug}</Link>
                  </td>
                  <td className="counter-table__cell-namespace">{row.namespace}</td>
                  <td>{row.currentValue}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function CoordinationSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading coordination state">
      <Card className="coordination-page__skeleton">
        <div className="skeleton-line skeleton-line--title" />
        <div className="skeleton-line skeleton-line--body" />
      </Card>
    </div>
  );
}
