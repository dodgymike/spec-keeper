import "./StatChip.css";

interface StatChipProps {
  label: string;
  value: string | number;
}

/** A compact "label: value" chip, e.g. task/epic counts on a project card. */
export function StatChip({ label, value }: StatChipProps) {
  return (
    <span className="stat-chip">
      <span className="stat-chip__value">{value}</span>
      <span className="stat-chip__label">{label}</span>
    </span>
  );
}
