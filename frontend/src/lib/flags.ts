/**
 * WP4.2 (ADR-0031) — frontend feature flags.
 *
 * `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` gates the entire Broker Accounts journey. It is a
 * BUILD-TIME flag (Next.js inlines `NEXT_PUBLIC_*` at build), default OFF: when OFF the UI does not
 * exist — no nav entry, the routes return 404, and no API call is ever made. Arming = a rebuild with
 * the flag on (a separate, Sponsor-gated step). Documented in parity/env-allowlist.json + ADR-0031.
 */
function truthy(v: string | undefined): boolean {
  return ["1", "true", "yes", "on"].includes((v ?? "").toString().trim().toLowerCase());
}

export function brokerConnectivityEnabled(): boolean {
  return truthy(process.env.NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED);
}
