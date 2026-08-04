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
  is_demo: boolean;
  is_active: boolean;
  created_at?: string;
  updated_at?: string;
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
  correlation_id: string;
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

/** POST .../{id}/broker/disconnect/ */
export type DisconnectResult = {
  disconnected: boolean;
  credential_destroyed: boolean;
  row_deleted: boolean;
};
