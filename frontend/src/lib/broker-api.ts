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
