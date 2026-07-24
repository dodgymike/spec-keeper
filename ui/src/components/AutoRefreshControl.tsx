import {
  AUTO_REFRESH_OPTIONS,
  useAutoRefreshPreference,
  type AutoRefreshPreference,
} from "../hooks/autoRefresh";
import "./AutoRefreshControl.css";

/** Serialises a preference to a stable <option> value ("" == Default/null). */
function toOptionValue(pref: AutoRefreshPreference): string {
  return pref === null ? "" : String(pref);
}

/** Parses an <option> value back to a preference ("" -> null, else ms). */
function fromOptionValue(raw: string): AutoRefreshPreference {
  return raw === "" ? null : Number(raw);
}

/**
 * Header control that sets the dashboard's background auto-refresh cadence
 * (persisted via `useAutoRefreshPreference`). "Off" stops all background polling;
 * every page's manual Refresh button still fetches on demand.
 */
export function AutoRefreshControl() {
  const { preference, setPreference } = useAutoRefreshPreference();
  return (
    <label className="auto-refresh-control">
      <span className="auto-refresh-control__label">Auto-refresh</span>
      <select
        className="auto-refresh-control__select"
        value={toOptionValue(preference)}
        onChange={(event) => setPreference(fromOptionValue(event.target.value))}
      >
        {AUTO_REFRESH_OPTIONS.map((option) => (
          <option key={option.label} value={toOptionValue(option.value)}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
