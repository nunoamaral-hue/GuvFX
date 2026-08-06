/** Minimum-hardening WS-G — the SINGLE mapping from validation-agent readiness (band / state) to UI views,
 * mirroring operations-status.ts / broker-status.ts: components render a StatusView, never a raw backend
 * enum. Two audiences are kept strictly apart here so the separation cannot drift:
 *   - operatorAgentView(): full operational detail for the staff Ops surface.
 *   - customerAgentView(): a neutral availability sentence ONLY — never a reason code, host detail,
 *     supervision flag, alert or correlation id, and never phrased to imply the customer is at fault.
 */
import type { StatusView } from "@/lib/broker-status";

/** Operator-facing DTO (matches backend agent_status_presenter.present_operator). */
export interface OperatorAgentStatus {
  state: string;
  band: string;
  supervised: boolean | null;
  reason: string;
  alerts: { name: string; severity: string; detects_state: string; runbook: string; detail?: string }[];
  rates?: Record<string, number>;
  correlation_id?: string;
}

/** Customer-facing DTO (matches backend present_customer) — availability + one neutral sentence. */
export interface CustomerAgentStatus {
  available: boolean;
  status: string;
  message: string;
}

const UNKNOWN: StatusView = { label: "Unknown", color: "gray" };

/** Coarse band → HEALTHY(green) / DEGRADED(yellow) / UNAVAILABLE(red). Operator only. */
export function operatorAgentView(band: string | null | undefined): StatusView {
  switch ((band || "").toUpperCase()) {
    case "HEALTHY": return { label: "Healthy", color: "green" };
    case "DEGRADED": return { label: "Degraded", color: "yellow" };
    case "UNAVAILABLE": return { label: "Unavailable", color: "red" };
    default: return UNKNOWN;
  }
}

/** Operator-only fine-state label (never shown to a customer). */
const _STATE_LABEL: Record<string, string> = {
  HEALTHY: "Healthy",
  READY_UNARMED: "Up — not armed",
  SUPERVISION_UNKNOWN: "Supervision unknown",
  UNSUPERVISED: "Unsupervised listener",
  INCOMPATIBLE: "Contract mismatch",
  LISTENING_NO_NEGOTIATE: "Listening — not negotiating",
  UNREACHABLE: "Unreachable",
  UNCONFIGURED: "Probe unconfigured",
};
export function operatorStateLabel(state: string | null | undefined): string {
  return _STATE_LABEL[(state || "").toUpperCase()] || "Unknown";
}

/** severity → colour, reusing the operations palette (INFO/WARNING/ERROR/CRITICAL + HIGH/MEDIUM/LOW). */
export function alertSeverityView(sev: string | null | undefined): StatusView {
  switch ((sev || "").toUpperCase()) {
    case "HIGH": case "CRITICAL": case "ERROR": return { label: sev!.toUpperCase(), color: "red" };
    case "MEDIUM": case "WARNING": return { label: sev!.toUpperCase(), color: "yellow" };
    default: return { label: (sev || "INFO").toUpperCase(), color: "blue" };
  }
}

/** Customer-facing availability view — the ONLY thing a customer sees. No internal vocabulary. */
export function customerAgentView(s: CustomerAgentStatus | null | undefined): StatusView {
  if (s?.available) return { label: "Available", color: "green" };
  return { label: "Temporarily unavailable", color: "yellow" };
}
