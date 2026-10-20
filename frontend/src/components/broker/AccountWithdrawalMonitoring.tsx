"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import type {
  AccountContactsResponse,
  BrokerContact,
  CreateContactResponse,
  VerifyContactResponse,
} from "@/types/broker-contact";

/** WITHDRAWAL MONITORING — account-level broker-email association (MULTI_ACCOUNT §A/§C/§4).
 *
 * For ONE of the member's OWN trading accounts: associate the broker-registered notification email (optionally the
 * connected mailbox that receives it), then verify the association. One email may be associated with many of the
 * member's accounts. Binding is explicit and NEVER auto-verified; verification is fail-closed on composed proof.
 * DARK-safe: the contacts API returns 404 while BROKER_ACCOUNT_CONTACT_ENABLED is off, so the whole panel HIDES
 * itself (renders null). Owner-private: the API only ever returns this member's own bindings, always MASKED. It never
 * accepts a password/token and never moves funds.
 */
type Lang = "en" | "ja";

const contactsUrl = (id: number) => `/api/broker-intelligence/accounts/${id}/contacts/`;
const createUrl = (id: number) => `/api/broker-intelligence/accounts/${id}/contacts/create/`;
const verifyUrl = (id: number, cid: number) => `/api/broker-intelligence/accounts/${id}/contacts/${cid}/verify/`;
const MAILBOX_STATUS = "/api/broker-intelligence/mailboxes/status/";

const STR = {
  title: { en: "Withdrawal monitoring", ja: "出金モニタリング" },
  intro: {
    en: "Associate the broker-registered email that receives this account's withdrawal notifications, then verify it.",
    ja: "この口座の出金通知を受け取るブローカー登録メールを関連付け、認証します。",
  },
  emailLabel: { en: "Broker-registered notification email", ja: "ブローカー登録の通知メール" },
  mailboxLabel: { en: "Receiving connected mailbox (optional)", ja: "受信する接続済みメールボックス（任意）" },
  mailboxNone: { en: "— none —", ja: "— なし —" },
  associate: { en: "Associate", ja: "関連付ける" },
  verify: { en: "Verify", ja: "認証する" },
  working: { en: "Working…", ja: "処理中…" },
  none: { en: "No broker email associated with this account yet.", ja: "この口座に関連付けられたブローカーメールはまだありません。" },
  statusLabel: { en: "Status", ja: "状態" },
  verifiedAt: { en: "Verified", ja: "認証日時" },
  privacy: {
    en: "GuvFX reads supported broker notifications to monitor withdrawals. It never initiates, confirms or cancels a withdrawal.",
    ja: "GuvFX は出金監視のため対応ブローカーの通知を読み取ります。出金の開始・確認・取消は一切行いません。",
  },
  needEmail: { en: "Enter the broker-registered email.", ja: "ブローカー登録のメールを入力してください。" },
  associateFailed: { en: "Could not associate that email with this account.", ja: "そのメールをこの口座に関連付けできませんでした。" },
  verifyFailed: { en: "Could not verify right now. Please try again.", ja: "現在認証できませんでした。もう一度お試しください。" },
} as const;

const STATUS_STR: Record<string, { en: string; ja: string }> = {
  PENDING: { en: "Pending verification", ja: "認証待ち" },
  VERIFIED: { en: "Verified", ja: "認証済み" },
  UNRESOLVED: { en: "Could not be verified", ja: "認証できませんでした" },
  RETIRED: { en: "Retired", ja: "解除済み" },
};

// Fail-closed verification reason codes from contact_verification.check_consistency (secret-free).
const REASON_STR: Record<string, { en: string; ja: string }> = {
  no_connected_mailbox: {
    en: "Select the connected mailbox that receives this broker's mail, then verify.",
    ja: "このブローカーのメールを受信する接続済みメールボックスを選択してから認証してください。",
  },
  mailbox_not_connected: {
    en: "That mailbox is not currently connected. Reconnect it, then verify.",
    ja: "そのメールボックスは現在接続されていません。再接続してから認証してください。",
  },
  mailbox_owner_mismatch: {
    en: "The selected mailbox isn't owned by this account's owner.",
    ja: "選択したメールボックスはこの口座の所有者のものではありません。",
  },
  identity_owner_mismatch: {
    en: "That broker email can't be verified for this account.",
    ja: "そのブローカーメールはこの口座では認証できません。",
  },
  broker_mismatch: {
    en: "The email's broker doesn't match this account's broker.",
    ja: "メールのブローカーがこの口座のブローカーと一致しません。",
  },
};

interface MailboxOption { mailbox_id: number; masked_email: string; state: string; }

export const AccountWithdrawalMonitoring: React.FC<{ accountId: number; brokerName?: string; lang?: Lang }> = ({
  accountId, brokerName = "", lang = "en",
}) => {
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [hidden, setHidden] = useState(false);
  const [contacts, setContacts] = useState<BrokerContact[] | null>(null);
  const [mailboxes, setMailboxes] = useState<MailboxOption[]>([]);
  const [email, setEmail] = useState("");
  const [mailboxId, setMailboxId] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reasonById, setReasonById] = useState<Record<number, string>>({});
  // Unmount guard: the mount-time fetches below (and the post-await handlers) must never setState after the
  // component unmounts — otherwise an in-flight apiFetch resolving after teardown triggers an async-setState-after-
  // unmount warning (observed as the PR#531 frontend CI failure). All setState is gated on mountedRef.current.
  const mountedRef = useRef(true);
  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false; }; }, []);

  const load = useCallback(() => {
    apiFetch<AccountContactsResponse>(contactsUrl(accountId), {})
      .then((d) => {
        if (!mountedRef.current) return;
        if (d && Array.isArray(d.contacts)) setContacts(d.contacts);
        else setHidden(true);               // non-conforming -> hide, never a broken card
      })
      .catch(() => { if (mountedRef.current) setHidden(true); });   // 404 => feature DARK; any error => hide
    // Best-effort mailbox list for the dropdown; its own flag may be off (404) — then just omit the selector.
    apiFetch<{ mailboxes?: MailboxOption[] }>(MAILBOX_STATUS, {})
      .then((d) => { if (mountedRef.current) setMailboxes(Array.isArray(d?.mailboxes) ? d!.mailboxes : []); })
      .catch(() => { if (mountedRef.current) setMailboxes([]); });
  }, [accountId]);

  useEffect(() => { load(); }, [load]);

  const associate = useCallback(async () => {
    const addr = email.trim();
    if (!addr) { setError(STR.needEmail[L]); return; }
    setBusy(true); setError(null);
    try {
      const body: Record<string, unknown> = { email: addr, broker: brokerName || brokerOf(contacts) };
      if (mailboxId) body.connected_mailbox_id = Number(mailboxId);
      await apiFetch<CreateContactResponse>(createUrl(accountId), { method: "POST", body: JSON.stringify(body) });
      if (!mountedRef.current) return;
      setEmail("");
      load();
    } catch {
      if (mountedRef.current) setError(STR.associateFailed[L]);
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }, [email, mailboxId, accountId, brokerName, contacts, L, load]);

  const verify = useCallback(async (cid: number) => {
    setBusy(true); setError(null);
    try {
      const r = await apiFetch<VerifyContactResponse>(verifyUrl(accountId, cid), { method: "POST" });
      if (!mountedRef.current) return;
      if (r && !r.verified && r.reason) setReasonById((m) => ({ ...m, [cid]: r.reason }));
      else setReasonById((m) => { const n = { ...m }; delete n[cid]; return n; });
      load();
    } catch {
      if (mountedRef.current) setError(STR.verifyFailed[L]);
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }, [accountId, L, load]);

  if (hidden) return null;                    // DARK: feature off (404) — render nothing
  if (contacts === null) return null;         // loading — keep the page calm until data is ready

  return (
    <div style={{ marginTop: 18, border: "1px solid rgba(74,179,255,0.15)", borderRadius: 12, padding: "1.25rem 1.5rem",
                  background: "rgba(255,255,255,0.02)" }}>
      <h3 style={{ margin: "0 0 0.35rem", fontSize: "1.05rem" }}>{STR.title[L]}</h3>
      <p style={{ color: "#9db6d6", margin: "0 0 1rem", fontSize: "0.9rem", lineHeight: 1.5 }}>{STR.intro[L]}</p>

      {contacts.length === 0 && <p style={{ color: "#9db6d6", fontSize: "0.9rem" }}>{STR.none[L]}</p>}

      {contacts.map((c) => (
        <div key={c.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "1rem",
                                 padding: "0.6rem 0", borderTop: "1px solid rgba(74,179,255,0.08)" }}>
          <div>
            <div style={{ fontSize: "0.95rem" }}>{c.email_masked} · {c.broker}</div>
            <div style={{ color: "#9db6d6", fontSize: "0.8rem", marginTop: 2 }}>
              {STR.statusLabel[L]}: {(STATUS_STR[c.status] || { en: c.status, ja: c.status })[L]}
              {c.connected_mailbox_masked ? ` · ${c.connected_mailbox_masked}` : ""}
            </div>
            {reasonById[c.id] && REASON_STR[reasonById[c.id]] && (
              <div style={{ color: "#ffd27a", fontSize: "0.8rem", marginTop: 4 }}>{REASON_STR[reasonById[c.id]][L]}</div>
            )}
          </div>
          {c.status !== "VERIFIED" && c.status !== "RETIRED" && (
            <button onClick={() => void verify(c.id)} disabled={busy}
                    style={btnStyle(false)}>{busy ? STR.working[L] : STR.verify[L]}</button>
          )}
        </div>
      ))}

      <div style={{ marginTop: "1rem", display: "flex", flexWrap: "wrap", gap: "0.5rem", alignItems: "center" }}>
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
               placeholder={STR.emailLabel[L]} aria-label={STR.emailLabel[L]}
               style={{ flex: "1 1 240px", padding: "0.5rem 0.75rem", borderRadius: 8,
                        border: "1px solid rgba(255,255,255,0.15)", background: "transparent", color: "#e5f4ff" }} />
          {mailboxes.length > 0 && (
            <select value={mailboxId} onChange={(e) => setMailboxId(e.target.value)} aria-label={STR.mailboxLabel[L]}
                    style={{ padding: "0.5rem 0.75rem", borderRadius: 8, border: "1px solid rgba(255,255,255,0.15)",
                             background: "transparent", color: "#e5f4ff" }}>
              <option value="">{STR.mailboxNone[L]}</option>
              {mailboxes.map((m) => (
                <option key={m.mailbox_id} value={String(m.mailbox_id)}>{m.masked_email}</option>
              ))}
            </select>
          )}
        <button onClick={() => void associate()} disabled={busy} style={btnStyle(true)}>
          {busy ? STR.working[L] : STR.associate[L]}
        </button>
      </div>

      {error && <p style={{ color: "#ff8a8a", fontSize: "0.85rem", marginTop: "0.5rem" }}>{error}</p>}
      <p style={{ color: "#6f86a6", fontSize: "0.75rem", marginTop: "0.75rem", lineHeight: 1.5 }}>{STR.privacy[L]}</p>
    </div>
  );
};

function brokerOf(contacts: BrokerContact[] | null): string {
  // The account's broker is the broker of any existing binding; for the first binding the member types the email and
  // the backend derives the (email, broker) pair from the broker field — default to the existing broker if present.
  return contacts && contacts.length ? contacts[0].broker : "";
}

function btnStyle(primary: boolean): React.CSSProperties {
  return {
    padding: "0.5rem 1rem", borderRadius: 999, fontSize: "0.85rem", fontWeight: 600, cursor: "pointer",
    border: primary ? "none" : "1px solid rgba(255,255,255,0.15)",
    background: primary ? "linear-gradient(135deg, #1e6fff 0%, #00d4ff 50%, #1e6fff 100%)" : "transparent",
    color: primary ? "#fff" : "#c2d5ff",
  };
}

export default AccountWithdrawalMonitoring;
