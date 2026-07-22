import { useMemo } from "react";
import type { Epic, Task, TaskStatus } from "../api/types";
import "./EpicProgressChart.css";

interface EpicProgressChartProps {
  epics: Epic[];
  tasks: Task[];
}

/** Which of the four status-colour buckets a raw TaskStatus rolls up into. */
const STATUS_GROUP: Record<TaskStatus, "done" | "in_progress" | "todo" | "other"> = {
  todo: "todo",
  in_progress: "in_progress",
  blocked: "other",
  deferred: "other",
  done: "done",
  superseded: "other",
  cancelled: "other",
};

/** Stacking order, left to right - "done" fills first so the bar reads like a progress bar. */
const STATUS_ORDER = ["done", "in_progress", "todo", "other"] as const;

export interface EpicProgressRow {
  /** Epic key, or "—" for the synthetic "no epic" bucket. */
  key: string;
  title: string;
  /** Sort position; epics not present in `epics` (shouldn't happen) sort last, before "no epic". */
  position: number;
  done: number;
  in_progress: number;
  todo: number;
  other: number;
  total: number;
  /** 0-100, or null when `total` is 0 (nothing to compute a percentage of). */
  donePct: number | null;
}

/**
 * Builds one row per epic (done/in_progress/todo/other counts), plus a
 * trailing "No epic" row for tasks with a null/unrecognised `epic_key` - so
 * unassigned work stays visible rather than silently dropped. Sorted by
 * completion percentage (desc), ties by epic position; "No epic" is always
 * last since it has no meaningful position.
 */
export function buildEpicProgressRows(epics: Epic[], tasks: Task[]): EpicProgressRow[] {
  const byEpicKey = new Map<string, Task[]>();
  const unassigned: Task[] = [];
  for (const task of tasks) {
    if (task.epic_key === null) {
      unassigned.push(task);
      continue;
    }
    const bucket = byEpicKey.get(task.epic_key);
    if (bucket) {
      bucket.push(task);
    } else {
      byEpicKey.set(task.epic_key, [task]);
    }
  }

  function toRow(key: string, title: string, position: number, epicTasks: Task[]): EpicProgressRow {
    const counts = { done: 0, in_progress: 0, todo: 0, other: 0 };
    for (const task of epicTasks) {
      counts[STATUS_GROUP[task.status]] += 1;
    }
    const total = epicTasks.length;
    const donePct = total === 0 ? null : (counts.done / total) * 100;
    return { key, title, position, ...counts, total, donePct };
  }

  const rows: EpicProgressRow[] = [];
  const seenKeys = new Set<string>();
  for (const epic of epics) {
    seenKeys.add(epic.key);
    rows.push(toRow(epic.key, epic.title, epic.position, byEpicKey.get(epic.key) ?? []));
  }
  // Tasks referencing an epic key the project doesn't have (shouldn't normally happen)
  // still get a row rather than being silently excluded from the chart.
  for (const [key, epicTasks] of byEpicKey) {
    if (!seenKeys.has(key)) {
      rows.push(toRow(key, key, Number.POSITIVE_INFINITY, epicTasks));
    }
  }

  rows.sort((a, b) => (b.donePct ?? -1) - (a.donePct ?? -1) || a.position - b.position);

  if (unassigned.length > 0) {
    rows.push(toRow("—", "No epic", Number.POSITIVE_INFINITY, unassigned));
  }

  return rows;
}

const CHART_WIDTH = 640;
const LEFT_LABEL_WIDTH = 90;
const RIGHT_LABEL_WIDTH = 150;
const PADDING = 8;
const ROW_HEIGHT = 26;
const BAR_HEIGHT = 12;
const TOP_PADDING = 6;

const BAR_X = PADDING + LEFT_LABEL_WIDTH;
const BAR_WIDTH = CHART_WIDTH - PADDING * 2 - LEFT_LABEL_WIDTH - RIGHT_LABEL_WIDTH;

/**
 * Per-epic progress: one horizontal stacked bar per epic (done / in_progress
 * / todo / other, reusing the app's status colour tokens), sorted by
 * completion. Hand-rolled SVG, no charting library - see ThroughputChart for
 * the same CSP-clean approach (geometry via attributes, colour via CSS).
 */
export function EpicProgressChart({ epics, tasks }: EpicProgressChartProps) {
  const rows = useMemo(() => buildEpicProgressRows(epics, tasks), [epics, tasks]);

  if (rows.length === 0) {
    return (
      <div className="epic-progress-chart epic-progress-chart--empty">
        <p>Not enough data yet. No epics or tasks to summarise.</p>
      </div>
    );
  }

  const chartHeight = TOP_PADDING * 2 + rows.length * ROW_HEIGHT;
  const doneTotal = rows.reduce((sum, row) => sum + row.done, 0);
  const taskTotal = rows.reduce((sum, row) => sum + row.total, 0);
  const overallPct = taskTotal === 0 ? 0 : Math.round((doneTotal / taskTotal) * 100);

  const summary =
    `Stacked bar per epic showing done, in progress, todo, and other task counts, sorted by ` +
    `completion. Overall: ${doneTotal} of ${taskTotal} tasks done (${overallPct}%) across ${rows.length} ` +
    `epic${rows.length === 1 ? "" : "s"} (including "No epic" for unassigned tasks, if any).`;

  return (
    <figure className="epic-progress-chart">
      <svg
        className="epic-progress-chart__svg"
        viewBox={`0 0 ${CHART_WIDTH} ${chartHeight}`}
        role="img"
        aria-labelledby="epic-progress-chart-title epic-progress-chart-desc"
      >
        <title id="epic-progress-chart-title">Per-epic progress</title>
        <desc id="epic-progress-chart-desc">{summary}</desc>

        {rows.map((row, index) => {
          const rowY = TOP_PADDING + index * ROW_HEIGHT;
          const barY = rowY + (ROW_HEIGHT - BAR_HEIGHT) / 2;
          const textY = rowY + ROW_HEIGHT / 2 + 4;

          let cursorX = BAR_X;
          const segments = row.total === 0 ? [] : STATUS_ORDER.map((status) => {
            const count = row[status];
            const width = (count / row.total) * BAR_WIDTH;
            const segment = { status, count, x: cursorX, width };
            cursorX += width;
            return segment;
          });

          const pctLabel = row.donePct === null ? "no tasks" : `${row.done}/${row.total} (${Math.round(row.donePct)}%)`;

          return (
            <g key={row.key} className="epic-progress-chart__row">
              <text className="epic-progress-chart__row-label" x={PADDING} y={textY}>
                {row.key}
              </text>
              <rect
                className="epic-progress-chart__track"
                x={BAR_X}
                y={barY}
                width={BAR_WIDTH}
                height={BAR_HEIGHT}
              />
              {segments
                .filter((segment) => segment.width > 0)
                .map((segment) => (
                  <rect
                    key={segment.status}
                    className={`epic-progress-chart__segment epic-progress-chart__segment--${segment.status}`}
                    x={segment.x}
                    y={barY}
                    width={segment.width}
                    height={BAR_HEIGHT}
                  />
                ))}
              <text
                className="epic-progress-chart__row-pct"
                x={BAR_X + BAR_WIDTH + 8}
                y={textY}
              >
                {pctLabel}
              </text>
            </g>
          );
        })}
      </svg>

      <figcaption className="epic-progress-chart__caption">
        How to read this: each row is one epic's tasks, stacked done → in progress → todo → other (left to
        right), coloured to match status badges elsewhere in the app. The label on the right is done/total
        and percent done.
      </figcaption>

      <table className="sr-only epic-progress-chart__table">
        <caption>Per-epic progress</caption>
        <thead>
          <tr>
            <th scope="col">Epic</th>
            <th scope="col">Done</th>
            <th scope="col">In progress</th>
            <th scope="col">To do</th>
            <th scope="col">Other</th>
            <th scope="col">Total</th>
            <th scope="col">% done</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key}>
              <td>
                {row.key === "—" ? row.title : `${row.key} – ${row.title}`}
              </td>
              <td>{row.done}</td>
              <td>{row.in_progress}</td>
              <td>{row.todo}</td>
              <td>{row.other}</td>
              <td>{row.total}</td>
              <td>{row.donePct === null ? "—" : `${Math.round(row.donePct)}%`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
