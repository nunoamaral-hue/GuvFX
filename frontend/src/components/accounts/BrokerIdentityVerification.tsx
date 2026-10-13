"use client";

import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { IdentityListResponse, VerifyResult } from "@/types/broker-identity";

/** BROKER IDENTITY VERIFICATION — minimum member UI for the WP7 verify/bind capability.
 *
 * Lets a member bind a broker-registered email address to ONE of their OWN trading accounts, on composed proof
 * (connected-mailbox control + GuvFX account ownership). It NEVER initiates/confirms/modifies any financial action —
 * it only records a verified ownership attribution. DARK-safe: the API returns 404 while
 * BROKER_IDENTITY_VERIFICATION_ENABLED is off, so the whole panel HIDES itself (renders null) until the Sponsor arms
 * the feature — no half-built UI ever appears. Owner-private: only the member's own identities/mailboxes are shown,
 * always MASKED (the raw address is never returned by the API).
 */
type Lang = "en" | "ja";

type Account = {
  id: number;
  broker_name: string;
  account_number: string;
  masked_account_number?: string | null;
  is_demo?: boolean | null;
};

const IDENTITIES = "/api/broker-intelligence/identities/";
const VERIFY = "/api/broker-intelligence/identities/verify/";
const ACCOUNTS = "/api/trading/accounts/";

const STR = {
  title: { en: "Broker email identities", ja: "ブローカーメール認証" },
  intro: {
    en: "Verify that a broker-registered email belongs to one of your accounts. GuvFX never moves money — this only records a verified link.",
    ja: "ブローカーに登録したメールがあなたの口座のものであることを確認します。GuvFX は資金を移動しません。",
  },
  verified: { en: "Verified identities", ja: "認証済み" },
  none: { en: "No verified identities yet", ja: "認証済みの識別子はまだありません" },
  account: { en: "Account", ja: "口座" },
  email: { en: "Broker-registered email", ja: "ブローカー登録メール" },
  verify: { en: "Verify & bind", ja: "認証してひも付け" },
  verifying: { en: "Verifying…", ja: "確認中…" },
  ok: { en: "Verified and bound.", ja: "認証され、ひも付けられました。" },
  choose: { en: "Select an account…", ja: "口座を選択…" },
};

const MSG: Record<string, { en: string; ja: string }> = {
  mailbox_not_connected: {
    en: "No connected mailbox of yours matches this address. Connect that mailbox first.",
    ja: "この住所に一致する接続済みメールボックスがありません。先に接続してください。",
  },
  account_not_owned: { en: "That trading account is not one of yours.", ja: "その取引口座はあなたのものではありません。" },
  broker_mismatch: {
    en: "This address is registered at a different broker than the selected account.",
    ja: "このメールは選択した口座とは別のブローカーに登録されています。",
  },
  cross_user_conflict: { en: "This address is already verified to a different member.", ja: "このメールは別の会員に認証済みです。" },
  account_conflict: { en: "This address is already verified to a different account.", ja: "このメールは別の口座に認証済みです。" },
  retired_identity: { en: "This identity was retired for this broker.", ja: "この識別子はこのブローカーで廃止されています。" },
  invalid_input: { en: "Please choose an account and enter the email.", ja: "口座を選び、メールを入力してください。" },
};

const box: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.09)", borderRadius: 14, padding: "1.1rem 1.2rem",
  background: "rgba(12,18,40,0.6)",
};
const meta: React.CSSProperties = { fontSize: "0.72rem", color: "#8fa0b7" };
const input: React.CSSProperties = {
  width: "100%", padding: "0.5rem 0.6rem", borderRadius: 8, border: "1px solid rgba(255,255,255,0.12)",
  background: "rgba(8,12,28,0.7)", color: "#e9f4ff", fontSize: "0.85rem",
};

export const BrokerIdentityVerification: React.FC<{ lang?: Lang }> = ({ lang = "en" }) => {
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [data, setData] = useState<IdentityListResponse | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [hidden, setHidden] = useState(false);
  const [accountId, setAccountId] = useState<string>("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(() => {
    apiFetch<IdentityListResponse>(IDENTITIES, {})
      .then((d) => {
        if (d && Array.isArray(d.identities)) setData(d);
        else setHidden(true);
      })
      .catch((e: unknown) => {
        // 404 => feature DARK; any other error => also hide (never a broken card).
        setHidden(true);
        void e;
      });
  }, []);

  useEffect(() => {
    load();
    apiFetch<Account[]>(ACCOUNTS, {})
      .then((a) => setAccounts(Array.isArray(a) ? a : []))
      .catch(() => setAccounts([]));
  }, [load]);

  const selected = accounts.find((a) => String(a.id) === accountId);

  const onVerify = useCallback(async () => {
    if (!selected || !email.trim()) {
      setResult({ ok: false, text: MSG.invalid_input[L] });
      return;
    }
    setBusy(true);
    setResult(null);
    try {
      const r = await apiFetch<VerifyResult>(VERIFY, {
        method: "POST",
        body: JSON.stringify({ email: email.trim(), broker: selected.broker_name, trading_account_id: selected.id }),
      });
      if (r && (r as { ok?: boolean }).ok) {
        setResult({ ok: true, text: STR.ok[L] });
        setEmail("");
        load();
      } else {
        setResult({ ok: false, text: STR.none[L] });
      }
    } catch (e: unknown) {
      const err = e as { body?: { error?: string; message?: string }; message?: string };
      const code = err.body?.error || "";
      setResult({ ok: false, text: (code && MSG[code]?.[L]) || err.body?.message || err.message || STR.none[L] });
    } finally {
      setBusy(false);
    }
  }, [selected, email, L, load]);

  if (hidden || !data) return null;   // DARK / unavailable / not loaded -> render nothing

  const acctLabel = (a: Account) =>
    `${a.broker_name} · ${a.masked_account_number || a.account_number}${a.is_demo === false ? " (live)" : a.is_demo ? " (demo)" : ""}`;

  return (
    <div style={box} data-testid="broker-identity-panel">
      <div style={{ color: "#e9f4ff", fontSize: "0.95rem", fontWeight: 600, marginBottom: 4 }}>
        <i className="ti ti-mail-check" aria-hidden="true" style={{ marginRight: 6 }} />{STR.title[L]}
      </div>
      <div style={{ ...meta, marginBottom: 14 }}>{STR.intro[L]}</div>

      <div style={{ ...meta, marginBottom: 6 }}>{STR.verified[L]}</div>
      {data.identities.filter((i) => i.verified).length === 0 ? (
        <div style={{ ...meta, color: "#6c7a90", marginBottom: 14 }}>{STR.none[L]}</div>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: "0 0 14px" }}>
          {data.identities.filter((i) => i.verified).map((i) => (
            <li key={i.id} style={{ display: "flex", justifyContent: "space-between", padding: "4px 0",
              fontSize: "0.82rem", color: "#cfe0f5" }}>
              <span>{i.email_masked} · {i.broker}</span>
              <span style={{ color: "#5ad19a" }}><i className="ti ti-check" aria-hidden="true" /> ✓</span>
            </li>
          ))}
        </ul>
      )}

      <div style={{ display: "grid", gap: 8 }}>
        <div>
          <label style={{ ...meta, display: "block", marginBottom: 3 }}>{STR.account[L]}</label>
          <select style={input} value={accountId} onChange={(e) => setAccountId(e.target.value)}
            data-testid="identity-account-select" aria-label={STR.account[L]}>
            <option value="">{STR.choose[L]}</option>
            {accounts.map((a) => <option key={a.id} value={String(a.id)}>{acctLabel(a)}</option>)}
          </select>
        </div>
        <div>
          <label style={{ ...meta, display: "block", marginBottom: 3 }}>{STR.email[L]}</label>
          <input style={input} type="email" value={email} onChange={(e) => setEmail(e.target.value)}
            data-testid="identity-email-input" placeholder="you@example.com" autoComplete="off" />
        </div>
        <button onClick={onVerify} disabled={busy || !accountId || !email.trim()} data-testid="identity-verify-btn"
          style={{ ...input, cursor: busy ? "default" : "pointer", background: "rgba(90,209,154,0.14)",
            color: "#9ff0c6", border: "1px solid rgba(90,209,154,0.3)", fontWeight: 600,
            opacity: busy || !accountId || !email.trim() ? 0.5 : 1 }}>
          {busy ? STR.verifying[L] : STR.verify[L]}
        </button>
        {result && (
          <div data-testid="identity-result" style={{ fontSize: "0.8rem",
            color: result.ok ? "#5ad19a" : "#e8a0a0" }}>
            {result.text}
          </div>
        )}
      </div>
    </div>
  );
};

export default BrokerIdentityVerification;
