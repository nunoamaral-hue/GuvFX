// Typed client for the Copy Trading API (ADR-0051, PR C + founder config UX). Read-only monitoring + the
// founder-only SHADOW configuration write (which can never enable execution).
import { apiFetch } from "@/lib/api";
import type {
  CopyTradingConfig, CopyTradingConfigSaveResult, CopyTradingConfigWrite,
  CopyTradingOverview, CopyTradingStatus,
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
