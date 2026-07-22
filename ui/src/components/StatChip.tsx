import type { TaskStatus } from "../api/types";
import "./StatChip.css";

interface StatChipProps {
  label: string;
  value: string | number;
  /**
   * When set, colours the chip's value via the `[data-status]` CSS
   * attribute selector (see StatChip.css) - never via inline styles (CSP:
   * no `style-src 'unsafe-inline'`). Mirrors `Badge`'s `status` prop.
   */
  status?: TaskStatus;
}

/** A compact "label: value" chip, e.g. task/epic counts on a project card. */
export function StatChip({ label, value, status }: StatChipProps) {
  return (
    <span className={status ? "stat-chip stat-chip--status" : "stat-chip"} data-status={status}>
      <span className="stat-chip__value">{value}</span>
      <span className="stat-chip__label">{label}</span>
    </span>
  );
}
