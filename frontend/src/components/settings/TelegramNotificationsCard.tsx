"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLang } from "@/components/AppShell";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import {
  createTelegramConnection,
  disconnectTelegram,
  getTelegramSettings,
  type TelegramPreferences,
  type TelegramSettings,
  updateTelegramPreferences,
} from "@/lib/customer-notifications";
import { t } from "@/lib/i18n";

type BooleanPreference = Exclude<keyof TelegramPreferences, "language">;

const preferenceRows: Array<{ field: BooleanPreference; label: string; detail: string }> = [
  { field: "winning_trades", label: "telegram.pref.winners", detail: "telegram.pref.winnersDetail" },
  { field: "losing_trades", label: "telegram.pref.losers", detail: "telegram.pref.losersDetail" },
  { field: "tp_progress", label: "telegram.pref.tpProgress", detail: "telegram.pref.tpProgressDetail" },
  { field: "system_messages", label: "telegram.pref.system", detail: "telegram.pref.systemDetail" },
];

function safeError(lang: "en" | "ja") {
  return t(lang, "telegram.error");
}

export function TelegramNotificationsCard() {
  const lang = useLang();
  const [settings, setSettings] = useState<TelegramSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const [error, setError] = useState("");
  const polls = useRef(0);

  const load = useCallback(async () => {
    try {
      let next = await getTelegramSettings();
      // The app language is authoritative for new customer messages. Persist a language
      // switch immediately; customers should not need to toggle an unrelated preference.
      if (next.connected && next.preferences.language !== lang) {
        next = await updateTelegramPreferences({ language: lang });
      }
      setSettings(next);
      if (next.connected) setWaiting(false);
      return next;
    } catch {
      setError(safeError(lang));
      return null;
    } finally {
      setLoading(false);
    }
  }, [lang]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!waiting) return;
    polls.current = 0;
    const id = window.setInterval(async () => {
      polls.current += 1;
      const next = await load();
      if (next?.connected || polls.current >= 60) {
        setWaiting(false);
        window.clearInterval(id);
      }
    }, 2000);
    return () => window.clearInterval(id);
  }, [waiting, load]);

  const connect = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await createTelegramConnection(lang);
      const opened = window.open(result.url, "_blank", "noopener,noreferrer");
      if (!opened) window.location.assign(result.url);
      setWaiting(true);
    } catch {
      setError(safeError(lang));
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    setError("");
    try {
      setSettings(await disconnectTelegram());
      setWaiting(false);
    } catch {
      setError(safeError(lang));
    } finally {
      setBusy(false);
    }
  };

  const update = async (field: BooleanPreference, value: boolean) => {
    if (!settings) return;
    setBusy(true);
    setError("");
    try {
      setSettings(await updateTelegramPreferences({ [field]: value, language: lang }));
    } catch {
      setError(safeError(lang));
    } finally {
      setBusy(false);
    }
  };

  const connectedLabel = settings?.display.username
    ? `@${settings.display.username}`
    : settings?.display.first_name || "Telegram";
  const stateLabel = settings?.connected
    ? t(lang, "telegram.connected")
    : waiting
      ? t(lang, "telegram.connecting")
      : t(lang, "telegram.notConnected");

  return (
    <div id="telegram-notifications">
    <Card
      title={t(lang, "telegram.title")}
      subtitle={t(lang, "telegram.subtitle")}
      style={{ borderColor: settings?.connected ? "rgba(34,197,94,0.45)" : undefined }}
    >
      {error && <Alert type="error">{error}</Alert>}
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: "0.75rem" }}>
        <div>
          <span
            aria-label={stateLabel}
            style={{
              display: "inline-flex", alignItems: "center", gap: 7, borderRadius: 999,
              padding: "0.3rem 0.7rem", fontSize: "0.82rem", fontWeight: 700,
              color: settings?.connected ? "#86efac" : "#cbd5e1",
              background: settings?.connected ? "rgba(34,197,94,0.12)" : "rgba(148,163,184,0.10)",
              border: `1px solid ${settings?.connected ? "rgba(34,197,94,0.35)" : "rgba(148,163,184,0.25)"}`,
            }}
          >
            <span aria-hidden="true" style={{ width: 7, height: 7, borderRadius: "50%", background: settings?.connected ? "#4ade80" : "#94a3b8" }} />
            {stateLabel}
          </span>
          {settings?.connected && (
            <p style={{ margin: "0.45rem 0 0", color: "#a8b7cc", fontSize: "0.82rem" }}>
              {t(lang, "telegram.connectedAs")} {connectedLabel}
            </p>
          )}
        </div>
        {!loading && !settings?.connected && settings?.available && (
          <Button onClick={connect} disabled={busy}>
            {waiting ? t(lang, "telegram.waiting") : t(lang, "telegram.connect")}
          </Button>
        )}
        {settings?.connected && (
          <div style={{ display: "flex", gap: "0.55rem", flexWrap: "wrap" }}>
            <Button variant="secondary" onClick={() => setPreferencesOpen(true)} disabled={busy}>
              {t(lang, "telegram.preferences")}
            </Button>
            <Button variant="secondary" onClick={disconnect} disabled={busy}>
              {t(lang, "telegram.disconnect")}
            </Button>
          </div>
        )}
      </div>

      {!loading && settings && !settings.available && !settings.connected && (
        <p style={{ color: "#fbbf24", fontSize: "0.85rem", margin: "0.8rem 0 0" }}>
          {t(lang, "telegram.unavailable")}
        </p>
      )}
      {waiting && !settings?.connected && (
        <Alert type="info">{t(lang, "telegram.startPrompt")}</Alert>
      )}

    </Card>
    {settings?.connected && preferencesOpen && (
      <div
        role="presentation"
        onMouseDown={(event) => { if (event.target === event.currentTarget) setPreferencesOpen(false); }}
        style={{
          position: "fixed", inset: 0, zIndex: 1000, display: "grid", placeItems: "center",
          padding: "1rem", background: "rgba(2,6,18,0.76)", backdropFilter: "blur(5px)",
        }}
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="telegram-preferences-title"
          style={{
            width: "min(460px, calc(100vw - 2rem))", maxHeight: "calc(100vh - 2rem)", overflowY: "auto",
            borderRadius: 16, border: "1px solid rgba(74,179,255,0.24)", background: "#09132b",
            boxShadow: "0 24px 80px rgba(0,0,0,0.55)", padding: "1.15rem",
          }}
        >
          <div style={{ display: "flex", alignItems: "start", justifyContent: "space-between", gap: "1rem" }}>
            <div>
              <h2 id="telegram-preferences-title" style={{ margin: 0, fontSize: "1.05rem", color: "#e9f4ff" }}>
                {t(lang, "telegram.preferences")}
              </h2>
              <p style={{ margin: "0.35rem 0 0", color: "#8fa0b7", fontSize: "0.8rem", lineHeight: 1.5 }}>
                {t(lang, "telegram.preferencesDetail")}
              </p>
            </div>
            <button
              type="button"
              aria-label={t(lang, "common.close")}
              onClick={() => setPreferencesOpen(false)}
              style={{ border: 0, background: "transparent", color: "#a8b7cc", fontSize: "1.35rem", cursor: "pointer" }}
            >×</button>
          </div>
          <fieldset disabled={busy} style={{ border: 0, margin: "1rem 0 0", padding: 0, minWidth: 0 }}>
            {preferenceRows.map((row) => (
              <PreferenceRow
                key={row.field}
                label={t(lang, row.label)}
                detail={t(lang, row.detail)}
                checked={Boolean(settings.preferences[row.field])}
                onChange={(value) => update(row.field, value)}
              />
            ))}
          </fieldset>
          <div style={{ marginTop: "1rem", display: "flex", justifyContent: "flex-end" }}>
            <Button variant="secondary" onClick={() => setPreferencesOpen(false)}>{t(lang, "common.done")}</Button>
          </div>
        </div>
      </div>
    )}
    </div>
  );
}

function PreferenceRow(props: {
  label: string; detail: string; checked: boolean; disabled?: boolean; strong?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label style={{
      display: "grid", gridTemplateColumns: "minmax(0,1fr) auto", alignItems: "center", gap: "1rem",
      padding: "0.72rem 0", borderTop: "1px solid rgba(148,163,184,0.16)", cursor: props.disabled ? "default" : "pointer",
    }}>
      <span style={{ minWidth: 0 }}>
        <span style={{ display: "block", color: "#e5edf9", fontSize: "0.88rem", fontWeight: props.strong ? 700 : 500 }}>
          {props.label}
        </span>
        <span style={{ display: "block", color: "#8fa0b7", fontSize: "0.78rem", marginTop: 2 }}>
          {props.detail}
        </span>
      </span>
      <input
        type="checkbox" checked={props.checked} disabled={props.disabled}
        onChange={(event) => props.onChange(event.target.checked)}
        style={{ width: 20, height: 20, accentColor: "#38bdf8", cursor: props.disabled ? "default" : "pointer" }}
      />
    </label>
  );
}
