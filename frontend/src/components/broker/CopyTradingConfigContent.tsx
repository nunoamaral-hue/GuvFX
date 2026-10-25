"use client";

import React, { useEffect, useMemo, useState } from "react";
import { getCopyTradingConfig, saveCopyTradingConfig } from "@/lib/copy-trading-api";
import type {
  CopyTradingConfig, CopyTradingConfigSaveResult, EligibleAccount, ConfigPair,
} from "@/types/copy-trading";

/** Founder-only Copy Trading CONFIGURATION UX (/copy-trading). SHADOW — EXECUTION OFF.
 *
 * Designate exactly one master + one-or-more followers from the owner's own LIVE accounts and save the SHADOW
 * configuration. The page never enables execution: it posts to the owner-scoped, audited, idempotent config API,
 * which writes only CopierMaster/CopierFollower rows. Disconnected/stale accounts are never shown as "ready".
 */
type Lang = "en" | "ja";

const BANNER = "SHADOW — EXECUTION OFF";

const T = {
  title: { en: "Copy Trading — Configuration", ja: "コピートレード — 設定" },
  notAvailable: { en: "Copy Trading configuration is not available for your account.",
                  ja: "お客様の口座ではコピートレード設定は利用できません。" },
  loading: { en: "Loading…", ja: "読み込み中…" },
  explainTitle: { en: "What this does", ja: "この機能について" },
  master: { en: "Master account", ja: "マスター口座" },
  masterHint: { en: "Select exactly one account to observe.", ja: "観測する口座を1つ選択してください。" },
  followers: { en: "Follower accounts", ja: "フォロワー口座" },
  followersHint: { en: "Select one or more. The master cannot also be a follower.",
                   ja: "1つ以上選択してください。マスターはフォロワーにできません。" },
  sizing: { en: "SHADOW sizing (applies to selected followers)", ja: "シャドーサイズ設定（選択したフォロワーに適用）" },
  fixedLot: { en: "Fixed lot", ja: "固定ロット" },
  maxVol: { en: "Max volume", ja: "最大数量" },
  maxConc: { en: "Max concurrent", ja: "最大同時" },
  save: { en: "Save configuration", ja: "設定を保存" },
  confirmTitle: { en: "Confirm SHADOW configuration", ja: "シャドー設定の確認" },
  confirmBody: { en: "This saves an observe/compare SHADOW configuration. No follower broker order will be placed and execution stays OFF. Continue?",
                 ja: "これは監視／比較のシャドー設定を保存します。フォロワーへの発注は行われず、実行はオフのままです。続行しますか？" },
  confirm: { en: "Confirm & Save", ja: "確認して保存" },
  cancel: { en: "Cancel", ja: "キャンセル" },
  saved: { en: "Configuration saved (SHADOW — execution remains OFF).", ja: "設定を保存しました（シャドー — 実行はオフのまま）。" },
  ready: { en: "Ready", ja: "準備完了" },
  stale: { en: "Stale", ja: "古い" },
  noData: { en: "No data", ja: "データなし" },
  mapReady: { en: "Instrument map ready", ja: "銘柄マップ準備完了" },
  mapMissing: { en: "No verified instrument map", ja: "検証済み銘柄マップなし" },
  unavailableMeta: { en: "Unavailable (not provided by the broker feed)", ja: "利用不可（ブローカーフィード未提供）" },
  needMaster: { en: "Select a master account.", ja: "マスター口座を選択してください。" },
  needFollower: { en: "Select at least one follower.", ja: "フォロワーを1つ以上選択してください。" },
  operatorAttention: {
    en: "Operator attention required: a saved account in this configuration is no longer current (disconnected). It is shown for review and has not been silently changed or removed; execution remains OFF.",
    ja: "オペレーター対応が必要です：この設定に保存された口座が現在のものではなくなりました（切断済み）。確認のため表示しており、自動変更・削除は行っていません。実行はオフのままです。",
  },
} as const;

const panel: React.CSSProperties = {
  border: "1px solid rgba(74,179,255,0.15)", borderRadius: 12, padding: "1.1rem 1.3rem",
  background: "rgba(255,255,255,0.02)", marginTop: 14,
};
const muted: React.CSSProperties = { color: "#9db6d6", fontSize: "0.85rem" };

function acctState(a: EligibleAccount, L: Lang): { label: string; color: string } {
  if (a.monitoring_state === "monitored") return { label: T.ready[L], color: "#86efac" };
  if (a.monitoring_state === "stale") return { label: T.stale[L], color: "#fca5a5" };
  return { label: T.noData[L], color: "#9db6d6" };
}

function AccountLine({ a, L }: { a: EligibleAccount; L: Lang }) {
  const st = acctState(a, L);
  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <span style={{ fontWeight: 600 }}>#{a.account_id} · {a.broker}</span>
      <span style={muted}>{a.name || "—"}</span>
      <span style={muted}>{a.masked_login}</span>
      <span style={muted}>{a.currency || "—"}</span>
      <span style={{ fontSize: "0.72rem", fontWeight: 700, color: st.color }}>{st.label}</span>
      {!a.ready && <span style={{ fontSize: "0.68rem", color: "#fca5a5" }}>· not ready</span>}
    </span>
  );
}

export const CopyTradingConfigContent: React.FC<{ lang?: Lang }> = ({ lang = "en" }) => {
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [data, setData] = useState<CopyTradingConfig | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [master, setMaster] = useState<number | null>(null);
  const [followers, setFollowers] = useState<Set<number>>(new Set());
  const [fixedLot, setFixedLot] = useState("0.01");
  const [maxVol, setMaxVol] = useState("0.10");
  const [maxConc, setMaxConc] = useState("1");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<CopyTradingConfigSaveResult | null>(null);
  const [error, setError] = useState("");

  const load = () => {
    getCopyTradingConfig()
      .then((d) => {
        setData(d);
        if (d.config) {
          setMaster(d.config.master_account_id);
          setFollowers(new Set(d.config.followers.filter((f) => f.enabled).map((f) => f.follower_account_id)));
          const f0 = d.config.followers[0];
          if (f0?.fixed_lot) setFixedLot(f0.fixed_lot);
          if (f0?.max_volume) setMaxVol(f0.max_volume);
          if (f0?.max_concurrent_positions != null) setMaxConc(String(f0.max_concurrent_positions));
        }
        setLoaded(true);
      })
      .catch(() => { setData({ enabled: false, banner: BANNER }); setLoaded(true); });
  };
  useEffect(load, []);

  const pairByFollower = useMemo(() => {
    const m = new Map<number, ConfigPair>();
    (data?.config?.followers || []).forEach((f) => m.set(f.follower_account_id, f));
    return m;
  }, [data]);

  if (!loaded) return <div style={{ padding: "1.5rem" }}><p style={muted}>{T.loading[L]}</p></div>;

  const Banner = (
    <div data-testid="copier-config-banner"
         style={{ border: "1px solid rgba(239,68,68,0.45)", background: "rgba(239,68,68,0.10)", borderRadius: 10,
                  padding: "0.6rem 0.95rem", color: "#fca5a5", fontWeight: 800, letterSpacing: 0.4, fontSize: "0.9rem" }}>
      {BANNER}
    </div>
  );

  if (!data?.enabled) {
    return (
      <div style={{ padding: "1.5rem", maxWidth: 820 }}>
        <h2 style={{ margin: "0 0 0.8rem", fontSize: "1.3rem" }}>{T.title[L]}</h2>
        {Banner}
        <p style={{ ...muted, marginTop: 14 }}>{T.notAvailable[L]}</p>
      </div>
    );
  }

  const eligible = data.eligible_accounts || [];
  const toggleFollower = (id: number) => {
    setFollowers((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  // A master can never also be a follower: drop it from the selection if chosen as master.
  const onPickMaster = (id: number) => {
    setMaster(id);
    setFollowers((prev) => { const next = new Set(prev); next.delete(id); return next; });
  };

  const followerIds = [...followers].filter((id) => id !== master);
  const canSave = master != null && followerIds.length > 0 && !saving;

  const doSave = async () => {
    setSaving(true); setError(""); setResult(null);
    try {
      const res = await saveCopyTradingConfig({
        master_account_id: master as number,
        followers: followerIds.map((id) => ({
          account_id: id, sizing_mode: "FIXED_LOT", fixed_lot: fixedLot,
          max_volume: maxVol, max_concurrent_positions: Number(maxConc) || 1,
        })),
      });
      setResult(res);
      setConfirmOpen(false);
      load();   // reflect persisted state + fresh readiness
    } catch (e) {
      setError((e as Error)?.message || "save_failed");
      setConfirmOpen(false);
    } finally {
      setSaving(false);
    }
  };

  const fieldStyle: React.CSSProperties = {
    background: "rgba(255,255,255,0.04)", border: "1px solid rgba(74,179,255,0.2)", borderRadius: 7,
    color: "#e5f4ff", padding: "0.35rem 0.5rem", width: 90, fontSize: "0.85rem",
  };

  return (
    <div style={{ padding: "1.5rem", maxWidth: 940 }}>
      <h2 style={{ margin: "0 0 0.8rem", fontSize: "1.3rem" }}>{T.title[L]}</h2>
      {Banner}

      {data.config?.needs_operator_attention && (
        <div data-testid="copier-config-attention" role="alert"
             style={{ marginTop: 12, border: "1px solid rgba(251,191,36,0.5)", background: "rgba(251,191,36,0.10)",
                      borderRadius: 10, padding: "0.7rem 0.95rem", color: "#fde68a", fontSize: "0.84rem", lineHeight: 1.5 }}>
          ⚠ {T.operatorAttention[L]}
        </div>
      )}

      <div style={{ ...panel }}>
        <strong style={{ color: "#c2d5ff", fontSize: "0.9rem" }}>{T.explainTitle[L]}</strong>
        <p style={{ ...muted, margin: "0.4rem 0 0", lineHeight: 1.55 }}>{data.explain}</p>
      </div>

      {/* Master */}
      <div style={panel}>
        <div style={{ fontWeight: 700, marginBottom: 2 }}>{T.master[L]}</div>
        <div style={{ ...muted, marginBottom: 8, fontSize: "0.78rem" }}>{T.masterHint[L]}</div>
        {eligible.map((a) => (
          <label key={a.account_id} style={{ display: "flex", gap: 8, alignItems: "center", padding: "0.3rem 0", cursor: "pointer" }}>
            <input type="radio" name="copier-master" checked={master === a.account_id}
                   onChange={() => onPickMaster(a.account_id)} aria-label={`master ${a.account_id}`} />
            <AccountLine a={a} L={L} />
          </label>
        ))}
      </div>

      {/* Followers */}
      <div style={panel}>
        <div style={{ fontWeight: 700, marginBottom: 2 }}>{T.followers[L]}</div>
        <div style={{ ...muted, marginBottom: 8, fontSize: "0.78rem" }}>{T.followersHint[L]}</div>
        {eligible.map((a) => {
          const isMaster = a.account_id === master;
          const checked = followers.has(a.account_id) && !isMaster;
          const pair = pairByFollower.get(a.account_id);
          return (
            <div key={a.account_id} style={{ padding: "0.35rem 0", borderTop: "1px solid rgba(74,179,255,0.08)" }}>
              <label style={{ display: "flex", gap: 8, alignItems: "center", cursor: isMaster ? "not-allowed" : "pointer", opacity: isMaster ? 0.45 : 1 }}>
                <input type="checkbox" checked={checked} disabled={isMaster}
                       onChange={() => toggleFollower(a.account_id)} aria-label={`follower ${a.account_id}`} />
                <AccountLine a={a} L={L} />
                {isMaster && <span style={{ fontSize: "0.68rem", color: "#9db6d6" }}>· master</span>}
              </label>
              {checked && (
                <div style={{ marginLeft: 26, marginTop: 4, fontSize: "0.72rem", color: "#9db6d6", display: "flex", gap: 12, flexWrap: "wrap" }}>
                  <span style={{ color: pair?.instrument_map_ready ? "#86efac" : "#fca5a5" }}>
                    {pair?.instrument_map_ready ? `✓ ${T.mapReady[L]}` : `⚠ ${T.mapMissing[L]}`}
                  </span>
                  {(pair?.instrument_maps || []).filter((m) => m.master_symbol).map((m, i) => (
                    <span key={i}>{m.master_symbol} → {m.follower_symbol}</span>
                  ))}
                  {pair?.last_observed_event && <span>last: {pair.last_observed_event.event_type} {pair.last_observed_event.symbol}</span>}
                  {pair?.live_execution_blocked && <span style={{ color: "#fca5a5" }}>LIVE execution blocked</span>}
                  <span title={(pair?.unavailable_risk_metadata || []).join(", ")}>
                    margin/leverage: {T.unavailableMeta[L]}
                  </span>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Sizing */}
      <div style={panel}>
        <div style={{ fontWeight: 700, marginBottom: 8 }}>{T.sizing[L]}</div>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-end" }}>
          <label style={{ fontSize: "0.78rem", color: "#9db6d6" }}>{T.fixedLot[L]}<br />
            <input style={fieldStyle} value={fixedLot} onChange={(e) => setFixedLot(e.target.value)} aria-label="fixed lot" /></label>
          <label style={{ fontSize: "0.78rem", color: "#9db6d6" }}>{T.maxVol[L]}<br />
            <input style={fieldStyle} value={maxVol} onChange={(e) => setMaxVol(e.target.value)} aria-label="max volume" /></label>
          <label style={{ fontSize: "0.78rem", color: "#9db6d6" }}>{T.maxConc[L]}<br />
            <input style={fieldStyle} value={maxConc} onChange={(e) => setMaxConc(e.target.value)} aria-label="max concurrent" /></label>
        </div>
      </div>

      {/* Save */}
      <div style={{ marginTop: 16, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <button data-testid="copier-config-save" disabled={!canSave} onClick={() => setConfirmOpen(true)}
                style={{ background: canSave ? "#1d6fd6" : "rgba(255,255,255,0.08)", color: "#fff", border: "none",
                         borderRadius: 8, padding: "0.55rem 1.1rem", fontWeight: 700, cursor: canSave ? "pointer" : "not-allowed" }}>
          {T.save[L]}
        </button>
        {master == null && <span style={{ ...muted, color: "#fca5a5", fontSize: "0.78rem" }}>{T.needMaster[L]}</span>}
        {master != null && followerIds.length === 0 && <span style={{ ...muted, color: "#fca5a5", fontSize: "0.78rem" }}>{T.needFollower[L]}</span>}
        {error && <span style={{ color: "#fca5a5", fontSize: "0.78rem" }}>{error}</span>}
      </div>

      {result?.ok && (
        <div data-testid="copier-config-result" role="status"
             style={{ marginTop: 14, border: "1px solid rgba(34,197,94,0.35)", background: "rgba(34,197,94,0.08)",
                      borderRadius: 10, padding: "0.7rem 0.95rem", color: "#86efac", fontSize: "0.85rem" }}>
          {T.saved[L]} — created {result.audit.created.length}, enabled {result.audit.enabled.length},
          updated {result.audit.updated.length}, disabled {result.audit.disabled.length}.
        </div>
      )}

      {/* Confirmation step */}
      {confirmOpen && (
        <div role="dialog" aria-modal="true" aria-label={T.confirmTitle[L]}
             style={{ position: "fixed", inset: 0, background: "rgba(2,6,20,0.72)", display: "flex",
                      alignItems: "center", justifyContent: "center", zIndex: 1000, padding: 16 }}>
          <div style={{ maxWidth: 460, background: "#0b1020", border: "1px solid rgba(74,179,255,0.25)",
                        borderRadius: 14, padding: "1.3rem 1.5rem" }}>
            <h3 style={{ margin: "0 0 0.5rem" }}>{T.confirmTitle[L]}</h3>
            <div style={{ ...Banner.props.style, marginBottom: 10 }}>{BANNER}</div>
            <p style={{ ...muted, lineHeight: 1.55 }}>{T.confirmBody[L]}</p>
            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 14 }}>
              <button onClick={() => setConfirmOpen(false)} disabled={saving}
                      style={{ background: "transparent", color: "#9db6d6", border: "1px solid rgba(255,255,255,0.15)", borderRadius: 8, padding: "0.45rem 0.9rem", cursor: "pointer" }}>
                {T.cancel[L]}
              </button>
              <button data-testid="copier-config-confirm" onClick={() => void doSave()} disabled={saving}
                      style={{ background: "#1d6fd6", color: "#fff", border: "none", borderRadius: 8, padding: "0.45rem 1.1rem", fontWeight: 700, cursor: "pointer" }}>
                {saving ? T.loading[L] : T.confirm[L]}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default CopyTradingConfigContent;
