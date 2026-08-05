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
  login_timeout: "The broker did not respond in time. Please try again.",
  server_not_found: "That server could not be found. Please check the server name.",
  server_unavailable: "The broker server is temporarily unavailable. Please try again shortly.",
  account_disabled: "This account appears to be disabled at the broker.",
  could_not_verify: "We could not verify the connection. Please try again.",
  mt5_unavailable: "The validation service is temporarily unavailable. Please try again shortly.",
  bridge_unavailable: "The validation service is temporarily unavailable. Please try again shortly.",
  runtime_unavailable: "The validation service is temporarily unavailable. Please try again shortly.",
  // Service-side / not-yet-provisioned checks — the check never reached the broker, so the customer's
  // details are NOT implicated. Say so, and confirm nothing was changed (packet WS-H). ``verified`` is the
  // agent's success taxonomy alongside ``demo_ok``.
  validation_unconfigured:
    "We couldn't test the connection because broker validation isn't available for your account yet. " +
    "Your account details weren't changed — there's nothing to fix on your side. Please check back later.",
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
  const msg = err instanceof Error ? err.message : String(err ?? "");
  if (!msg) return fallback;
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
