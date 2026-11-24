// Typed client for the Copy Trading API (ADR-0051, PR C + founder config UX). Read-only monitoring + the
// founder-only SHADOW configuration write (which can never enable execution).
import { apiFetch } from "@/lib/api";
import type {
  CopyTradingConfig, CopyTradingConfigSaveResult, CopyTradingConfigWrite,
  CopyTradingOverview, CopyTradingStatus, InstrumentMatrix,
  OwnerDemoConfirmResult, OwnerDemoPrepareResult, OwnerDemoReadiness, OwnerDemoStopResult,
} from "@/types/copy-trading";

export async function getCopyTradingOverview(accountId: number): Promise<CopyTradingOverview> {
  return apiFetch<CopyTradingOverview>(`/api/copy-trading/accounts/${accountId}/overview/`, {});
}

// Per-user visibility probe: `enabled` is true only for a founder/operator cohort member (default-DENY).
// Used to hide the Copy Trading navigation entry for non-cohort users.
export async function getCopyTradingStatus(): Promise<CopyTradingStatus> {
  return apiFetch<CopyTradingStatus>(`/api/copy-trading/status/`, {});
}

// Founder-only SHADOW configuration (owner-scoped, cohort-gated). GET returns eligible accounts + current config;
// POST persists the master/follower selection. Neither can enable execution.
export async function getCopyTradingConfig(): Promise<CopyTradingConfig> {
  return apiFetch<CopyTradingConfig>(`/api/copy-trading/config/`, {});
}

export async function saveCopyTradingConfig(body: CopyTradingConfigWrite): Promise<CopyTradingConfigSaveResult> {
  return apiFetch<CopyTradingConfigSaveResult>(`/api/copy-trading/config/`, {
    method: "POST", body: JSON.stringify(body),
  });
}

// Founder-only, owner-scoped, READ-ONLY multi-broker instrument-mapping matrix. Pass master+follower to
// drill into one ordered pair. Catalogue coverage is not execution readiness; this endpoint never writes.
export async function getInstrumentMatrix(pair?: { master: number; follower: number }): Promise<InstrumentMatrix> {
  const q = pair ? `?master=${pair.master}&follower=${pair.follower}` : "";
  return apiFetch<InstrumentMatrix>(`/api/copy-trading/instrument-matrix/${q}`, {});
}

// GFX-170 / ADR-0124 — OWNER-operable governed #46→#48 demonstration control (four stages). Authorization is
// OWNERSHIP of the governed route (server-side); the endpoints 503 when DARK. `confirm` carries the owner's
// re-submitted password for a FRESH re-authentication proof (verified server-side; never stored) + the prepare
// token. The browser never receives or submits a signing secret.
export async function getOwnerDemoReadiness(): Promise<OwnerDemoReadiness> {
  return apiFetch<OwnerDemoReadiness>(`/api/copy-trading/owner-demo/readiness/`, {});
}

export async function ownerDemoPrepare(): Promise<OwnerDemoPrepareResult> {
  return apiFetch<OwnerDemoPrepareResult>(`/api/copy-trading/owner-demo/prepare/`, {
    method: "POST", body: "{}",
  });
}

export async function ownerDemoConfirm(body: { confirm_token: string; password: string }): Promise<OwnerDemoConfirmResult> {
  return apiFetch<OwnerDemoConfirmResult>(`/api/copy-trading/owner-demo/confirm/`, {
    method: "POST", body: JSON.stringify(body),
  });
}

export async function ownerDemoStop(): Promise<OwnerDemoStopResult> {
  return apiFetch<OwnerDemoStopResult>(`/api/copy-trading/owner-demo/stop/`, {
    method: "POST", body: "{}",
  });
}
