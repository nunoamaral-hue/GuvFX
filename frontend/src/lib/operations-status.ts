/** WP5.3 (ADR-0032) — the SINGLE mapping from WP5.1 operational-event vocabularies (severity / category /
 * resolution / health state) to UI views. Mirrors the broker-status.ts pattern: components render a
 * StatusView, never a raw backend enum, so label/colour live in one place (no duplicated colour mapping).
 * Reuses BadgeColor/StatusView + formatWhen/toCustomerError/maskAccountNumber from broker-status. */
import type { BadgeColor, StatusView } from "@/lib/broker-status";

const UNKNOWN: StatusView = { label: "Unknown", color: "gray" };

/** severity → INFO(blue) / WARNING(yellow) / ERROR(red) / CRITICAL(red). */
export function severityView(sev: string | null | undefined): StatusView {
  switch ((sev || "").toUpperCase()) {
    case "INFO": return { label: "Info", color: "blue" };
    case "WARNING": return { label: "Warning", color: "yellow" };
    case "ERROR": return { label: "Error", color: "red" };
    case "CRITICAL": return { label: "Critical", color: "red" };
    default: return UNKNOWN;
  }
}

/** The 7 operational categories. Colour is not load-bearing (7 domains > 5 palette colours) — the label
 * carries the meaning; colours group VALIDATION/HEALTH/CONNECTIVITY as connectivity-ish. */
const _CATEGORY: Record<string, StatusView> = {
  VALIDATION: { label: "Validation", color: "blue" },
  HEALTH: { label: "Health", color: "green" },
  EXECUTION: { label: "Execution", color: "gray" },
  RUNTIME: { label: "Runtime", color: "gray" },
  CREDENTIAL: { label: "Credential", color: "yellow" },
  CONNECTIVITY: { label: "Connectivity", color: "blue" },
  SYSTEM: { label: "System", color: "gray" },
};
export function categoryView(cat: string | null | undefined): StatusView {
  return _CATEGORY[(cat || "").toUpperCase()] || UNKNOWN;
}

/** resolved → green "Resolved"; open → yellow "Open". */
export function resolutionView(resolved: boolean): StatusView {
  return resolved ? { label: "Resolved", color: "green" } : { label: "Open", color: "yellow" };
}

/** WP3 broker-health state (UNKNOWN/HEALTHY/DEGRADED/STALE/DISCONNECTED/TOMBSTONED). Distinct from the
 * broker-status attempt-status view. */
const _HEALTH_STATE: Record<string, StatusView> = {
  HEALTHY: { label: "Healthy", color: "green" },
  DEGRADED: { label: "Degraded", color: "yellow" },
  STALE: { label: "Stale", color: "yellow" },
  DISCONNECTED: { label: "Disconnected", color: "red" },
  TOMBSTONED: { label: "Disconnected", color: "gray" },
  UNKNOWN: { label: "Unknown", color: "gray" },
};
export function healthStateView(state: string | null | undefined, available = true): StatusView {
  if (!available) return { label: "Not observed", color: "gray" };
  return _HEALTH_STATE[(state || "").toUpperCase()] || UNKNOWN;
}

/** runtime pause → red "Paused" / green "Active". */
export function pauseView(paused: boolean): StatusView {
  return paused ? { label: "Paused", color: "red" } : { label: "Active", color: "green" };
}

/** credential presence → green "On file" / gray "Missing". */
export function credentialView(present: boolean): StatusView {
  return present ? { label: "On file", color: "green" } : { label: "Missing", color: "gray" };
}

/** disconnect → red "Disconnected" / green "Connected". */
export function disconnectView(disconnected: boolean): StatusView {
  return disconnected ? { label: "Disconnected", color: "red" } : { label: "Connected", color: "green" };
}

/** The severities that make an unresolved event "open" (needs attention) — INFO is informational only. */
export const OPEN_SEVERITIES = new Set(["WARNING", "ERROR", "CRITICAL"]);

/** Canonical category list for the filter dropdown (the API does not validate category server-side, so
 * only submit values from this list). */
export const CATEGORIES: OpsCategoryLiteral[] = [
  "VALIDATION", "HEALTH", "EXECUTION", "RUNTIME", "CREDENTIAL", "CONNECTIVITY", "SYSTEM",
];
export const SEVERITIES = ["INFO", "WARNING", "ERROR", "CRITICAL"] as const;
type OpsCategoryLiteral =
  | "VALIDATION" | "HEALTH" | "EXECUTION" | "RUNTIME" | "CREDENTIAL" | "CONNECTIVITY" | "SYSTEM";

// A colour type re-export so components import from one place if needed.
export type { BadgeColor };
