"use client";

import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";

/** CONNECTED EMAIL ACCOUNTS — customer self-service Gmail connection + mailbox management (Profile).
 *
 * Lets a member connect a read-only Gmail mailbox so GuvFX can monitor supported BROKER withdrawal notifications,
 * see its live connection status, reconnect it (targeted — same Google account), and disconnect it. It NEVER asks for
 * a Google password, never accepts/stores OAuth tokens, never puts authorization codes in frontend URLs, and reads
 * the AUTHORITATIVE connection state from the backend (never infers success from a query-string). DARK-safe: the
 * status API returns 404 while BROKER_MAILBOX_STATUS_ENABLED is off, so the whole panel HIDES itself (renders null)
 * until the feature is armed. Owner-private: the API only ever returns this member's own mailboxes, always MASKED.
 */
type Lang = "en" | "ja";

type MailboxState =
  | "NOT_CONNECTED" | "CONNECTED" | "HEALTHY" | "DEGRADED" | "RECONNECT_REQUIRED" | "REVOKED";

type Mailbox = {
  mailbox_id: number;
  provider: string;
  masked_email: string;
  state: MailboxState;
  connection_status: string;
  last_successful_sync: string | null;
  reconnect_required: boolean;
  has_verified_broker_bindings: boolean;
};

type StatusResponse = { connected: boolean; mailboxes: Mailbox[] };

const STATUS = "/api/broker-intelligence/mailboxes/status/";
const CONNECT = "/api/broker-intelligence/mailboxes/connect/";
const REVOKE = (id: number) => `/api/broker-intelligence/mailboxes/${id}/revoke/`;

const STR = {
  title: { en: "Connected email accounts", ja: "接続済みメールアカウント" },
  intro: {
    en: "Connect a read-only Gmail mailbox so GuvFX can monitor supported broker withdrawal notifications.",
    ja: "読み取り専用の Gmail を接続すると、GuvFX が対応ブローカーの出金通知を監視できます。",
  },
  privacy: {
    en: "GuvFX reads supported broker notifications to monitor withdrawals. It cannot send email or move funds through this connection.",
    ja: "GuvFX は出金監視のため対応ブローカーの通知のみを読み取ります。この接続でメール送信や資金移動はできません。",
  },
  none: { en: "No mailbox connected yet.", ja: "接続済みのメールボックスはまだありません。" },
  connect: { en: "Connect Gmail", ja: "Gmail を接続" },
  reconnect: { en: "Reconnect", ja: "再接続" },
  disconnect: { en: "Disconnect", ja: "切断" },
  confirm: { en: "Confirm disconnect", ja: "切断を確認" },
  cancel: { en: "Cancel", ja: "キャンセル" },
  disconnectWarn: {
    en: "Disconnecting stops future broker-notification capture. Your existing withdrawal history is kept.",
    ja: "切断すると今後の通知取得が停止します。これまでの出金履歴は保持されます。",
  },
  working: { en: "Working…", ja: "処理中…" },
  lastSync: { en: "Last sync", ja: "最終同期" },
  neverSynced: { en: "not yet", ja: "未実施" },
  verified: { en: "Broker account verified", ja: "ブローカー口座 認証済み" },
  connectFailed: {
    en: "Could not start the Google connection. Please try again.",
    ja: "Google 接続を開始できませんでした。もう一度お試しください。",
  },
  disconnectFailed: { en: "Could not disconnect. Please try again.", ja: "切断できませんでした。もう一度お試しください。" },
};

const STATE_LABEL: Record<MailboxState, { en: string; ja: string }> = {
  NOT_CONNECTED: { en: "Not connected", ja: "未接続" },
  CONNECTED: { en: "Connected — awaiting first sync", ja: "接続済み（初回同期待ち）" },
  HEALTHY: { en: "Connected & monitoring", ja: "接続・監視中" },
  DEGRADED: { en: "Connected — sync delayed", ja: "接続済み（同期が遅延）" },
  RECONNECT_REQUIRED: { en: "Reconnect required", ja: "再接続が必要" },
  REVOKED: { en: "Disconnected", ja: "切断済み" },
};

const STATE_COLOR: Record<MailboxState, string> = {
  NOT_CONNECTED: "#8fa0b7", CONNECTED: "#c9b56b", HEALTHY: "#5ad19a",
  DEGRADED: "#c9b56b", RECONNECT_REQUIRED: "#e8a0a0", REVOKED: "#8fa0b7",
};

const box: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.09)", borderRadius: 14, padding: "1.1rem 1.2rem",
  background: "rgba(12,18,40,0.6)",
};
const meta: React.CSSProperties = { fontSize: "0.72rem", color: "#8fa0b7" };
const btn: React.CSSProperties = {
  padding: "0.42rem 0.7rem", borderRadius: 8, border: "1px solid rgba(255,255,255,0.12)",
  background: "rgba(8,12,28,0.7)", color: "#e9f4ff", fontSize: "0.82rem", cursor: "pointer",
};
const primaryBtn: React.CSSProperties = {
  ...btn, background: "rgba(90,160,209,0.16)", color: "#9fd0f0", border: "1px solid rgba(90,160,209,0.3)",
  fontWeight: 600,
};

export const ConnectedEmailAccounts: React.FC<{ lang?: Lang }> = ({ lang = "en" }) => {
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [data, setData] = useState<StatusResponse | null>(null);
  const [hidden, setHidden] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    apiFetch<StatusResponse>(STATUS, {})
      .then((d) => {
        if (d && Array.isArray(d.mailboxes)) setData(d);
        else setHidden(true);               // non-conforming -> hide, never a broken card
      })
      .catch(() => setHidden(true));        // 404 => feature DARK; any error => hide
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onConnect = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      // The backend returns a signed Google consent URL (for a reconnect it binds this member's existing mailbox as
      // the expected account). We never see or handle the authorization code — Google redirects to the backend
      // callback, and we re-read the authoritative status on return.
      const r = await apiFetch<{ authorization_url?: string }>(CONNECT, {});
      if (r?.authorization_url) {
        window.location.assign(r.authorization_url);
      } else {
        setError(STR.connectFailed[L]);
      }
    } catch {
      setError(STR.connectFailed[L]);
    } finally {
      setBusy(false);
    }
  }, [L]);

  const onDisconnect = useCallback(async (id: number) => {
    setBusy(true);
    setError(null);
    try {
      await apiFetch(REVOKE(id), { method: "POST" });
      setConfirmId(null);
      load();
    } catch {
      setError(STR.disconnectFailed[L]);
    } finally {
      setBusy(false);
    }
  }, [L, load]);

  if (hidden || !data) return null;   // DARK / unavailable / not loaded -> render nothing

  const mailboxes = data.mailboxes;

  return (
    <div style={box} data-testid="connected-email-panel">
      <div style={{ color: "#e9f4ff", fontSize: "0.95rem", fontWeight: 600, marginBottom: 4 }}>
        <i className="ti ti-mail" aria-hidden="true" style={{ marginRight: 6 }} />{STR.title[L]}
      </div>
      <div style={{ ...meta, marginBottom: 12 }}>{STR.intro[L]}</div>

      {mailboxes.length === 0 ? (
        <div style={{ ...meta, color: "#6c7a90", marginBottom: 12 }} data-testid="connected-email-empty">
          {STR.none[L]}
        </div>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: "0 0 12px" }}>
          {mailboxes.map((m) => (
            <li key={m.mailbox_id} data-testid="connected-email-row"
              style={{ padding: "8px 0", borderBottom: "1px solid rgba(255,255,255,0.05)" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
                <span style={{ fontSize: "0.85rem", color: "#cfe0f5" }}>{m.masked_email}</span>
                <span data-testid="connected-email-state" style={{ fontSize: "0.78rem", color: STATE_COLOR[m.state] }}>
                  {STATE_LABEL[m.state]?.[L] ?? m.state}
                </span>
              </div>
              <div style={{ ...meta, marginTop: 2 }}>
                {STR.lastSync[L]}: {m.last_successful_sync || STR.neverSynced[L]}
                {m.has_verified_broker_bindings && (
                  <span style={{ color: "#5ad19a", marginLeft: 8 }}>· {STR.verified[L]} ✓</span>
                )}
              </div>
              <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                <button style={btn} disabled={busy} data-testid="reconnect-btn" onClick={onConnect}>
                  {STR.reconnect[L]}
                </button>
                {confirmId === m.mailbox_id ? (
                  <>
                    <button style={{ ...btn, color: "#e8a0a0", border: "1px solid rgba(232,160,160,0.3)" }}
                      disabled={busy} data-testid="disconnect-confirm-btn" onClick={() => onDisconnect(m.mailbox_id)}>
                      {busy ? STR.working[L] : STR.confirm[L]}
                    </button>
                    <button style={btn} disabled={busy} data-testid="disconnect-cancel-btn"
                      onClick={() => setConfirmId(null)}>{STR.cancel[L]}</button>
                  </>
                ) : (
                  <button style={btn} disabled={busy} data-testid="disconnect-btn"
                    onClick={() => { setConfirmId(m.mailbox_id); setError(null); }}>{STR.disconnect[L]}</button>
                )}
              </div>
              {confirmId === m.mailbox_id && (
                <div style={{ ...meta, color: "#c9b56b", marginTop: 6 }}>{STR.disconnectWarn[L]}</div>
              )}
            </li>
          ))}
        </ul>
      )}

      {mailboxes.length === 0 && (
        <button style={primaryBtn} disabled={busy} data-testid="connect-btn" onClick={onConnect}>
          {busy ? STR.working[L] : STR.connect[L]}
        </button>
      )}

      {error && (
        <div data-testid="connected-email-error" style={{ fontSize: "0.8rem", color: "#e8a0a0", marginTop: 8 }}>
          {error}
        </div>
      )}
      <div style={{ ...meta, marginTop: 12, lineHeight: 1.4 }}>{STR.privacy[L]}</div>
    </div>
  );
};

export default ConnectedEmailAccounts;
