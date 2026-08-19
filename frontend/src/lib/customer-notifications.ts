import { apiFetch } from "@/lib/api";

export type TelegramPreferences = {
  telegram_enabled: boolean;
  trade_opened: boolean;
  trade_closed: boolean;
  strategy_changed: boolean;
  execution_problem: boolean;
  workspace_ready: boolean;
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
