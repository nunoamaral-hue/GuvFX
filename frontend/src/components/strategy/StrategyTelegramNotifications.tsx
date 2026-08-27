"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  getStrategyNotificationSettings,
  type StrategyNotificationSettings,
  updateStrategyNotificationSettings,
} from "@/lib/customer-notifications";
import { t, type Lang } from "@/lib/i18n";

export function StrategyTelegramNotifications({ assignmentId, lang }: { assignmentId: number | null; lang: Lang }) {
  const [settings, setSettings] = useState<StrategyNotificationSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    setSettings(null);
    setError(false);
    if (!assignmentId) return;
    let cancelled = false;
    getStrategyNotificationSettings(assignmentId)
      .then((value) => { if (!cancelled) setSettings(value); })
      .catch(() => { if (!cancelled) setError(true); });
    return () => { cancelled = true; };
  }, [assignmentId]);

  if (!assignmentId) return null;

  const update = async (enabled: boolean) => {
    setBusy(true);
    setError(false);
    try {
      setSettings(await updateStrategyNotificationSettings(assignmentId, enabled));
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby="strategy-notifications-title" style={{ marginTop: "1rem", paddingTop: "1rem", borderTop: "1px solid rgba(255,255,255,0.08)" }}>
      <h3 id="strategy-notifications-title" style={{ margin: 0, color: "#e8f0ff", fontSize: "0.95rem" }}>
        {t(lang, "configure.notificationsTitle")}
      </h3>
      {settings && !settings.telegram_connected ? (
        // Telegram not connected: show an intentional explanatory state + a direct route to the single
        // authoritative Telegram settings surface — NOT a toggle that silently does nothing. No binding
        // logic is duplicated here; the CTA links to the profile Telegram card anchor.
        <div role="status" style={{ marginTop: "0.7rem", borderRadius: 9, padding: "0.7rem 0.8rem", background: "rgba(59,130,246,0.10)", border: "1px solid rgba(59,130,246,0.28)", color: "#b9d6ff", fontSize: "0.8rem" }}>
          {t(lang, "configure.notificationsConnectRequired")}{" "}
          <Link href="/profile#telegram-notifications" style={{ color: "#7dd3fc", fontWeight: 700 }}>
            {t(lang, "configure.notificationsConnect")}
          </Link>
        </div>
      ) : (
        <label style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) auto", alignItems: "center", gap: "1rem", marginTop: "0.7rem", cursor: busy ? "default" : "pointer" }}>
          <span>
            <span style={{ display: "block", color: "#dce8f8", fontSize: "0.86rem" }}>
              {t(lang, "configure.notificationsEnable")}
            </span>
            <span style={{ display: "block", color: "#7f91aa", fontSize: "0.75rem", marginTop: 3, lineHeight: 1.45 }}>
              {t(lang, "configure.notificationsDetail")}
            </span>
          </span>
          <input
            aria-label={t(lang, "configure.notificationsEnable")}
            type="checkbox"
            checked={Boolean(settings?.enabled)}
            disabled={busy || !settings}
            onChange={(event) => void update(event.target.checked)}
            style={{ width: 20, height: 20, accentColor: "#38bdf8" }}
          />
        </label>
      )}
      {error && <p role="alert" style={{ color: "#fca5a5", fontSize: "0.78rem", margin: "0.65rem 0 0" }}>{t(lang, "configure.notificationsError")}</p>}
    </section>
  );
}
