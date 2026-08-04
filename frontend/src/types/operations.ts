/** WP5.3 (ADR-0032) — types for the WP5.1 operational-event API ({summary, timeline}). Non-secret
 * projections only: no credentials, ciphertext, host paths, or diagnostics ever cross this boundary.
 * health_state and runtime_pause have two possible shapes (keys depend on data availability), so the
 * conditional fields are optional and must be null-guarded. */

export type OpsSeverity = "INFO" | "WARNING" | "ERROR" | "CRITICAL";
export type OpsCategory =
  | "VALIDATION" | "HEALTH" | "EXECUTION" | "RUNTIME" | "CREDENTIAL" | "CONNECTIVITY" | "SYSTEM";

export type OperationalEvent = {
  id: number;
  timestamp: string | null;
  account_id: number | null;
  runtime_uuid: string;
  category: string;
  event_type: string;
  severity: string;
  status: string;
  reason_code: string;
  summary: string;
  source: string;
  correlation_id: string;
  state_version: number | null;
  actor: string;
  customer_visible: boolean;
  resolved: boolean;
  resolved_at: string | null;
  metadata: Record<string, unknown>;
};

export type ValidationState = { status: string; validated_at: string | null };
export type HealthState = {
  state: string;
  available: boolean;
  eligible?: boolean | null;
  pause_required?: boolean | null;
  reason_code?: string;
  state_version?: number | null;
  updated_at?: string | null;
};
export type RuntimePauseState = {
  paused: boolean;
  live_paused: boolean;
  reason_code?: string;
  record?: Record<string, unknown> | null;
};
export type CredentialState = { present: boolean; state: string };
export type DisconnectState = { disconnected: boolean; disconnected_at: string | null };
export type EventCounts = {
  total: number;
  open: number;
  by_severity: Record<string, number>;
  by_category: Record<string, number>;
};

export type OperationalSummary = {
  account_id: number;
  generated_at: string;
  validation_state: ValidationState;
  health_state: HealthState;
  runtime_pause: RuntimePauseState;
  credential_status: CredentialState;
  disconnect_state: DisconnectState;
  latest_validation: OperationalEvent | null;
  latest_error: OperationalEvent | null;
  latest_warning: OperationalEvent | null;
  event_counts: EventCounts;
  last_update: string | null;
};

export type OperationsResponse = { summary: OperationalSummary; timeline: OperationalEvent[] };
