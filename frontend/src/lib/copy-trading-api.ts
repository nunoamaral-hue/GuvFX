// Typed client for the Copy Trading API (ADR-0051, PR C + founder config UX). Read-only monitoring + the
// founder-only SHADOW configuration write (which can never enable execution).
import { apiFetch } from "@/lib/api";
import type {
  CopyTradingConfig, CopyTradingConfigSaveResult, CopyTradingConfigWrite,
  CopyTradingOverview, CopyTradingStatus, InstrumentMatrix,
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
