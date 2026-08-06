/** WP4.2 (ADR-0031) — the SINGLE mapping from backend status/reason codes to customer-facing UI.
 * Components never hardcode backend enum strings; they render these views. Reason codes are mapped to
 * customer-safe wording (unknown → a generic message) so operator diagnostics never surface. */

export type BadgeColor = "green" | "gray" | "blue" | "red" | "yellow";
export type StatusView = { label: string; color: BadgeColor };

const UNKNOWN: StatusView = { label: "Unknown", color: "gray" };

/** validation_status (NEVER / VALIDATED / CONNECTION_FAILED / TECHNICAL_ERROR). */
const VALIDATION: Record<string, StatusView> = {
  VALIDATED: { label: "Validated", color: "green" },
  CONNECTION_FAILED: { label: "Connection failed", color: "red" },
  TECHNICAL_ERROR: { label: "Temporarily unavailable", color: "yellow" },
  NEVER: { label: "Not validated", color: "gray" },
};
export function validationStatusView(s: string | null | undefined): StatusView {
  return VALIDATION[String(s ?? "")] ?? UNKNOWN;
}

/** Broker health from the latest attempt status (HEALTHY / NEEDS_ATTENTION / UNAVAILABLE / UNKNOWN). */
const HEALTH: Record<string, StatusView> = {
  HEALTHY: { label: "Connected & ready", color: "green" },
  NEEDS_ATTENTION: { label: "Needs attention", color: "yellow" },
  UNAVAILABLE: { label: "Unavailable", color: "red" },
  UNKNOWN,
};
export function healthStatusView(s: string | null | undefined): StatusView {
  return HEALTH[String(s ?? "UNKNOWN")] ?? UNKNOWN;
}

/** Connection state from is_active + disconnected_at. */
export function connectionView(isActive: boolean, disconnectedAt: string | null | undefined): StatusView {
  if (disconnectedAt) return { label: "Disconnected", color: "gray" };
  return isActive ? { label: "Connected", color: "green" } : { label: "Inactive", color: "gray" };
}

/** reason_code → customer-safe message. Unknown codes fall back to a generic line — never the raw code. */
const REASON: Record<string, string> = {
  demo_ok: "Demo account verified.",
  is_demo: "Demo account verified.",
  live_detected: "This looks like a live account — the beta supports demo accounts only.",
  classification_mismatch: "The account type did not match what was expected.",
  invalid_password: "The password was not accepted. Please check it and try again.",
  invalid_login: "The login was not accepted. Please check your account number.",
  // Phase-4 WS-C: login_timeout is TRANSPORT-ambiguous — it comes from a backend->agent channel timeout (the
  // request may never have reached the agent, let alone the broker) or an MT5-side timeout with no IPC/broker
  // evidence. It must NOT be attributed to the broker ("the broker didn't respond" wrongly implies the broker
  // was reached and is at fault). Neutral, broker-agnostic wording only.
  login_timeout: "The connection check didn't complete in time. Please try again.",
  // Phase-4 WS-C: the validation service was momentarily busy handling another check (agent single-flight
  // lock). Not a broker fault, not the customer's details — a brief, retryable service condition.
  validation_busy: "The validation service is busy right now. Please try again in a moment.",
  server_not_found: "That server could not be found. Please check the server name.",
  server_unavailable: "The broker server is temporarily unavailable. Please try again shortly.",
  account_disabled: "This account appears to be disabled at the broker.",
  could_not_verify: "We could not verify the connection. Please try again.",
  mt5_unavailable: "The validation service is temporarily unavailable. Please try again shortly.",
  bridge_unavailable: "The validation service is temporarily unavailable. Please try again shortly.",
  runtime_unavailable: "The validation service is temporarily unavailable. Please try again shortly.",
  // WS-C (2026-08-05) — transport-layer timeouts. The check never reached the broker or a login, so the
  // wording names NEITHER (no "broker", "login" or "server"). unreachable = couldn't open a connection to the
  // validation service; timeout = the service accepted the connection but didn't answer in time.
  validation_agent_unreachable:
    "The validation service couldn't be reached. Your details weren't rejected. Please try again later.",
  validation_agent_timeout:
    "The validation service took too long to respond. Your details weren't rejected. Please try again later.",
  // Service-side / not-yet-provisioned checks — the check never reached the broker, so the customer's
  // details are NOT implicated. Say so, and confirm nothing was changed (packet WS-H). ``verified`` is the
  // agent's success taxonomy alongside ``demo_ok``.
  validation_unconfigured:
    "We couldn't test the connection because broker validation isn't available for your account yet. " +
    "Your account details weren't changed — there's nothing to fix on your side. Please check back later.",
  // WS-A (2026-08-05): the validation host couldn't start the secure check (a local/host condition that never
  // reached the broker). It is NOT a broker outage and NOT a credential rejection. Customer-safe wording only —
  // no IPC / session / MT5 / error-code / host detail — and it must not read as "the broker is down".
  validation_ipc_unavailable:
    "We couldn't start the secure broker-validation session. Your broker details weren't rejected. " +
    "Please try again later, or contact support if this continues.",
  credential_missing:
    "We don't have a saved password for this account. Add or replace your credentials, then try again.",
  broker_server_missing: "No broker server is set for this account. Please reconnect the account.",
  verified: "Connection verified.",
};
export function reasonMessage(code: string | null | undefined): string {
  const c = String(code ?? "").trim();
  if (!c) return "";
  // Unknown code → a NEUTRAL line. Never the accusatory "check your details": an unmapped code is almost
  // always a technical/service reason, and blaming the customer's details for a server-side failure is the
  // exact defect this replaces (packet WS-H). Known user-fixable codes carry their own "check …" wording.
  return REASON[c] ?? "We couldn't complete the connection check. Please try again shortly.";
}

/** The next action a customer can take after a FAILED validation, so the result modal is never a dead end
 * (never "just dismiss"). Only genuinely-actionable, non-misleading affordances are returned — the modal
 * always offers Close in addition:
 *   - "replace" — a credential the customer can fix here (wrong/absent password).
 *   - "retry"   — a transient / service-side hiccup where trying again is reasonable (also the unknown-code
 *                 default, and the transport-failure default).
 *   - support-only outcomes (nothing the customer can change: not provisioned, disabled at the broker,
 *     wrong account number with no in-place edit, live account in a demo beta) return [] — the message
 *     itself carries the "please contact support / check back later" guidance, and no button pretends the
 *     customer can fix it. */
export type ValidationActionKind = "retry" | "replace";

const REPLACE_CODES = new Set(["invalid_password", "credential_missing"]);
const SUPPORT_ONLY_CODES = new Set([
  "validation_unconfigured", "broker_server_missing", "server_not_found", "account_disabled",
  "live_detected", "classification_mismatch", "invalid_login",
  // WS-A (2026-08-05): a validation-host/IPC failure takes a long time and holds the single-flight validator;
  // an immediate "Try again" would just queue another slow, self-colliding attempt (a retry storm). Offer
  // Close + "try again later"/support guidance instead — no in-modal retry button.
  "validation_ipc_unavailable",
]);

export function validationActions(reasonCode: string | null | undefined): ValidationActionKind[] {
  const c = String(reasonCode ?? "").trim();
  if (REPLACE_CODES.has(c)) return ["replace"];
  if (SUPPORT_ONLY_CODES.has(c)) return [];
  // Transient (login_timeout / *_unavailable / could_not_verify) AND any unknown code → retrying is safe.
  return ["retry"];
}

/** WS-C (2026-08-05) — "Current validation state" and "Latest validation attempt" are DIFFERENT concepts and
 * must be shown separately: a durable VALIDATED state (last success) is preserved even when a later attempt
 * fails for a transient/host reason. `latestAttemptLine` renders the most-recent attempt as its own line with
 * a CONCISE, customer-safe outcome — never a broker-outage claim for a host/agent failure, never internals. */
const REASON_SHORT: Record<string, string> = {
  demo_ok: "Verified", is_demo: "Verified", verified: "Verified",
  live_detected: "Live account (demo required)",
  validation_ipc_unavailable: "Couldn't start the validation session",
  validation_busy: "Validation busy — try again shortly",           // Phase-4 WS-C (concept 6)
  validation_unconfigured: "Validation not available yet",
  // Phase-4 WS-C (S14): the "validation service temporarily unavailable" family had long-form messages but
  // no short outcome, so the compact "Latest attempt" line / history column fell back to the generic label.
  mt5_unavailable: "Validation temporarily unavailable",
  bridge_unavailable: "Validation temporarily unavailable",
  runtime_unavailable: "Validation temporarily unavailable",
  validation_agent_unreachable: "Validation service unreachable",   // WS-C: transport connect timeout
  validation_agent_timeout: "Validation service didn't respond",    // WS-C: transport read timeout
  could_not_verify: "Couldn't verify — try again",
  credential_missing: "No saved password",
  broker_server_missing: "No broker server set",
  invalid_password: "Password not accepted",
  invalid_login: "Login not accepted",
  account_disabled: "Account disabled at broker",
  server_not_found: "Server not found",
  server_unavailable: "Broker temporarily unavailable",
  // Phase-4 WS-C: neutral — login_timeout is transport-ambiguous, never a proven broker fault.
  login_timeout: "Didn't complete in time",
  classification_mismatch: "Account type mismatch",
};
export function reasonShort(code: string | null | undefined): string {
  const c = String(code ?? "").trim();
  if (!c) return "Couldn't complete the check";
  return REASON_SHORT[c] ?? "Couldn't complete the check";
}

/** The most-recent attempt as a distinct one-liner: "Latest attempt: <when> — <outcome>". Empty when there
 * is no attempt yet. HEALTHY reads "Verified"; every other outcome uses the concise, safe `reasonShort`.
 * Cross-checked against the current validation_status (like `lastValidatedLine`): a stale prior-success
 * attempt must NOT render "Latest attempt: … — Verified" under a "Not validated" badge (e.g. after a
 * disconnect resets validation_status to NEVER while the old HEALTHY attempt row remains the latest). */
export function latestAttemptLine(
  attempt: { status?: string; reason_code?: string; created_at?: string } | null | undefined,
  validationStatus?: string | null,
): string {
  if (!attempt || !attempt.created_at) return "";
  const healthy = attempt.status === "HEALTHY";
  if (healthy && String(validationStatus ?? "") === "NEVER") return "";  // would contradict the badge
  const when = formatWhen(attempt.created_at);
  const outcome = healthy ? "Verified" : reasonShort(attempt.reason_code);
  return `Latest attempt: ${when} — ${outcome}`;
}

/** The "last validated" line, cross-checked against validation_status. A stale validated_at can outlive
 * the status that produced it — e.g. disconnect resets validation_status to NEVER but does not clear
 * validated_at — which would otherwise render "Last validated <T1>" directly under a "Not validated"
 * badge. Suppress the timestamp whenever the account is in the explicit never-validated state. */
export function lastValidatedLine(
  validationStatus: string | null | undefined, validatedAtIso: string | null | undefined,
): string {
  const when = formatWhen(validatedAtIso);
  if (when && String(validationStatus ?? "") !== "NEVER") return `Last validated ${when}`;
  return "No successful validation yet";
}

/** Mask an account number to the last 4 digits. */
export function maskAccountNumber(n: string | null | undefined): string {
  const s = String(n ?? "").trim();
  if (!s) return "";
  if (s.length <= 4) return "••" + s;
  return "••••" + s.slice(-4);
}

/** Turn a caught error into customer-safe wording. `apiFetch` throws the DRF `detail` (already
 * customer-safe) for most errors, but for field-shaped validation errors it throws `JSON.stringify(obj)`
 * — this flattens that into the plain validation sentences and never shows the customer a raw JSON blob
 * (or, if extraction yields nothing, a generic fallback). */
export function toCustomerError(err: unknown, fallback = "Something went wrong. Please try again."): string {
  // Transport-level failures (dropped/reset/aborted connection, DNS, CORS, a gunicorn-killed request) are
  // tagged `kind: "network"` by apiFetch. NEVER show the raw "Failed to fetch"/TypeError text to a customer.
  if ((err as { kind?: string } | null)?.kind === "network") {
    return "We couldn't reach the validation service. Your details weren't changed — please check your connection and try again shortly.";
  }
  const msg = err instanceof Error ? err.message : String(err ?? "");
  if (!msg) return fallback;
  // Belt-and-braces: if a raw JS/transport exception reaches here untagged, do NOT surface it. DRF `detail`
  // strings are full sentences and pass through; these exception shapes never do.
  if (/^(network_unreachable|Failed to fetch|NetworkError|TypeError|Load failed|AbortError|The user aborted|The operation was aborted|Unexpected token|JSON\.parse|Request failed: \d)/i.test(msg)
      || /(\.tsx?:\d+|\.js:\d+|\n\s+at\s)/.test(msg)) {
    return fallback;
  }
  if (msg.startsWith("{") || msg.startsWith("[")) {
    try {
      const parts: string[] = [];
      const collect = (v: unknown): void => {
        if (typeof v === "string") parts.push(v);
        else if (Array.isArray(v)) v.forEach(collect);
        else if (v && typeof v === "object") Object.values(v).forEach(collect);
      };
      collect(JSON.parse(msg));
      const cleaned = parts.map((s) => s.trim()).filter(Boolean).join(" ");
      return cleaned || fallback;
    } catch {
      return fallback;
    }
  }
  return msg;
}

/** Format an ISO timestamp for display; empty string when absent/invalid. */
export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}
