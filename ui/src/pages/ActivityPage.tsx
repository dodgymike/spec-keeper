import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, getChainRun, listChainRuns, listEvents, listProjectNotes } from "../api/client";
import type { ChainRun, ProjectEvent, ProjectNote } from "../api/types";
import { Badge } from "../components/Badge";
import { Card } from "../components/Card";
import { formatRelativeTime, useLiveRefresh } from "../hooks/useLiveRefresh";
import "./ActivityPage.css";

/** Same background-refresh cadence as the other pages. */
const AUTO_REFRESH_MS = 30_000;

/** Notes/events fetch cap per page load; "Load more" bumps this. */
const INITIAL_LIMIT = 100;
const LOAD_MORE_STEP = 100;
const MAX_LIMIT = 1000;

type FeedState =
  | { status: "loading" }
  | { status: "error"; error: ApiError | Error }
  | { status: "ready"; notes: ProjectNote[]; events: ProjectEvent[] };

/** A note or event, merged into one newest-first timeline. */
type TimelineEntry =
  | { kind: "note"; createdAt: string; note: ProjectNote }
  | { kind: "event"; createdAt: string; event: ProjectEvent };

/**
 * Agents tag note bodies with a leading `[report]`/`[response]`/`[model]` or
 * `kind=report` marker. Parses that prefix off so it can render as a Badge
 * instead of raw text, degrading gracefully (no match -> no badge, full body).
 */
const NOTE_KIND_PATTERN = /^\s*(?:\[(report|response|model)\]|kind=(report|response|model))\s*[:\-]?\s*/i;

function parseNoteKind(body: string): { kind: string | null; text: string } {
  const match = body.match(NOTE_KIND_PATTERN);
  if (!match) return { kind: null, text: body };
  const kind = (match[1] ?? match[2]).toLowerCase();
  return { kind, text: body.slice(match[0].length) };
}

/** Relative time with hour/day units - activity spans longer than the "Ns ago" header indicator. */
function formatTimelineAge(iso: string, now: number): string {
  const elapsedMs = Math.max(0, now - new Date(iso).getTime());
  const seconds = Math.round(elapsedMs / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

function mergeTimeline(notes: ProjectNote[], events: ProjectEvent[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [
    ...notes.map((note): TimelineEntry => ({ kind: "note", createdAt: note.created_at, note })),
    ...events.map((event): TimelineEntry => ({ kind: "event", createdAt: event.created_at, event })),
  ];
  entries.sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());
  return entries;
}

/**
 * Project-wide activity feed: a merged, newest-first timeline of notes and
 * events (UI-5), plus a best-effort chain-run stepper. See the client
 * comment on `listChainRuns` - the backend does not yet expose a way to
 * discover a task's chain runs, only to start one or fetch one by ID, so
 * that panel is a manual lookup rather than something the timeline can
 * surface automatically.
 */
export function ActivityPage() {
  const { slug = "" } = useParams<{ slug: string }>();
  const [state, setState] = useState<FeedState>({ status: "loading" });
  const [limit, setLimit] = useState(INITIAL_LIMIT);
  const [authorInput, setAuthorInput] = useState("");
  const [appliedAuthor, setAppliedAuthor] = useState("");
  const { reload, refresh, lastUpdated, markUpdated, now } = useLiveRefresh(AUTO_REFRESH_MS);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    const author = appliedAuthor.trim() || undefined;
    Promise.all([
      listProjectNotes(slug, { scope: "all", author, limit }),
      listEvents(slug, { agent: author, limit }),
    ])
      .then(([notes, events]) => {
        if (cancelled) return;
        setState({ status: "ready", notes, events });
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
  }, [slug, reload, limit, appliedAuthor]);

  const timeline = useMemo(
    () => (state.status === "ready" ? mergeTimeline(state.notes, state.events) : []),
    [state]
  );

  function handleFilterSubmit(evt: FormEvent<HTMLFormElement>) {
    evt.preventDefault();
    setLimit(INITIAL_LIMIT);
    setAppliedAuthor(authorInput);
  }

  return (
    <section className="activity-page">
      <p className="activity-page__back">
        <Link to={`/projects/${encodeURIComponent(slug)}`}>&larr; {slug}</Link>
      </p>

      <header className="activity-page__header">
        <div>
          <h1 className="activity-page__title">Activity</h1>
          <p className="activity-page__subtitle">Notes and events across this project, newest first.</p>
        </div>
        <div className="activity-page__header-controls">
          {lastUpdated !== null && (
            <span className="activity-page__updated">Updated {formatRelativeTime(now - lastUpdated)}</span>
          )}
          <button type="button" className="activity-page__refresh-button" onClick={refresh}>
            Refresh
          </button>
        </div>
      </header>

      <form className="activity-page__filters" onSubmit={handleFilterSubmit}>
        <label className="activity-page__filter-label" htmlFor="activity-author-filter">
          Author
        </label>
        <input
          id="activity-author-filter"
          type="text"
          className="activity-page__filter-input"
          placeholder="e.g. implementer"
          value={authorInput}
          onChange={(evt) => setAuthorInput(evt.target.value)}
        />
        <button type="submit" className="activity-page__filter-button">
          Apply
        </button>
        {appliedAuthor && (
          <button
            type="button"
            className="activity-page__filter-clear"
            onClick={() => {
              setAuthorInput("");
              setAppliedAuthor("");
              setLimit(INITIAL_LIMIT);
            }}
          >
            Clear
          </button>
        )}
      </form>

      {state.status === "loading" && <TimelineSkeleton />}

      {state.status === "error" && (
        <Card className="activity-page__error">
          <p>Could not load activity from the Spec Server API.</p>
          <p className="activity-page__error-detail">{state.error.message}</p>
          <button type="button" className="activity-page__retry-button" onClick={refresh}>
            Retry
          </button>
        </Card>
      )}

      {state.status === "ready" && (
        <div aria-live="polite" aria-busy="false">
          <Timeline entries={timeline} now={now} />
          {timeline.length > 0 && limit < MAX_LIMIT && (
            <button
              type="button"
              className="activity-page__load-more"
              onClick={() => setLimit((l) => Math.min(MAX_LIMIT, l + LOAD_MORE_STEP))}
            >
              Load more
            </button>
          )}
        </div>
      )}

      <ChainRunPanel slug={slug} />
    </section>
  );
}

interface TimelineProps {
  entries: TimelineEntry[];
  now: number;
}

function Timeline({ entries, now }: TimelineProps) {
  return (
    <div className="timeline" aria-label="Activity timeline">
      <h2 className="activity-page__section-title">Timeline</h2>
      {entries.length === 0 ? (
        <Card>
          <p>No activity yet.</p>
        </Card>
      ) : (
        <ol className="timeline__list">
          {entries.map((entry, index) => (
            <TimelineRow key={`${entry.kind}-${entry.createdAt}-${index}`} entry={entry} now={now} />
          ))}
        </ol>
      )}
    </div>
  );
}

interface TimelineRowProps {
  entry: TimelineEntry;
  now: number;
}

function TimelineRow({ entry, now }: TimelineRowProps) {
  const age = formatTimelineAge(entry.createdAt, now);

  if (entry.kind === "note") {
    const { note } = entry;
    const { kind, text } = parseNoteKind(note.body);
    const targetKey = note.task ?? note.epic ?? "—";
    return (
      <li className="timeline__row">
        <time className="timeline__time" dateTime={note.created_at} title={note.created_at}>
          {age}
        </time>
        <span className="timeline__actor">{note.author ?? "—"}</span>
        <span className="timeline__what">
          <Badge label={note.scope === "task" ? "task note" : "epic note"} />
          {kind && <Badge label={kind} />}
          <span className="timeline__body">{text}</span>
        </span>
        <span className="timeline__target">{targetKey}</span>
      </li>
    );
  }

  const { event } = entry;
  return (
    <li className="timeline__row">
      <time className="timeline__time" dateTime={event.created_at} title={event.created_at}>
        {age}
      </time>
      <span className="timeline__actor">{event.agent ?? "—"}</span>
      <span className="timeline__what">
        <Badge label={event.event_type} />
        <span className="timeline__body">{event.message ?? "(no message)"}</span>
      </span>
      <span className="timeline__target">
        {Object.keys(event.payload).length > 0 ? (
          <details className="timeline__payload">
            <summary>payload</summary>
            <pre>{JSON.stringify(event.payload, null, 2)}</pre>
          </details>
        ) : (
          "—"
        )}
      </span>
    </li>
  );
}

function TimelineSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading activity">
      <Card className="activity-page__skeleton">
        <div className="skeleton-line skeleton-line--title" />
        <div className="skeleton-line skeleton-line--body" />
        <div className="skeleton-line skeleton-line--body" />
      </Card>
    </div>
  );
}

/** Lookup state for the chain-run panel (deliberately separate from the timeline fetch above). */
type ChainLookupState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; runs: ChainRun[] };

interface ChainRunPanelProps {
  slug: string;
}

/**
 * Chain-run stepper, looked up on demand. The backend does not (yet) expose
 * a way to list a task's chain runs or discover run IDs from the event/note
 * feed - `POST .../tasks/{ident}/chain-runs` only starts a run, and
 * `GET .../chain-runs/{id}` needs the ID up front. So this is a manual
 * lookup rather than something that appears automatically in the timeline.
 */
function ChainRunPanel({ slug }: ChainRunPanelProps) {
  const [taskIdent, setTaskIdent] = useState("");
  const [runId, setRunId] = useState("");
  const [state, setState] = useState<ChainLookupState>({ status: "idle" });

  function lookupByTask(evt: FormEvent<HTMLFormElement>) {
    evt.preventDefault();
    const ident = taskIdent.trim();
    if (!ident) return;
    setState({ status: "loading" });
    listChainRuns(slug, ident)
      .then((runs) => setState({ status: "ready", runs }))
      .catch(() =>
        setState({
          status: "error",
          message:
            "No chain-run listing available for this task (the API only supports starting a run or " +
            "fetching one by ID - try a chain-run ID below instead).",
        })
      );
  }

  function lookupByRunId(evt: FormEvent<HTMLFormElement>) {
    evt.preventDefault();
    const id = runId.trim();
    if (!id) return;
    setState({ status: "loading" });
    getChainRun(slug, id)
      .then((run) => setState({ status: "ready", runs: [run] }))
      .catch((error: unknown) =>
        setState({
          status: "error",
          message: error instanceof ApiError ? error.message : "Chain run not found.",
        })
      );
  }

  return (
    <div className="chain-panel" aria-label="Chain runs">
      <h2 className="activity-page__section-title">Chain runs</h2>
      <Card>
        <form className="chain-panel__form" onSubmit={lookupByTask}>
          <label className="chain-panel__label" htmlFor="chain-task-ident">
            Task key or ID
          </label>
          <input
            id="chain-task-ident"
            type="text"
            className="chain-panel__input"
            placeholder="e.g. UI-5"
            value={taskIdent}
            onChange={(evt) => setTaskIdent(evt.target.value)}
          />
          <button type="submit" className="chain-panel__button">
            List runs
          </button>
        </form>
        <form className="chain-panel__form" onSubmit={lookupByRunId}>
          <label className="chain-panel__label" htmlFor="chain-run-id">
            or chain-run ID
          </label>
          <input
            id="chain-run-id"
            type="text"
            className="chain-panel__input"
            placeholder="run public_id"
            value={runId}
            onChange={(evt) => setRunId(evt.target.value)}
          />
          <button type="submit" className="chain-panel__button">
            Fetch run
          </button>
        </form>

        {state.status === "loading" && <p className="chain-panel__status" aria-busy="true">Looking up…</p>}
        {state.status === "error" && <p className="chain-panel__status chain-panel__status--error">{state.message}</p>}
        {state.status === "ready" && state.runs.length === 0 && (
          <p className="chain-panel__status">No chain runs found.</p>
        )}
        {state.status === "ready" &&
          state.runs.map((run) => <ChainRunStepper key={run.public_id} run={run} />)}
      </Card>
    </div>
  );
}

function ChainRunStepper({ run }: { run: ChainRun }) {
  const steps = [...run.steps].sort((a, b) => a.step_order - b.step_order);
  return (
    <div className="chain-stepper">
      <div className="chain-stepper__header">
        <span className="chain-stepper__run-id">{run.public_id}</span>
        <Badge label={run.status} />
        <span className="chain-stepper__started-by">{run.started_by ?? "—"}</span>
      </div>
      {steps.length === 0 ? (
        <p className="chain-stepper__empty">No steps recorded yet.</p>
      ) : (
        <ol className="chain-stepper__steps">
          {steps.map((step) => (
            <li key={step.step_name} className="chain-stepper__step" data-step-status={step.status}>
              <span className="chain-stepper__step-name">{step.step_name}</span>
              <span className="chain-stepper__step-status">{step.status}</span>
              {step.agent && <span className="chain-stepper__step-agent">{step.agent}</span>}
              {step.status === "skipped" && step.skip_justification && (
                <span className="chain-stepper__step-justification">{step.skip_justification}</span>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
