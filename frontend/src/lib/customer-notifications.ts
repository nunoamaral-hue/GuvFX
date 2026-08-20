import { apiFetch } from "@/lib/api";

export type TelegramPreferences = {
  winning_trades: boolean;
  losing_trades: boolean;
  tp_progress: boolean;
  system_messages: boolean;
  language: "en" | "ja";
};

export type TelegramSettings = {
  available: boolean;
  connected: boolean;
  display: { username: string; first_name: string };
  preferences: TelegramPreferences;
};

const BASE = "/api/customer-notifications/telegram";

export const getTelegramSettings = () => apiFetch<TelegramSettings>(`${BASE}/`);

export const createTelegramConnection = (language: "en" | "ja") =>
  apiFetch<{ url: string; expires_at: string }>(`${BASE}/connect/`, {
    method: "POST",
    body: JSON.stringify({ language }),
  });

export const disconnectTelegram = () =>
  apiFetch<TelegramSettings>(`${BASE}/disconnect/`, { method: "POST" });

export const updateTelegramPreferences = (updates: Partial<TelegramPreferences>) =>
  apiFetch<TelegramSettings>(`${BASE}/preferences/`, {
    method: "PATCH",
    body: JSON.stringify(updates),
  });

export type StrategyNotificationSettings = {
  assignment_id: number;
  enabled: boolean;
  pending_enable: boolean;
  telegram_connected: boolean;
};

export const getStrategyNotificationSettings = (assignmentId: number) =>
  apiFetch<StrategyNotificationSettings>(`${BASE}/strategy-preferences/${assignmentId}/`);

export const updateStrategyNotificationSettings = (assignmentId: number, enabled: boolean) =>
  apiFetch<StrategyNotificationSettings>(`${BASE}/strategy-preferences/${assignmentId}/`, {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  });

export type WorkspaceReadinessSettings = {
  available: boolean;
  has_workspace: boolean;
  requested: boolean;
  fulfilled: boolean;
  workspace_ready: boolean;
  telegram_connected: boolean;
  connect_url?: string | null;
};

export const getWorkspaceReadinessSettings = () =>
  apiFetch<WorkspaceReadinessSettings>(`${BASE}/workspace-readiness/`);

export const requestWorkspaceReadinessNotification = (language: "en" | "ja") =>
  apiFetch<WorkspaceReadinessSettings>(`${BASE}/workspace-readiness/`, {
    method: "POST",
    body: JSON.stringify({ language }),
  });
