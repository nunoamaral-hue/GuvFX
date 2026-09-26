/** WP4.2 (ADR-0031) — Broker Accounts UI types. Mirror the merged WP1A backend serializers
 * (trading.serializers). Secret-safe: no password/ciphertext ever crosses this boundary. */

export type ValidationStatus = "NEVER" | "VALIDATED" | "CONNECTION_FAILED" | "TECHNICAL_ERROR";
export type AttemptStatus = "HEALTHY" | "NEEDS_ATTENTION" | "UNAVAILABLE";

/** GET /api/trading/accounts/ (TradingAccountSerializer). */
export type BrokerAccount = {
  id: number;
  name: string;
  broker_name: string;
  broker_display_name?: string | null;
  server_name?: string | null;
  broker_server?: number | null;
  account_number: string;
  /** Phase C4 — last-4 masked number the customer UI shows (raw `account_number` stays for internal use). */
  masked_account_number?: string | null;
  /** Phase C4 — count of ACTIVE StrategyAssignments on this account (N:M), for the "strategies" badge. */
  active_strategy_count?: number;
  /** Phase C4 — Provider-A vs Provider-B (persistent workspace) readiness provider; presentation only. */
  readiness_provider?: string | null;
  /** Hosted (Provider-B/beta) accounts have no shared MT5 instance; the runtime IS the terminal. */
  mt5_instance?: number | null;
  runtime_ready?: boolean;
  runtime_state?: string | null;
  is_demo: boolean;
  is_active: boolean;
  /** Phase C4 — customer-supplied public Myfxbook page link + optional system id (NO credentials, ever). */
  myfxbook_url?: string | null;
  myfxbook_system_id?: string | null;
  myfxbook_enabled?: boolean;
  created_at?: string;
  updated_at?: string;
};

/** GET /api/trading/accounts/entitlement-summary/ — Phase C4 read-only header summary.
 * `account_mode` "standard" ⇒ only one account can trade; "concurrent" ⇒ up to `concurrent_limit` active. */
export type EntitlementSummary = {
  account_mode: string;      // "standard" | "concurrent"
  active_count: number;
  concurrent_limit: number;
  owned_count: number;
  owned_limit: number;
};

/** POST /api/mt5/desktop-link/ (account-explicit in C4). `url` is null when the viewer isn't applicable. */
export type DesktopLinkResult = { url: string | null; available?: boolean; detail?: string };

/** GET /api/hosted-workspace/delivery-state/?account_id= — the AUTHORITATIVE per-account hosted delivery
 * signal. `deliverable` = an owner "Open MetaTrader" mint would succeed right now (availability, NOT
 * connection) — the correct gate for the Open MT5 button. `delivery_state` is the session/broker lifecycle
 * (e.g. "NONE"/"CONNECTED"). These are DISTINCT: a terminal being deliverable does not imply a broker is
 * connected or trading. Owner-scoped + IDOR-safe on the backend. */
export type DeliveryStateResult = {
  account_id: number;
  deliverable: boolean;         // may Open MetaTrader now (availability) — the correct gate for the button
  connected: boolean;           // broker RemoteApp session actually up (distinct from deliverable)
  delivery_readiness: string;   // DELIVERY_PREPARING | DELIVERY_DELIVERABLE | DELIVERY_READY | ...
  delivery_state: string;       // raw session field: "NONE" | "CONNECTED" | ...
  remoteapp_ready: boolean;
  node_assigned: boolean;
  is_owner: boolean;
};

/** BrokerValidationAttemptSerializer — ADR-0027 secret-safe allow-list only. */
export type ValidationAttempt = {
  id: number;
  trigger: string;
  status: string; // AttemptStatus, but tolerate unknown values
  reason_code: string;
  retryable: boolean;
  is_demo: boolean | null;
  server: string;
  login_masked: string;
  // WS-P3: NOT sent on the customer-facing serializer (operator diagnostic only). Present only where a
  // staff-scoped source provides it (e.g. mocked staff views / the timeline endpoint).
  correlation_id?: string;
  created_at: string;
};

/** GET .../{id}/broker/status/ */
export type BrokerStatus = {
  validation_status: string; // ValidationStatus, tolerate unknown
  validated_at: string | null;
  is_active: boolean;
  disconnected_at: string | null;
  latest_attempt: ValidationAttempt | null;
};

/** POST .../{id}/broker/replace-credentials/ */
export type ReplaceCredentialsResult = {
  replaced: boolean;
  validation_invalidated: boolean;
  validation?: ValidationAttempt;
};

/** WS-D/Phase-3 — staff-only validation timeline (GET .../validation-timeline/). Operator/admin only. */
export type ValidationTimelineStage = {
  key: string;
  operator_label: string;
  customer_label: string;
  state: "ok" | "failed" | "not_reached";
  reason: string;
};
export type ValidationTimeline = {
  correlation_id: string;
  found: boolean;
  attempt_id: number | null;
  account_id: number | null;
  status: string;
  reason_code: string;
  is_demo: boolean | null;
  server: string;
  login_masked: string;
  trigger: string;
  started_at: string;
  finished_at: string;
  duration_ms: number | null;
  stages: ValidationTimelineStage[];
  customer_summary: string;
  operator_summary: string;
};

/** POST .../{id}/broker/disconnect/ */
export type DisconnectResult = {
  disconnected: boolean;
  credential_destroyed: boolean;
  row_deleted: boolean;
};
