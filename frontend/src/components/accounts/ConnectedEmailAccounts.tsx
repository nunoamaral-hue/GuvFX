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

type StatusResponse = {
  connected: boolean;
  mailboxes: Mailbox[];
  can_add_mailbox?: boolean;   // owner-scoped multi-mailbox cohort gate AND below the per-owner cap (default false)
  mailbox_limit?: number;
};

// Explicit connect operations — MUST match the backend contract (views.py). A reconnect ALWAYS names the mailbox so
// the backend binds its canonical identity and can never retarget another account; a first connect names no target;
// ADD_NEW connects an ADDITIONAL mailbox without touching existing rows (multi-mailbox cohort only).
type ConnectOp = "CONNECT_FIRST_MAILBOX" | "RECONNECT_EXISTING_MAILBOX" | "ADD_NEW_MAILBOX";

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
  addAccount: { en: "Add email account", ja: "メールアカウントを追加" },
  addHint: {
    en: "You can connect an additional Gmail mailbox. The mailbox's Google account must be an approved OAuth test user while the app is in Testing; otherwise Google will say the app isn't available — ask the operator to add it.",
    ja: "追加の Gmail を接続できます。アプリが「テスト中」の間は、対象 Google アカウントが承認済みの OAuth テストユーザーである必要があります。未承認の場合は Google が「アプリを利用できません」と表示するため、運用者に追加を依頼してください。",
  },
  reconnect: { en: "Reconnect", ja: "再接続" },
  disconnect: { en: "Disconnect", ja: "切断" },
  confirm: { en: "Confirm disconnect", ja: "切断を確認" },
  cancel: { en: "Cancel", ja: "キャンセル" },
  disconnectWarn: {
    en: "Disconnecting stops future monitoring — GuvFX will no longer read this mailbox. Your existing withdrawal history is kept.",
    ja: "切断すると今後の監視が停止し、GuvFX はこのメールボックスを今後読み取りません。これまでの出金履歴は保持されます。",
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
  // Return-journey banners (after the Google callback redirects back with a non-sensitive ?mailbox_connect marker).
  returnConnected: {
    en: "Mailbox connected. Monitoring will begin shortly.",
    ja: "メールボックスを接続しました。まもなく監視を開始します。",
  },
  returnDenied: {
    en: "You declined the Google permission, so no mailbox was connected.",
    ja: "Google の許可が拒否されたため、メールボックスは接続されませんでした。",
  },
  returnMismatch: {
    en: "That was a different Google account. Please reconnect with the same account.",
    ja: "別の Google アカウントでした。同じアカウントで再接続してください。",
  },
  returnError: {
    en: "We couldn’t complete the Google connection. Please try again.",
    ja: "Google 接続を完了できませんでした。もう一度お試しください。",
  },
  returnAlready: {
    en: "That Gmail account is already connected to your GuvFX account.",
    ja: "その Gmail アカウントは、すでにお客様の GuvFX アカウントに接続されています。",
  },
  returnLimit: {
    en: "You’ve reached the maximum number of connected mailboxes. Disconnect one before adding another.",
    ja: "接続できるメールボックスの上限に達しました。別のものを追加する前に、いずれかを切断してください。",
  },
};

// Map a backend outcome marker to a customer-safe banner message. Anything not listed (every other error code) maps
// to the generic returnError — a member never sees a raw diagnostic. The marker is ONLY a hint; the mailbox list is
// driven by the authoritative owner-scoped status API, never by this query parameter.
const OUTCOME_MSG: Record<string,
  "returnConnected" | "returnDenied" | "returnMismatch" | "returnError" | "returnAlready" | "returnLimit"> = {
  connected: "returnConnected",
  consent_denied: "returnDenied",
  oauth_account_mismatch: "returnMismatch",
  mailbox_already_connected: "returnAlready",
  mailbox_limit_reached: "returnLimit",
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
  const [outcome, setOutcome] = useState<string | null>(null);   // ?mailbox_connect marker from the callback return

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

  // Return journey: after the Google callback the backend redirects back here with a NON-sensitive
  // ?mailbox_connect=<outcome> marker (never a token/code/state). Record it once for a customer-safe banner, then
  // strip it from the URL so a refresh doesn't repeat it. The AUTHORITATIVE mailbox state still comes from load() —
  // a forged marker can show a banner but can never fabricate a connected mailbox in the list.
  useEffect(() => {
    try {
      const params = new URLSearchParams(window.location.search || "");
      const oc = params.get("mailbox_connect");
      if (oc) {
        setOutcome(oc);
        try {
          params.delete("mailbox_connect");
          const qs = params.toString();
          window.history.replaceState(null, "", window.location.pathname + (qs ? `?${qs}` : ""));
        } catch { /* history unavailable (SSR/tests) — harmless */ }
      }
    } catch { /* no window.location — harmless */ }
  }, []);

  const startConnect = useCallback(async (op: ConnectOp, mailboxId?: number) => {
    setBusy(true);
    setError(null);
    try {
      // The backend returns a signed Google consent URL. For a RECONNECT we pass the explicit op + the selected
      // mailbox id, so the backend binds THAT mailbox's canonical identity as the expected account (it can never
      // retarget another). journey=web asks the backend to redirect the browser back to the fixed Profile page on
      // return. We never see or handle the authorization code/token — Google redirects to the backend callback, and
      // this panel re-reads the AUTHORITATIVE status on return (never inferring success from a URL).
      const qs = new URLSearchParams({ journey: "web", op });
      if (mailboxId != null) qs.set("mailbox_id", String(mailboxId));
      const r = await apiFetch<{ authorization_url?: string }>(`${CONNECT}?${qs.toString()}`, {});
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
  const hasLive = !!data.connected;   // >=1 CONNECTED mailbox (a revoked-only member is NOT live)
  const outcomeOk = outcome === "connected";
  const outcomeMsg = outcome ? STR[OUTCOME_MSG[outcome] ?? "returnError"][L] : null;

  return (
    <div style={box} data-testid="connected-email-panel">
      <div style={{ color: "#e9f4ff", fontSize: "0.95rem", fontWeight: 600, marginBottom: 4 }}>
        <i className="ti ti-mail" aria-hidden="true" style={{ marginRight: 6 }} />{STR.title[L]}
      </div>
      <div style={{ ...meta, marginBottom: 12 }}>{STR.intro[L]}</div>

      {/* Return-journey banner (customer-safe; a hint only — the list below is the authoritative status). */}
      {outcomeMsg && (
        <div data-testid="connected-email-notice"
          style={{
            fontSize: "0.8rem", borderRadius: 8, padding: "0.5rem 0.7rem", marginBottom: 12,
            color: outcomeOk ? "#5ad19a" : "#e8a0a0",
            background: outcomeOk ? "rgba(90,209,154,0.08)" : "rgba(232,160,160,0.08)",
            border: `1px solid ${outcomeOk ? "rgba(90,209,154,0.3)" : "rgba(232,160,160,0.3)"}`,
          }}>
          {outcomeMsg}
        </div>
      )}

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
                {/* Reconnect ALWAYS targets THIS mailbox (op + id) — never a generic connect. Prominent when the
                    mailbox actually needs re-authorization, secondary otherwise, so it is clearly distinct from the
                    first-time "Connect Gmail" action. */}
                <button
                  style={m.state === "RECONNECT_REQUIRED" || m.state === "REVOKED" ? primaryBtn : btn}
                  disabled={busy} data-testid="reconnect-btn"
                  onClick={() => startConnect("RECONNECT_EXISTING_MAILBOX", m.mailbox_id)}>
                  {STR.reconnect[L]}
                </button>
                {/* Disconnect is only offered for a LIVE mailbox — never an inapplicable "disconnect" on one that is
                    already revoked/disconnected. */}
                {m.state !== "REVOKED" && m.connection_status !== "REVOKED" && (
                  confirmId === m.mailbox_id ? (
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
                  )
                )}
              </div>
              {confirmId === m.mailbox_id && (
                <div style={{ ...meta, color: "#c9b56b", marginTop: 6 }}>{STR.disconnectWarn[L]}</div>
              )}
            </li>
          ))}
        </ul>
      )}

      {/* First-time connect — offered whenever NO mailbox is live (empty, or every mailbox is revoked). Visibly a
          DIFFERENT action from a per-row reconnect. */}
      {!hasLive && (
        <button style={primaryBtn} disabled={busy} data-testid="connect-btn"
          onClick={() => startConnect("CONNECT_FIRST_MAILBOX")}>
          {busy ? STR.working[L] : STR.connect[L]}
        </button>
      )}

      {/* ADD ANOTHER mailbox — shown ONLY when a live mailbox already exists AND the backend says this owner may add
          one (owner-scoped multi-mailbox cohort gate + below the per-owner cap). A DISTINCT action that connects an
          ADDITIONAL mailbox via ADD_NEW_MAILBOX and never touches the existing rows. Absent for single-mailbox members
          (can_add_mailbox falsy), so the unfinished multi flow is never shown outside the cohort. */}
      {hasLive && data.can_add_mailbox && (
        <div data-testid="add-account-block">
          <button style={primaryBtn} disabled={busy} data-testid="add-account-btn"
            onClick={() => startConnect("ADD_NEW_MAILBOX")}>
            {busy ? STR.working[L] : STR.addAccount[L]}
          </button>
          <div style={{ ...meta, marginTop: 6, lineHeight: 1.4 }}>{STR.addHint[L]}</div>
        </div>
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
