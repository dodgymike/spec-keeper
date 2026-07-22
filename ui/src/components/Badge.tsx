import type { TaskStatus } from "../api/types";
import "./Badge.css";

interface BadgeProps {
  /** Free-text label; used verbatim for generic badges. */
  label: string;
  /**
   * When set, colours the pill via the `[data-status]` CSS attribute
   * selector (see Badge.css) - never via inline styles (CSP: no
   * `style-src 'unsafe-inline'`).
   */
  status?: TaskStatus;
}

/** A small status pill, e.g. task status or priority. */
export function Badge({ label, status }: BadgeProps) {
  return (
    <span
      className={status ? "badge badge--status" : "badge"}
      data-status={status}
    >
      {label}
    </span>
  );
}
