// =============================================================================
// Admin Operations Console — Domain Types
// =============================================================================

// ---- RBAC ----

export type AdminRole = "super_admin" | "finance_admin" | "ops_admin";

export type AdminPermissions = {
  reconciliation: "full" | "read" | "acknowledge" | "none";
  payments: "read" | "none";
  workers: "full" | "none";
  entitlements: "full" | "none";
  execution_jobs: "full" | "read" | "none";
};

// ---- Reconciliation ----

export type ReconciliationEvent = {
  id: number;
  reconciliation_run_id: string;
  account: number;
  account_display: string;
  ticket: number;
  field_name: string;
  mt5_value: string;
  platform_value: string;
  severity: "info" | "warning" | "critical";
  resolution_status: "open" | "acknowledged" | "resolved";
  signature: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

// ---- Payment Events ----

export type PaymentEvent = {
  id: number;
  provider_name: string;
  provider_event_id: string;
  provider_event_type: string;
  subscription_reference: string;
  provider_timestamp: string | null;
  processing_status: "received" | "verified" | "duplicate" | "rejected" | "processed" | "failed";
  idempotency_key: string;
  raw_payload: Record<string, unknown>;
  user: number | null;
  user_email: string | null;
  processed_at: string | null;
  created_at: string;
};

// ---- Worker Identity ----

export type WorkerIdentity = {
  id: number;
  worker_id: string;
  status: "active" | "revoked" | "suspended";
  permission_set: string[];
  created_at: string;
  last_rotated_at: string | null;
};

export type WorkerCreateResponse = {
  id: number;
  worker_id: string;
  secret: string; // one-time only
  status: string;
  permission_set: string[];
  created_at: string;
};

export type WorkerRotateResponse = {
  worker_id: string;
  secret: string; // one-time only
  last_rotated_at: string;
};

// ---- Entitlement Overrides ----

export type EntitlementOverride = {
  id: number;
  user: number;
  user_email: string;
  capability: string;
  reason: string;
  created_by: string;
  expires_at: string;
  created_at: string;
  is_active: boolean;
};

// ---- Execution Jobs ----

export type ExecutionJob = {
  id: number;
  job_type: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  account: number | null;
  account_display: string;
  strategy: number | null;
  strategy_name: string;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  cancelled_at: string | null;
};

// ---- Paginated response wrapper ----

export type PaginatedResponse<T> = {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
};
