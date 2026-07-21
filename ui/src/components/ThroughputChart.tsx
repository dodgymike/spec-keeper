import { useMemo } from "react";
import type { Task } from "../api/types";
import "./ThroughputChart.css";

interface ThroughputChartProps {
  tasks: Task[];
}

type BucketUnit = "day" | "week";

export interface ThroughputBucket {
  /** Bucket start, ISO (UTC midnight for daily buckets, Monday for weekly). */
  key: string;
  /** Human label, e.g. "Jul 14" (day) or "wk of Jul 14" (week). */
  label: string;
  /** Tasks whose `completed_at` falls in this bucket. */
  count: number;
  /** Running total of `count` up to and including this bucket. */
  cumulative: number;
}

const MS_PER_DAY = 24 * 60 * 60 * 1000;
/** Ranges spanning more than this many days switch from daily to weekly buckets, keeping the axis legible. */
const WEEKLY_BUCKET_THRESHOLD_DAYS = 45;

/** Midnight UTC for the day containing `date` (daily bucket alignment). */
function startOfDayUtc(date: Date): Date {
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
}

/** Monday-aligned UTC week start containing `date` (weekly bucket alignment). */
function startOfWeekUtc(date: Date): Date {
  const day = startOfDayUtc(date);
  const weekday = (day.getUTCDay() + 6) % 7; // Mon=0..Sun=6
  return new Date(day.getTime() - weekday * MS_PER_DAY);
}

const DAY_LABEL_FORMAT: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };

/**
 * Buckets *completed* tasks (those with a non-null `completed_at`) by day or
 * week, picking whichever keeps the axis to a sane number of points. Tasks
 * without `completed_at` are excluded outright - they have no "done on this
 * date" fact to plot, they are not treated as zero-contribution.
 */
export function bucketiseCompletions(tasks: Task[]): { buckets: ThroughputBucket[]; unit: BucketUnit } {
  const completedDates = tasks
    .map((task) => task.completed_at)
    .filter((value): value is string => value !== null)
    .map((value) => new Date(value))
    .filter((date) => !Number.isNaN(date.getTime()))
    .sort((a, b) => a.getTime() - b.getTime());

  if (completedDates.length === 0) {
    return { buckets: [], unit: "day" };
  }

  const first = completedDates[0];
  const last = completedDates[completedDates.length - 1];
  const rangeDays = (last.getTime() - first.getTime()) / MS_PER_DAY;
  const unit: BucketUnit = rangeDays > WEEKLY_BUCKET_THRESHOLD_DAYS ? "week" : "day";
  const alignStart = unit === "day" ? startOfDayUtc : startOfWeekUtc;
  const step = unit === "day" ? MS_PER_DAY : MS_PER_DAY * 7;

  const firstBucketStart = alignStart(first).getTime();
  const lastBucketStart = alignStart(last).getTime();

  const counts = new Map<number, number>();
  for (const date of completedDates) {
    const key = alignStart(date).getTime();
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }

  const buckets: ThroughputBucket[] = [];
  let cumulative = 0;
  for (let t = firstBucketStart; t <= lastBucketStart; t += step) {
    const count = counts.get(t) ?? 0;
    cumulative += count;
    const bucketDate = new Date(t);
    const label =
      unit === "day"
        ? bucketDate.toLocaleDateString(undefined, DAY_LABEL_FORMAT)
        : `wk of ${bucketDate.toLocaleDateString(undefined, DAY_LABEL_FORMAT)}`;
    buckets.push({ key: bucketDate.toISOString(), label, count, cumulative });
  }
  return { buckets, unit };
}

const CHART_WIDTH = 640;
const CHART_HEIGHT = 220;
const PADDING_LEFT = 28;
const PADDING_RIGHT = 12;
const PADDING_TOP = 14;
const PADDING_BOTTOM = 26;

/**
 * Throughput: a bar per bucket (tasks completed in that day/week) plus an
 * overlaid line for the cumulative total done-over-time. Hand-rolled SVG -
 * geometry lives in x/y/width/height/points attributes (fine under CSP),
 * colour lives entirely in ThroughputChart.css via className (no inline
 * `style=`, no charting library).
 */
export function ThroughputChart({ tasks }: ThroughputChartProps) {
  const { buckets, unit } = useMemo(() => bucketiseCompletions(tasks), [tasks]);

  if (buckets.length === 0) {
    return (
      <div className="throughput-chart throughput-chart--empty">
        <p>Not enough data yet. No tasks have a `completed_at` timestamp.</p>
      </div>
    );
  }

  const totalCompleted = buckets[buckets.length - 1].cumulative;
  const unitLabel = unit === "day" ? "day" : "week";

  const plotWidth = CHART_WIDTH - PADDING_LEFT - PADDING_RIGHT;
  const plotHeight = CHART_HEIGHT - PADDING_TOP - PADDING_BOTTOM;
  const maxCount = Math.max(1, ...buckets.map((bucket) => bucket.count));
  const maxCumulative = Math.max(1, totalCompleted);

  const bandWidth = plotWidth / buckets.length;
  const barWidth = Math.max(2, bandWidth * 0.6);

  const barX = (index: number) => PADDING_LEFT + index * bandWidth + (bandWidth - barWidth) / 2;
  const barY = (count: number) => PADDING_TOP + plotHeight - (count / maxCount) * plotHeight;
  const barHeight = (count: number) => (count / maxCount) * plotHeight;

  const linePoints = buckets
    .map((bucket, index) => {
      const x = PADDING_LEFT + index * bandWidth + bandWidth / 2;
      const y = PADDING_TOP + plotHeight - (bucket.cumulative / maxCumulative) * plotHeight;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  // Label every Nth bucket so the x-axis stays legible when there are many buckets.
  const labelEvery = Math.max(1, Math.ceil(buckets.length / 8));

  const summary =
    `Bar chart of tasks completed per ${unitLabel}, from ${buckets[0].label} to ` +
    `${buckets[buckets.length - 1].label}: ${totalCompleted} tasks total. ` +
    `A line overlay tracks the cumulative count done over time, rising to ${totalCompleted}.`;

  return (
    <figure className="throughput-chart">
      <svg
        className="throughput-chart__svg"
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        role="img"
        aria-labelledby="throughput-chart-title throughput-chart-desc"
      >
        <title id="throughput-chart-title">Throughput: tasks completed per {unitLabel}</title>
        <desc id="throughput-chart-desc">{summary}</desc>

        <line
          className="throughput-chart__axis"
          x1={PADDING_LEFT}
          y1={PADDING_TOP + plotHeight}
          x2={CHART_WIDTH - PADDING_RIGHT}
          y2={PADDING_TOP + plotHeight}
        />

        {buckets.map((bucket, index) => (
          <rect
            key={bucket.key}
            className="throughput-chart__bar"
            x={barX(index)}
            y={barY(bucket.count)}
            width={barWidth}
            height={Math.max(0, barHeight(bucket.count))}
          />
        ))}

        <polyline className="throughput-chart__line" points={linePoints} />

        {buckets.map((bucket, index) =>
          index % labelEvery === 0 ? (
            <text
              key={bucket.key}
              className="throughput-chart__x-label"
              x={PADDING_LEFT + index * bandWidth + bandWidth / 2}
              y={CHART_HEIGHT - 8}
            >
              {bucket.label}
            </text>
          ) : null
        )}

        <text className="throughput-chart__y-label" x={2} y={PADDING_TOP + 6}>
          {maxCount}
        </text>
        <text className="throughput-chart__y-label" x={2} y={PADDING_TOP + plotHeight}>
          0
        </text>
      </svg>

      <figcaption className="throughput-chart__caption">
        How to read this: bars are tasks completed per {unitLabel} (left axis, max {maxCount} per {unitLabel}).
        The line is the cumulative total done over time (its own scale, ending at {totalCompleted}). Tasks with
        no `completed_at` timestamp are not counted anywhere in this chart.
      </figcaption>

      <table className="sr-only throughput-chart__table">
        <caption>Tasks completed per {unitLabel}</caption>
        <thead>
          <tr>
            <th scope="col">{unit === "day" ? "Day" : "Week of"}</th>
            <th scope="col">Completed that {unitLabel}</th>
            <th scope="col">Cumulative done</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((bucket) => (
            <tr key={bucket.key}>
              <td>{bucket.label}</td>
              <td>{bucket.count}</td>
              <td>{bucket.cumulative}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
