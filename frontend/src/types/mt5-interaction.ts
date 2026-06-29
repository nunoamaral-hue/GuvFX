/**
 * TypeScript types for Packet A — Terminal Interaction API responses.
 *
 * Mirrors the DRF serializers in backend/mt5/serializers.py.
 * All fields nullable where the backend may return null.
 */

// ─────────────────────────────────────────────────────────────────────
// MT5Session (safe subset — no launch_descriptor_snapshot, no adapter_metadata)
// ─────────────────────────────────────────────────────────────────────

export type MT5SessionSafe = {
  id: number;
  state: string; // "launching" | "connected" | "suspended" | "ended" | "failed"
  adapter_type: string;
  adapter_session_id: string | null;
  launch_issued_at: string | null;
  connected_at: string | null;
  suspended_at: string | null;
  ended_at: string | null;
  expires_at: string | null;
  last_heartbeat_at: string | null;
  failure_reason: string;
  created_at: string;
};

// ─────────────────────────────────────────────────────────────────────
// Safe LaunchDescriptor (4 approved frontend fields only)
// ─────────────────────────────────────────────────────────────────────

export type SafeLaunchDescriptor = {
  transport_type: string;
  embed_url: string;
  session_token: string;
  expiry: string | null;
};

// ─────────────────────────────────────────────────────────────────────
// InteractionSession response (launch endpoint includes launch_descriptor)
// ─────────────────────────────────────────────────────────────────────

export type InteractionSessionResponse = {
  id: number;
  state: string; // "requested" | "authorized" | "active" | "ended"
  terminal_binding_id: number;
  terminal_identifier: string;
  terminal_label: string;
  environment_type: string;
  binding_status: string;
  requested_at: string | null;
  authorized_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  expires_at: string | null;
  last_activity_at: string | null;
  terminated_reason: string;
  latest_mt5_session: MT5SessionSafe | null;
  launch_descriptor?: SafeLaunchDescriptor;
  created_at: string;
  updated_at: string;
};

// ─────────────────────────────────────────────────────────────────────
// Resumable context response
// ─────────────────────────────────────────────────────────────────────

export type ResumableContextResponse = {
  interaction_session: InteractionSessionResponse;
  can_resume: boolean;
  access_mode: string;
  launch_descriptor?: SafeLaunchDescriptor;
};

// ─────────────────────────────────────────────────────────────────────
// Terminal binding (list endpoint)
// ─────────────────────────────────────────────────────────────────────

export type TerminalBinding = {
  id: number;
  terminal_identifier: string;
  terminal_label: string;
  environment_type: string; // "live" | "demo"
  status: string; // "available" | "launching" | "active" | "suspended" | "maintenance" | "locked"
  supports_shared_view: boolean;
  terminal_node_hostname: string;
  created_at: string;
};
