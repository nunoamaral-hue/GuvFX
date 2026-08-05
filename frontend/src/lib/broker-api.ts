/** WP4.2 (ADR-0031) — the single client for the merged WP1A broker-connectivity backend
 * (trading.views bc_* actions on TradingAccountViewSet). All URLs live here so components never build
 * endpoints; each function is a thin, mockable wrapper over apiFetch (which injects CSRF, auto-refreshes
 * on 401, and throws the DRF customer-safe `detail` on error). No new backend endpoints. */
import { apiFetch } from "@/lib/api";
import type {
  BrokerAccount, BrokerStatus, DisconnectResult, ReplaceCredentialsResult, ValidationAttempt,
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

export function createAccount(input: {
  name: string; broker_name: string; account_number: string; password: string; is_demo: boolean;
}): Promise<BrokerAccount> {
  return apiFetch<BrokerAccount>(`${BASE}/`, { method: "POST", body: JSON.stringify(input) });
}
