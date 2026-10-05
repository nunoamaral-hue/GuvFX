/** WP4.2 (ADR-0031) — the single client for the merged WP1A broker-connectivity backend
 * (trading.views bc_* actions on TradingAccountViewSet). All URLs live here so components never build
 * endpoints; each function is a thin, mockable wrapper over apiFetch (which injects CSRF, auto-refreshes
 * on 401, and throws the DRF customer-safe `detail` on error). No new backend endpoints. */
import { apiFetch } from "@/lib/api";
import type {
  BrokerAccount, BrokerStatus, DeliveryStateResult, DesktopLinkResult, DisconnectResult, EntitlementSummary,
  ReplaceCredentialsResult, ValidationAttempt, ValidationTimeline,
} from "@/types/broker";

const BASE = "/api/trading/accounts";

export async function listAccounts(): Promise<BrokerAccount[]> {
  return (await apiFetch<BrokerAccount[]>(`${BASE}/`)) || [];
}

export function getAccount(id: number): Promise<BrokerAccount> {
  return apiFetch<BrokerAccount>(`${BASE}/${id}/`);
}

export function getBrokerStatus(id: number): Promise<BrokerStatus> {
  return apiFetch<BrokerStatus>(`${BASE}/${id}/broker/status/`);
}

export async function getValidationHistory(id: number): Promise<ValidationAttempt[]> {
  return (await apiFetch<ValidationAttempt[]>(`${BASE}/${id}/broker/validation-history/`)) || [];
}

export function testConnection(id: number): Promise<ValidationAttempt> {
  return apiFetch<ValidationAttempt>(`${BASE}/${id}/broker/test-connection/`, { method: "POST" });
}

export function retryValidation(id: number): Promise<ValidationAttempt> {
  return apiFetch<ValidationAttempt>(`${BASE}/${id}/broker/retry-validation/`, { method: "POST" });
}

/** Graceful reconnect after a transport failure. `run_broker_validation` COMMITS the attempt row (and the
 * durable status) BEFORE the HTTP response is sent, so if the connection drops on the way back — a proxy
 * read timeout, a browser network blip, a laptop lid — the validation may have completed successfully on
 * the backend while the browser only saw "Failed to fetch". Best-effort: re-fetch the (newest-first)
 * history a couple of times and return the newest attempt created AFTER `afterId` (ids are monotonic), so
 * the customer sees the REAL completed result instead of a transport error. Returns null when nothing new
 * appeared (e.g. the worker really died mid-validate before committing) — the caller then shows a safe
 * transient message. */
export async function recoverAttemptAfterTransportFailure(
  id: number, afterId: number, opts: { tries?: number; delayMs?: number } = {},
): Promise<ValidationAttempt | null> {
  const tries = Math.max(1, opts.tries ?? 2);
  const delayMs = opts.delayMs ?? 400;
  for (let i = 0; i < tries; i++) {
    const hist = await getValidationHistory(id).catch(() => [] as ValidationAttempt[]);
    const fresh = hist.filter((a) => a.id > afterId).sort((a, b) => b.id - a.id)[0];
    if (fresh) return fresh;
    if (i < tries - 1) await new Promise((r) => setTimeout(r, delayMs));
  }
  return null;
}

export function replaceCredentials(
  id: number, password: string, revalidate = true,
): Promise<ReplaceCredentialsResult> {
  return apiFetch<ReplaceCredentialsResult>(`${BASE}/${id}/broker/replace-credentials/`, {
    method: "POST", body: JSON.stringify({ password, revalidate }),
  });
}

export function disconnectAccount(id: number): Promise<DisconnectResult> {
  return apiFetch<DisconnectResult>(`${BASE}/${id}/broker/disconnect/`, { method: "POST" });
}

/** WS-D/Phase-3 — staff-only validation timeline. Search by ONE of correlation id / account id / attempt id.
 * The backend enforces IsAdminUser + darkness; this is a thin client. */
export function getValidationTimeline(
  params: { correlationId?: string; accountId?: string; attemptId?: string },
): Promise<ValidationTimeline> {
  const q = new URLSearchParams();
  if (params.correlationId) q.set("correlation_id", params.correlationId.trim());
  if (params.accountId) q.set("account_id", params.accountId.trim());
  if (params.attemptId) q.set("attempt_id", params.attemptId.trim());
  return apiFetch<ValidationTimeline>(`${BASE}/validation-timeline/?${q.toString()}`);
}

export function createAccount(input: {
  // D2 (Stream D): explicit required account type (demo|live) — the backend rejects a missing/null/invalid
  // type. Maps to the stored is_demo server-side (demo→True, live→False).
  name: string; broker_name: string; account_number: string; password: string; account_type: "demo" | "live";
}): Promise<BrokerAccount> {
  return apiFetch<BrokerAccount>(`${BASE}/`, { method: "POST", body: JSON.stringify(input) });
}

/** Result of adding a HOSTED broker account (Provider-B persistent workspace). No password is involved —
 * the customer logs into their broker inside MetaTrader after the runtime is provisioned. */
export type HostedAddResult = { status: "created" | "exists"; trading_account_id: number; workspace_uuid: string };

/** Add a HOSTED broker account through the certified member onboarding path (POST /api/hosted-workspace/
 * accounts/add/). It creates an intent-only TradingAccount + persistent workspace and the per-account
 * scheduler provisions ONLY this account (its own Windows identity, runtime, endpoint, RemoteApp). The
 * broker PASSWORD is NEVER sent here — the customer enters it directly in MetaTrader (Open MT5 → log in).
 * `expected_server` is stored as the account's broker server identity (used for the execution identity pin),
 * so pass the broker's real MT5 server name. Owner-scoped + entitlement-bounded on the backend. */
export function addHostedAccount(input: {
  broker_name: string; expected_login: string; expected_server: string; is_demo: boolean;
}): Promise<HostedAddResult> {
  return apiFetch<HostedAddResult>(`/api/hosted-workspace/accounts/add/`, {
    method: "POST", body: JSON.stringify(input),
  });
}

/** Phase C4 — read-only entitlement summary for the Broker Accounts header ("Active N / limit"). */
export function getEntitlementSummary(): Promise<EntitlementSummary> {
  return apiFetch<EntitlementSummary>(`${BASE}/entitlement-summary/`);
}

/** Phase 9 — per-user Broker Accounts UX capability from GET /api/auth/me/ (additive `broker_accounts_ux`).
 * Empty allowlist by default ⇒ False for every user (legacy experience); granted per-user (support@ in the
 * POC). Fails closed (False) so a fetch/auth error never exposes the unfinished multi-account UX. */
export async function getBrokerAccountsUxEnabled(): Promise<boolean> {
  try {
    const me = await apiFetch<{ broker_accounts_ux?: boolean }>(`/api/auth/me/`);
    return Boolean(me && me.broker_accounts_ux);
  } catch {
    return false;
  }
}

/** Phase C4 — activate / deactivate ONE account. The backend applies STANDARD (one-active-per-user) or
 * CONCURRENT (up-to-limit) semantics ONLY when concurrent enforcement is armed; while DARK it is the exact
 * legacy plain flip. Toggling active NEVER arms execution. */
export function setAccountActive(id: number, isActive: boolean): Promise<{ ok: boolean; id: number; is_active: boolean }> {
  return apiFetch(`${BASE}/${id}/set-active/`, { method: "POST", body: JSON.stringify({ is_active: isActive }) });
}

/** Member "Remove account" — a history-retaining decommission (tombstone). Stops trading, frees the entitlement
 * slot, and keeps trading history; fails (409) if the account has open trades. The broker account itself is not
 * deleted at the broker. */
export function removeAccount(id: number): Promise<{ ok: boolean; id: number; removed: boolean; already: boolean }> {
  return apiFetch(`${BASE}/${id}/remove/`, { method: "POST" });
}

/** Phase 9 — the AUTHORITATIVE per-account hosted delivery signal (owner-scoped, IDOR-safe, account-explicit).
 * `deliverable` gates the "Open MT5" button (availability, not connection); `delivery_state` reports the broker
 * session/connection lifecycle. Returns null on 404 (traditional account / dark / no workspace) or any error. */
export async function getDeliveryState(accountId: number): Promise<DeliveryStateResult | null> {
  try {
    return await apiFetch<DeliveryStateResult>(`/api/hosted-workspace/delivery-state/?account_id=${accountId}`);
  } catch {
    return null; // traditional account / dark / not-owned → no hosted delivery signal
  }
}

/** Phase C4 — account-EXPLICIT "View MT5". Passes the owner-scoped TradingAccount.id so the desktop link is
 * always for the account the customer clicked (never a `.first()` guess). A cross-user/unknown id 404s on the
 * backend; viewing account B never mutates account A. */
export function openMt5Desktop(accountId: number): Promise<DesktopLinkResult> {
  return apiFetch<DesktopLinkResult>(`/api/mt5/desktop-link/`, {
    method: "POST", body: JSON.stringify({ account_id: accountId }),
  });
}
