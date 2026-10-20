"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { WithdrawalHistory, WithdrawalRow, WithdrawalState } from "@/types/withdrawals";

/** WITHDRAWALS HISTORY — read-only, owner-private per-withdrawal list with broker + account selectors (PR2).
 *
 * Observation only: GuvFX never initiates, confirms, cancels or modifies a withdrawal. DARK-safe: the API returns
 * 404 while BROKER_WITHDRAWAL_UX_ENABLED is off, so the whole panel self-hides (renders null) until the Sponsor arms
 * the feature. Truthful by contract: "In progress" (pending) is shown distinctly from "Failed"; UNRESOLVED is a
 * visible state of its own; amounts are per-row AS OBSERVED (never summed); processing time shows "N/A" when a
 * withdrawal is not both requested AND completed (never a fabricated 0); account numbers are masked. Accurate empty
 * states (no withdrawals yet vs none for the chosen filter). */
type Lang = "en" | "ja";
const PATH = "/api/broker-intelligence/withdrawals/history/";

const STR = {
  title: { en: "Withdrawal history", ja: "出金履歴" },
  broker: { en: "Broker", ja: "ブローカー" },
  account: { en: "Account", ja: "口座" },
  all: { en: "All", ja: "すべて" },
  none: { en: "No withdrawals observed yet", ja: "出金はまだ検出されていません" },
  noneFilter: { en: "No withdrawals for this filter", ja: "この条件の出金はありません" },
  na: { en: "N/A", ja: "該当なし" },
  ref: { en: "ref", ja: "参照" },
  unresolvedNote: { en: "Unresolved — awaiting matching evidence", ja: "未解決 — 照合証跡待ち" },
  accountUnresolved: { en: "Account unresolved", ja: "口座未特定" },
  accountUnresolvedNote: {
    en: "Owner-attributed — this broker account is not registered in GuvFX",
    ja: "所有者に紐付け — このブローカー口座はGuvFXに未登録です",
  },
  demoBanner: {
    en: "DEMONSTRATION DATA — synthetic, for illustration only (not a genuine broker record)",
    ja: "デモ用データ — 合成データ、説明目的のみ（実際のブローカー記録ではありません）",
  },
};

const STATE_LABEL: Record<WithdrawalState, { en: string; ja: string; color: string }> = {
  pending: { en: "In progress", ja: "進行中", color: "#fbbf24" },
  completed: { en: "Completed", ja: "完了", color: "#5ad19a" },
  failed: { en: "Failed", ja: "失敗", color: "#e88" },
  unresolved: { en: "Unresolved", ja: "未解決", color: "#9fb0c7" },
};

const card: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.09)", borderRadius: 14, padding: "1.1rem 1.2rem",
  background: "rgba(12,18,40,0.6)",
};
const meta: React.CSSProperties = { fontSize: "0.72rem", color: "#8fa0b7" };
const select: React.CSSProperties = {
  padding: "0.35rem 0.5rem", borderRadius: 8, border: "1px solid rgba(255,255,255,0.12)",
  background: "rgba(8,12,28,0.7)", color: "#e9f4ff", fontSize: "0.8rem",
};

function humanDuration(seconds: number, lang: Lang): string {
  if (seconds < 90) return lang === "ja" ? `${seconds}秒` : `${seconds}s`;
  const mins = Math.round(seconds / 60);
  if (mins < 90) return lang === "ja" ? `${mins}分` : `${mins} min`;
  const hours = Math.round(seconds / 3600);
  if (hours < 48) return lang === "ja" ? `${hours}時間` : `${hours} h`;
  const days = Math.round(seconds / 86400);
  return lang === "ja" ? `${days}日` : `${days} d`;
}

export const WithdrawalsHistory: React.FC<{ lang?: Lang }> = ({ lang = "en" }) => {
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [data, setData] = useState<WithdrawalHistory | null>(null);
  const [hidden, setHidden] = useState(false);
  const [broker, setBroker] = useState("");
  const [accountId, setAccountId] = useState("");

  const load = useCallback(() => {
    const q = new URLSearchParams();
    if (broker) q.set("broker", broker);
    if (accountId) q.set("account_id", accountId);
    const qs = q.toString();
    apiFetch<WithdrawalHistory>(PATH + (qs ? `?${qs}` : ""), {})
      .then((d) => {
        if (d && Array.isArray((d as WithdrawalHistory).withdrawals)) setData(d);
        else setHidden(true);
      })
      .catch(() => setHidden(true));   // 404 => DARK; any error => hide (never a broken card)
  }, [broker, accountId]);

  useEffect(() => { load(); }, [load]);

  // The backend always returns the COMPLETE broker/account selector lists (collected from the member's full
  // withdrawal set, never narrowed by the active filters), so the dropdowns stay stable while filtering — no need
  // for separate state kept in sync via an effect.
  const brokers = useMemo(() => (data?.brokers ?? []), [data]);
  const accounts = useMemo(() => (data?.accounts ?? []), [data]);

  if (hidden || !data) return null;

  const rows = data.withdrawals;
  const everEmpty = !broker && !accountId && rows.length === 0;

  return (
    <div style={card} data-testid="withdrawals-history">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap",
        gap: 8, marginBottom: 12 }}>
        <div style={{ color: "#e9f4ff", fontSize: "0.95rem", fontWeight: 600 }}>
          <i className="ti ti-history" aria-hidden="true" style={{ marginRight: 6 }} />{STR.title[L]}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <select style={select} value={broker} onChange={(e) => { setBroker(e.target.value); setAccountId(""); }}
            data-testid="history-broker-select" aria-label={STR.broker[L]}>
            <option value="">{STR.broker[L]}: {STR.all[L]}</option>
            {brokers.map((b) => <option key={b} value={b}>{b}</option>)}
          </select>
          <select style={select} value={accountId} onChange={(e) => setAccountId(e.target.value)}
            data-testid="history-account-select" aria-label={STR.account[L]}>
            <option value="">{STR.account[L]}: {STR.all[L]}</option>
            {accounts.filter((a) => !broker || a.broker === broker).map((a) =>
              <option key={a.id} value={String(a.id)}>{a.broker} · {a.account_masked}</option>)}
          </select>
        </div>
      </div>

      {data.demonstration ? (
        <div data-testid="demo-banner" style={{ marginBottom: 10, padding: "6px 10px", borderRadius: 8,
          background: "rgba(201,162,74,0.14)", border: "1px solid rgba(201,162,74,0.5)", color: "#e7c77a",
          fontSize: "0.72rem", fontWeight: 600, letterSpacing: "0.02em" }}>
          ⚠ {STR.demoBanner[L]}
        </div>
      ) : null}

      {rows.length === 0 ? (
        <div style={{ ...meta, padding: "10px 0", textAlign: "center" }}>
          {everEmpty ? STR.none[L] : STR.noneFilter[L]}
        </div>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 8 }}>
          {rows.map((w: WithdrawalRow) => {
            const st = STATE_LABEL[w.state];
            return (
              <li key={w.id} data-testid="history-row" style={{ display: "flex", justifyContent: "space-between",
                alignItems: "center", gap: 10, padding: "8px 10px", borderRadius: 10,
                background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)" }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ color: "#e9f4ff", fontSize: "0.85rem" }}>
                    {w.amount !== null && w.currency ? `${w.amount} ${w.currency}` : STR.na[L]}
                    <span style={{ ...meta, marginLeft: 8 }}>
                      {w.broker} · {w.account_unresolved
                        ? <span style={{ color: "#c9a24a" }} title={STR.accountUnresolvedNote[L]}>⚠ {STR.accountUnresolved[L]}</span>
                        : w.account_masked}
                    </span>
                  </div>
                  <div style={meta}>
                    {w.state === "unresolved"
                      ? STR.unresolvedNote[L]
                      : (w.duration_seconds !== null
                          ? humanDuration(w.duration_seconds, L)
                          : STR.na[L])}
                    {w.broker_reference_id
                      ? ` · ${STR.ref[L]} ${w.broker_reference_id}${w.reference_correlated ? " ✓" : ""}`
                      : ""}
                  </div>
                </div>
                <span style={{ color: st.color, fontSize: "0.78rem", fontWeight: 600, whiteSpace: "nowrap" }}>
                  {st[L]}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
};

export default WithdrawalsHistory;
