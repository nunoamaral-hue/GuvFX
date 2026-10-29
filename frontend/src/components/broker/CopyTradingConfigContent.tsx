"use client";

import React, { useEffect, useMemo, useState } from "react";
import { getCopyTradingConfig, saveCopyTradingConfig } from "@/lib/copy-trading-api";
import type {
  CopyTradingConfig, CopyTradingConfigSaveResult, EligibleAccount, ConfigPair, ExecutionStatus,
} from "@/types/copy-trading";

/** Founder-only Copy Trading CONFIGURATION UX (/copy-trading). Observation mode — copy execution OFF.
 *
 * Designate exactly one master + one-or-more followers from the owner's own LIVE accounts and give EACH follower
 * its OWN sizing. The page never enables execution: it posts to the owner-scoped, audited, idempotent config API,
 * which writes only CopierMaster/CopierFollower rows. Disconnected/stale accounts are never shown as "ready".
 *
 * Per-follower sizing (Workstream A): every selected follower has independent fixed-lot / max-volume / max-concurrent
 * inputs; one follower's sizing is never applied to another. The backend validates each value against THAT
 * follower's own broker-observed symbol constraints and fails closed ("not execution-ready") when they are missing.
 * Instrument-map evidence (Workstream B) shows the source symbol, follower symbol, verification state and time.
 * Execution status (Workstream C) is a quiet, factual treatment — not a full-width alarm banner.
 */
type Lang = "en" | "ja";

const DEFAULT_STATUS: ExecutionStatus = {
  mode: "observation", execution: "off", label: "Observation mode",
  detail: "Copy execution off", note: "Saving configuration does not activate trading.",
};

type FollowerSizing = { fixed_lot: string; max_volume: string; max_concurrent: string };
const NEW_FOLLOWER_SIZING: FollowerSizing = { fixed_lot: "0.01", max_volume: "0.10", max_concurrent: "1" };

const T = {
  title: { en: "Copy Trading — Configuration", ja: "コピートレード — 設定" },
  notAvailable: { en: "Copy Trading configuration is not available for your account.",
                  ja: "お客様の口座ではコピートレード設定は利用できません。" },
  loading: { en: "Loading…", ja: "読み込み中…" },
  statusLabel: { en: "Observation mode", ja: "観測モード" },
  statusDetail: { en: "Copy execution off", ja: "コピー実行はオフ" },
  statusNote: { en: "Saving configuration does not activate trading.", ja: "設定を保存してもトレードは開始されません。" },
  explainTitle: { en: "What this does", ja: "この機能について" },
  master: { en: "Master account", ja: "マスター口座" },
  masterHint: { en: "Select exactly one account to observe.", ja: "観測する口座を1つ選択してください。" },
  followers: { en: "Follower accounts", ja: "フォロワー口座" },
  followersHint: { en: "Select one or more. Each follower is sized independently. The master cannot also be a follower.",
                   ja: "1つ以上選択してください。各フォロワーは個別にサイズ設定します。マスターはフォロワーにできません。" },
  followerSizing: { en: "Follower sizing", ja: "フォロワーのサイズ設定" },
  fixedLot: { en: "Fixed lot", ja: "固定ロット" },
  maxVol: { en: "Max volume", ja: "最大数量" },
  maxConc: { en: "Max concurrent", ja: "最大同時" },
  configured: { en: "Configured", ja: "設定値" },
  brokerObserved: { en: "Broker-observed", ja: "ブローカー観測値" },
  brokerMin: { en: "min", ja: "最小" },
  brokerStep: { en: "step", ja: "刻み" },
  brokerMax: { en: "max", ja: "最大" },
  contract: { en: "contract", ja: "契約サイズ" },
  save: { en: "Save configuration", ja: "設定を保存" },
  confirmTitle: { en: "Confirm configuration", ja: "設定の確認" },
  confirmBody: { en: "This saves an observe/compare configuration. No follower broker order will be placed and execution stays OFF. Continue?",
                 ja: "これは監視／比較の設定を保存します。フォロワーへの発注は行われず、実行はオフのままです。続行しますか？" },
  confirm: { en: "Confirm & Save", ja: "確認して保存" },
  cancel: { en: "Cancel", ja: "キャンセル" },
  saved: { en: "Configuration saved (observation only — execution remains OFF).", ja: "設定を保存しました（観測のみ — 実行はオフのまま）。" },
  ready: { en: "Ready", ja: "準備完了" },
  stale: { en: "Stale", ja: "古い" },
  noData: { en: "No data", ja: "データなし" },
  mapVerified: { en: "Instrument map verified", ja: "銘柄マップ検証済み" },
  configValid: { en: "Configuration valid · observation only", ja: "設定は有効 · 観測のみ" },
  notExecReady: { en: "Not execution-ready", ja: "実行準備未完了" },
  liveData: { en: "Live data", ja: "ライブデータ" },
  noLiveData: { en: "no live data", ja: "ライブデータなし" },
  freshnessNote: {
    en: "Monitoring freshness only — this is not Copy-Trading readiness. A pair is ready only when its mapping is verified, broker constraints are available and sizing is valid (shown per follower below).",
    ja: "監視データの鮮度のみ — コピートレードの準備状況ではありません。ペアが準備完了となるのは、マッピングが検証済みで、ブローカー制約が取得でき、サイズ設定が有効な場合のみです（下のフォロワーごとに表示）。",
  },
  copyReady: { en: "Copy-ready (SHADOW)", ja: "コピー可（シャドー）" },
  awaitingData: { en: "Config valid — awaiting live data", ja: "設定有効 — ライブデータ待ち" },
  dimSnapshot: { en: "Snapshot fresh", ja: "スナップショット鮮度" },
  dimMapped: { en: "Direction mapped", ja: "方向マップ" },
  dimVerified: { en: "Mapping verified", ja: "マップ検証" },
  dimConstraints: { en: "Constraints", ja: "制約" },
  dimSizing: { en: "Sizing valid", ja: "サイズ有効" },
  dimShadow: { en: "SHADOW ready", ja: "シャドー準備" },
  dimLive: { en: "LIVE blocked", ja: "ライブ遮断" },
  lastVerified: { en: "verified", ja: "検証" },
  unavailableMeta: { en: "Unavailable (not provided by the broker feed)", ja: "利用不可（ブローカーフィード未提供）" },
  needMaster: { en: "Select a master account.", ja: "マスター口座を選択してください。" },
  needFollower: { en: "Select at least one follower.", ja: "フォロワーを1つ以上選択してください。" },
  operatorAttention: {
    en: "Operator attention required: a saved account in this configuration is no longer current (disconnected). It is shown for review and has not been silently changed or removed; execution remains OFF.",
    ja: "オペレーター対応が必要です：この設定に保存された口座が現在のものではなくなりました（切断済み）。確認のため表示しており、自動変更・削除は行っていません。実行はオフのままです。",
  },
} as const;

// config_blocked_reason → human text. Covers the fail-closed states (missing/unverified map, un-synced constraints,
// disconnected follower) and the "sizing_invalid: …" family (keeps the backend's precise message as the tail).
function blockedReasonText(reason: string | null | undefined, L: Lang): string {
  if (!reason) return "";
  if (reason.startsWith("sizing_invalid:")) {
    const detail = reason.slice("sizing_invalid:".length).trim();
    return L === "ja" ? `設定ロットが執行不可: ${detail}` : `Configured lot not executable: ${detail}`;
  }
  const map: Record<string, { en: string; ja: string }> = {
    follower_disconnected: { en: "Follower disconnected — operator attention required", ja: "フォロワー切断 — オペレーター対応が必要" },
    no_symbol_map: { en: "No verified instrument map for this broker pair", ja: "このブローカー組には検証済み銘柄マップがありません" },
    map_present_but_unverified: { en: "Instrument map present but not verified", ja: "銘柄マップはあるが未検証" },
    follower_instruments_unsynced: { en: "Follower symbol constraints not synced", ja: "フォロワーの銘柄制約が未同期" },
    symbol_constraints_unavailable: { en: "Symbol constraints unavailable", ja: "銘柄制約が利用不可" },
    identity_mapping_symbol_set_not_enumerated: { en: "Same-broker identity mapping (symbol set not enumerated)", ja: "同一ブローカーの恒等マッピング（銘柄集合は未列挙）" },
  };
  const hit = map[reason];
  return hit ? hit[L] : reason;
}

const panel: React.CSSProperties = {
  border: "1px solid rgba(74,179,255,0.15)", borderRadius: 12, padding: "1.1rem 1.3rem",
  background: "rgba(255,255,255,0.02)", marginTop: 14,
};
const muted: React.CSSProperties = { color: "#9db6d6", fontSize: "0.85rem" };

// Freshness label ONLY — never "Ready" (which conflated data freshness with copy-readiness). Copy-readiness is
// a separate per-follower verdict rendered below.
function acctState(a: EligibleAccount, L: Lang): { label: string; color: string } {
  if (a.monitoring_state === "monitored") return { label: T.liveData[L], color: "#86efac" };
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
      {!(a.data_fresh ?? a.ready) && <span style={{ fontSize: "0.68rem", color: "#fca5a5" }}>· {T.noLiveData[L]}</span>}
    </span>
  );
}

// Compact, professional execution-status treatment (Workstream C): a quiet factual chip, not an alarm banner.
function ExecutionStatusChip({ status, L }: { status: ExecutionStatus; L: Lang }) {
  const label = L === "ja" ? T.statusLabel.ja : (status.label || T.statusLabel.en);
  const detail = L === "ja" ? T.statusDetail.ja : (status.detail || T.statusDetail.en);
  const note = L === "ja" ? T.statusNote.ja : (status.note || T.statusNote.en);
  return (
    <div data-testid="copier-config-status" role="status"
         style={{ marginTop: 10, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap",
                  border: "1px solid rgba(148,163,184,0.28)", background: "rgba(148,163,184,0.08)",
                  borderRadius: 999, padding: "0.35rem 0.85rem", width: "fit-content" }}>
      <span aria-hidden style={{ width: 8, height: 8, borderRadius: 999, background: "#94a3b8", display: "inline-block" }} />
      <span style={{ fontWeight: 700, fontSize: "0.82rem", color: "#cbd5e1" }}>{label}</span>
      <span style={{ color: "#64748b" }}>·</span>
      <span style={{ fontSize: "0.82rem", color: "#94a3b8" }}>{detail}</span>
      <span style={{ fontSize: "0.72rem", color: "#64748b" }}>— {note}</span>
    </div>
  );
}

export const CopyTradingConfigContent: React.FC<{ lang?: Lang }> = ({ lang = "en" }) => {
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [data, setData] = useState<CopyTradingConfig | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [master, setMaster] = useState<number | null>(null);
  const [followers, setFollowers] = useState<Set<number>>(new Set());
  const [sizing, setSizing] = useState<Record<number, FollowerSizing>>({});
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
          // Each follower keeps its OWN stored sizing — never a single shared value.
          const next: Record<number, FollowerSizing> = {};
          d.config.followers.forEach((f) => {
            next[f.follower_account_id] = {
              fixed_lot: f.fixed_lot ?? NEW_FOLLOWER_SIZING.fixed_lot,
              max_volume: f.max_volume ?? NEW_FOLLOWER_SIZING.max_volume,
              max_concurrent: f.max_concurrent_positions != null ? String(f.max_concurrent_positions) : NEW_FOLLOWER_SIZING.max_concurrent,
            };
          });
          setSizing(next);
        }
        setLoaded(true);
      })
      .catch(() => { setData({ enabled: false, banner: "" }); setLoaded(true); });
  };
  useEffect(load, []);

  const pairByFollower = useMemo(() => {
    const m = new Map<number, ConfigPair>();
    (data?.config?.followers || []).forEach((f) => m.set(f.follower_account_id, f));
    return m;
  }, [data]);

  if (!loaded) return <div style={{ padding: "1.5rem" }}><p style={muted}>{T.loading[L]}</p></div>;

  const status = data?.execution_status || DEFAULT_STATUS;

  if (!data?.enabled) {
    return (
      <div style={{ padding: "1.5rem", maxWidth: 820 }}>
        <h2 style={{ margin: "0 0 0.4rem", fontSize: "1.3rem" }}>{T.title[L]}</h2>
        <ExecutionStatusChip status={status} L={L} />
        <p style={{ ...muted, marginTop: 14 }}>{T.notAvailable[L]}</p>
      </div>
    );
  }

  const eligible = data.eligible_accounts || [];
  const ensureSizing = (id: number) => setSizing((prev) => (prev[id] ? prev : { ...prev, [id]: { ...NEW_FOLLOWER_SIZING } }));
  const setFollowerField = (id: number, field: keyof FollowerSizing, value: string) =>
    setSizing((prev) => ({ ...prev, [id]: { ...(prev[id] || NEW_FOLLOWER_SIZING), [field]: value } }));

  const toggleFollower = (id: number) => {
    setFollowers((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else { next.add(id); ensureSizing(id); }
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
        followers: followerIds.map((id) => {
          const s = sizing[id] || NEW_FOLLOWER_SIZING;   // each follower sends its OWN sizing
          return {
            account_id: id, sizing_mode: "FIXED_LOT", fixed_lot: s.fixed_lot,
            max_volume: s.max_volume, max_concurrent_positions: Number(s.max_concurrent) || 1,
          };
        }),
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

  const renderFollowerDetail = (id: number, pair?: ConfigPair) => {
    const s = sizing[id] || NEW_FOLLOWER_SIZING;
    const mapStatus = pair?.instrument_map;
    const observed = pair?.observed_constraints;
    const blocked = pair?.config_status === "blocked";
    return (
      <div style={{ marginLeft: 26, marginTop: 8, display: "flex", flexDirection: "column", gap: 8 }}>
        {/* Per-follower sizing — independent inputs, one follower never sizes another */}
        <div data-testid={`copier-sizing-${id}`} style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "flex-end" }}>
          <span style={{ fontSize: "0.72rem", fontWeight: 700, color: "#c2d5ff", alignSelf: "center" }}>{T.followerSizing[L]}</span>
          <label style={{ fontSize: "0.72rem", color: "#9db6d6" }}>{T.fixedLot[L]}<br />
            <input style={fieldStyle} value={s.fixed_lot} onChange={(e) => setFollowerField(id, "fixed_lot", e.target.value)}
                   aria-label={`fixed lot ${id}`} /></label>
          <label style={{ fontSize: "0.72rem", color: "#9db6d6" }}>{T.maxVol[L]}<br />
            <input style={fieldStyle} value={s.max_volume} onChange={(e) => setFollowerField(id, "max_volume", e.target.value)}
                   aria-label={`max volume ${id}`} /></label>
          <label style={{ fontSize: "0.72rem", color: "#9db6d6" }}>{T.maxConc[L]}<br />
            <input style={fieldStyle} value={s.max_concurrent} onChange={(e) => setFollowerField(id, "max_concurrent", e.target.value)}
                   aria-label={`max concurrent ${id}`} /></label>
        </div>

        {/* Instrument-map evidence (Workstream B): source symbol → follower symbol, verification state + time */}
        <div style={{ fontSize: "0.72rem", color: "#9db6d6", display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
          {mapStatus?.ready
            ? <span style={{ color: "#86efac", fontWeight: 700 }}>✓ {T.mapVerified[L]}</span>
            : <span style={{ color: "#fca5a5", fontWeight: 700 }}>⚠ {blockedReasonText(mapStatus?.failure_reason, L)}</span>}
          {(mapStatus?.pairs || []).map((p, i) => (
            <span key={i} style={{ fontFamily: "ui-monospace, monospace" }}>
              {p.master_symbol} → {p.follower_symbol}
              {p.verified && p.verified_at && <span style={{ color: "#64748b" }}> · {T.lastVerified[L]} {p.verified_at.slice(0, 10)}</span>}
            </span>
          ))}
        </div>

        {/* Configured vs broker-observed — kept explicitly distinct (Workstream A) */}
        <div style={{ fontSize: "0.72rem", color: "#9db6d6", display: "flex", gap: 14, flexWrap: "wrap" }}>
          <span><strong style={{ color: "#c2d5ff" }}>{T.configured[L]}:</strong> {T.fixedLot[L]} {s.fixed_lot} · {T.maxVol[L]} {s.max_volume}</span>
          {observed?.available
            ? Object.entries(observed.symbols).map(([sym, c]) => (
                <span key={sym} style={{ fontFamily: "ui-monospace, monospace" }}>
                  <strong style={{ color: "#c2d5ff" }}>{T.brokerObserved[L]}</strong> {sym}: {T.brokerMin[L]} {c.volume_min ?? "—"} · {T.brokerStep[L]} {c.volume_step ?? "—"} · {T.brokerMax[L]} {c.volume_max ?? "—"}
                  {c.contract_size != null && <> · {T.contract[L]} {c.contract_size}{c.currency_profit ? ` ${c.currency_profit}` : ""}</>}
                </span>
              ))
            : <span style={{ color: "#fbbf24" }}>{T.brokerObserved[L]}: {blockedReasonText(observed?.reason, L) || T.unavailableMeta[L]}</span>}
        </div>

        {/* Separated readiness dimensions — each an independent truth, so the UI shows WHICH condition is unmet
            instead of a single conflated "Ready". Copy-readiness ⇔ every copy dimension holds. */}
        {pair?.readiness && (
          <div data-testid={`copier-readiness-${id}`} style={{ fontSize: "0.68rem", display: "flex", gap: 12, flexWrap: "wrap" }}>
            {([
              [T.dimSnapshot[L], pair.readiness.snapshot_fresh],
              [T.dimMapped[L], pair.readiness.direction_mapped],
              [T.dimVerified[L], pair.readiness.mapping_verified],
              [T.dimConstraints[L], pair.readiness.constraints_available],
              [T.dimSizing[L], pair.readiness.sizing_valid],
              [T.dimShadow[L], pair.readiness.shadow_ready],
            ] as Array<[string, boolean]>).map(([label, ok], i) => (
              <span key={i} style={{ color: ok ? "#86efac" : "#fbbf24" }}>{ok ? "✓" : "⚠"} {label}</span>
            ))}
            <span style={{ color: "#64748b" }}>⛔ {T.dimLive[L]}</span>
          </div>
        )}

        {/* Precise copy-readiness verdict — NEVER a conflated "Ready"/"valid": blocked→reason; config ok but no
            live data→awaiting; all dimensions→Copy-ready (SHADOW only). LIVE execution is always blocked. */}
        <div data-testid={`copier-config-verdict-${id}`} style={{ fontSize: "0.72rem", fontWeight: 700 }}>
          {blocked
            ? <span style={{ color: "#fbbf24" }}>⚠ {T.notExecReady[L]} — {blockedReasonText(pair?.config_blocked_reason, L)}</span>
            : (pair?.readiness?.shadow_ready
                ? <span style={{ color: "#86efac" }}>✓ {T.copyReady[L]}</span>
                : <span style={{ color: "#fbbf24" }}>○ {T.awaitingData[L]}</span>)}
          {pair?.live_execution_blocked && <span style={{ color: "#64748b", marginLeft: 10 }}>· LIVE execution blocked</span>}
        </div>
      </div>
    );
  };

  return (
    <div style={{ padding: "1.5rem", maxWidth: 940 }}>
      <h2 style={{ margin: "0 0 0.4rem", fontSize: "1.3rem" }}>{T.title[L]}</h2>
      <ExecutionStatusChip status={status} L={L} />

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
        <div data-testid="copier-freshness-note" style={{ ...muted, marginBottom: 8, fontSize: "0.72rem", color: "#9db6d6", fontStyle: "italic" }}>
          {T.freshnessNote[L]}
        </div>
        {eligible.map((a) => (
          <label key={a.account_id} style={{ display: "flex", gap: 8, alignItems: "center", padding: "0.3rem 0", cursor: "pointer" }}>
            <input type="radio" name="copier-master" checked={master === a.account_id}
                   onChange={() => onPickMaster(a.account_id)} aria-label={`master ${a.account_id}`} />
            <AccountLine a={a} L={L} />
          </label>
        ))}
      </div>

      {/* Followers — each with its OWN sizing + map evidence + fail-closed verdict */}
      <div style={panel}>
        <div style={{ fontWeight: 700, marginBottom: 2 }}>{T.followers[L]}</div>
        <div style={{ ...muted, marginBottom: 8, fontSize: "0.78rem" }}>{T.followersHint[L]}</div>
        {eligible.map((a) => {
          const isMaster = a.account_id === master;
          const checked = followers.has(a.account_id) && !isMaster;
          const pair = pairByFollower.get(a.account_id);
          return (
            <div key={a.account_id} style={{ padding: "0.5rem 0", borderTop: "1px solid rgba(74,179,255,0.08)" }}>
              <label style={{ display: "flex", gap: 8, alignItems: "center", cursor: isMaster ? "not-allowed" : "pointer", opacity: isMaster ? 0.45 : 1 }}>
                <input type="checkbox" checked={checked} disabled={isMaster}
                       onChange={() => toggleFollower(a.account_id)} aria-label={`follower ${a.account_id}`} />
                <AccountLine a={a} L={L} />
                {isMaster && <span style={{ fontSize: "0.68rem", color: "#9db6d6" }}>· master</span>}
              </label>
              {checked && renderFollowerDetail(a.account_id, pair)}
            </div>
          );
        })}
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
            <div style={{ marginBottom: 10 }}><ExecutionStatusChip status={status} L={L} /></div>
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
