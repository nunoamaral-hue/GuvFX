"use client";

import React, { useEffect, useState } from "react";
import { getCopyTradingOverview } from "@/lib/copy-trading-api";
import type {
  CopyTradingFollower,
  CopyTradingOverview,
  CopyTradingReplication,
} from "@/types/copy-trading";

/** COPY TRADING — read-only customer monitoring dashboard (ADR-0051, PR C).
 *
 * Shows one master account's copy-trading state: the master, each selected follower (opt-in + sizing +
 * policy + sync status), the observed master trade events, and the per-follower replication outcomes —
 * plus the VISIBLE execution limitations (LIVE follower execution is blocked). It is display-only: it
 * never places, modifies or closes a trade. DARK-safe: when the API reports enabled:false (or 404s) the
 * page shows a calm "not enabled" state. Owner-private: the API only ever returns this member's data.
 */
type Lang = "en" | "ja";

const STR = {
  title: { en: "Copy Trading", ja: "コピートレード" },
  intro: {
    en: "Monitor how this master account's trades are copied to your selected follower accounts.",
    ja: "このマスター口座の取引が、選択したフォロワー口座へどのようにコピーされるかを監視します。",
  },
  configNote: {
    en: "Monitoring view only — follower selection and sizing changes are not available here yet.",
    ja: "監視専用ビューです — フォロワーの選択やサイズ変更はここではまだ行えません。",
  },
  shadowLegend: {
    en: "SHADOW — NO ORDER SENT means an intent was recorded; no broker order was placed. Green marks broker-verified execution only.",
    ja: "「シャドー — 発注なし」は意図を記録しただけで、ブローカーへの発注は行っていないことを意味します。緑はブローカー確認済みの実行のみを示します。",
  },
  notEnabled: {
    en: "Copy Trading is not enabled for your account yet.",
    ja: "お客様の口座ではコピートレードはまだ有効化されていません。",
  },
  notMaster: {
    en: "This account is not configured as a copy-trading master.",
    ja: "この口座はコピートレードのマスターとして設定されていません。",
  },
  limitationsTitle: { en: "Execution limitations", ja: "実行上の制限" },
  master: { en: "Master account", ja: "マスター口座" },
  observing: { en: "Observing", ja: "監視中" },
  notObserving: { en: "Not observing", ja: "監視停止中" },
  lastObserved: { en: "Last observed", ja: "最終監視" },
  followers: { en: "Followers", ja: "フォロワー" },
  noFollowers: { en: "No follower accounts selected yet.", ja: "フォロワー口座はまだ選択されていません。" },
  enabled: { en: "Enabled", ja: "有効" },
  disabled: { en: "Disabled", ja: "無効" },
  sizing: { en: "Sizing", ja: "サイズ設定" },
  policy: { en: "On disable", ja: "無効化時" },
  sync: { en: "Sync", ja: "同期" },
  liveBlocked: { en: "LIVE execution blocked", ja: "ライブ実行はブロック" },
  notReady: { en: "Not execution-ready", ja: "実行準備未完了" },
  demo: { en: "DEMO", ja: "デモ" },
  live: { en: "LIVE", ja: "ライブ" },
  events: { en: "Observed master events", ja: "観測されたマスターイベント" },
  noEvents: { en: "No observed events yet.", ja: "観測されたイベントはまだありません。" },
  replication: { en: "Replication status", ja: "レプリケーション状況" },
  noReplication: { en: "No replication activity yet.", ja: "レプリケーションの実績はまだありません。" },
  colEvent: { en: "Event", ja: "イベント" },
  colSymbol: { en: "Symbol", ja: "銘柄" },
  colSide: { en: "Side", ja: "売買" },
  colDelta: { en: "Δ Volume", ja: "数量変化" },
  colTime: { en: "Time", ja: "時刻" },
  colFollower: { en: "Follower", ja: "フォロワー" },
  colOp: { en: "Operation", ja: "操作" },
  colStatus: { en: "Status", ja: "状態" },
  colVol: { en: "Lots", ja: "ロット" },
  colSltp: { en: "SL / TP", ja: "SL / TP" },
  loading: { en: "Loading…", ja: "読み込み中…" },
} as const;

type BadgeColor = "green" | "gray" | "blue" | "red" | "yellow";

const BADGE_BG: Record<BadgeColor, string> = {
  green: "rgba(34,197,94,0.16)", gray: "rgba(148,163,184,0.16)", blue: "rgba(59,130,246,0.16)",
  red: "rgba(239,68,68,0.16)", yellow: "rgba(234,179,8,0.16)",
};
const BADGE_FG: Record<BadgeColor, string> = {
  green: "#86efac", gray: "#cbd5e1", blue: "#93c5fd", red: "#fca5a5", yellow: "#fde68a",
};

const Pill: React.FC<{ color: BadgeColor; children: React.ReactNode }> = ({ color, children }) => (
  <span style={{ display: "inline-block", padding: "0.1rem 0.55rem", borderRadius: 999, fontSize: "0.72rem",
                 fontWeight: 600, background: BADGE_BG[color], color: BADGE_FG[color] }}>{children}</span>
);

function statusColor(status: string): BadgeColor {
  // GREEN is reserved for BROKER-VERIFIED execution only. A recorded intent (SHADOW_RECORDED) and a
  // suppressed SHADOW JOB (SHADOW_JOB_RECORDED) must never read as a successful order (ADR-0051 UX).
  if (status === "EXECUTION_VERIFIED") return "green";
  if (status === "REJECTED" || status === "EXECUTION_FAILED") return "red";
  if (status === "SKIPPED") return "gray";
  if (status === "SHADOW_RECORDED" || status === "SHADOW_JOB_RECORDED") return "yellow";   // no order sent
  return "blue";                                           // e.g. EXECUTION_REQUESTED (in flight, not filled)
}

export function statusLabel(status: string, lang: Lang): string {
  if (status === "SHADOW_RECORDED") return lang === "ja" ? "シャドー — 発注なし" : "SHADOW — NO ORDER SENT";
  if (status === "SHADOW_JOB_RECORDED") {
    return lang === "ja" ? "シャドージョブ記録 — 発注なし" : "SHADOW JOB RECORDED — NO ORDER SENT";
  }
  if (status === "EXECUTION_REQUESTED") return lang === "ja" ? "実行要求（未確認）" : "EXECUTION REQUESTED";
  if (status === "EXECUTION_VERIFIED") return lang === "ja" ? "ブローカー確認済み" : "BROKER-VERIFIED";
  return status;
}

// config_blocked_reason → human text (mirrors CopyTradingConfigContent.blockedReasonText). Keeps the
// backend's precise "sizing_invalid: …" detail as the tail so an operator sees exactly why a configured lot
// is unexecutable (e.g. below the broker's volume_min). Only ever the NEGATIVE — never a positive claim.
function blockedReasonText(reason: string | null | undefined, L: Lang): string {
  if (!reason) return "";
  if (reason.startsWith("sizing_invalid:")) {
    const detail = reason.slice("sizing_invalid:".length).trim();
    return L === "ja" ? `設定ロットが執行不可: ${detail}` : `Configured lot not executable: ${detail}`;
  }
  const map: Record<string, { en: string; ja: string }> = {
    follower_disconnected: { en: "Follower disconnected", ja: "フォロワー切断" },
    no_symbol_map: { en: "No verified instrument map", ja: "検証済み銘柄マップなし" },
    map_present_but_unverified: { en: "Instrument map not verified", ja: "銘柄マップ未検証" },
    instrument_map_not_ready: { en: "Instrument map not ready", ja: "銘柄マップ未整備" },
    follower_instruments_unsynced: { en: "Follower constraints not synced", ja: "フォロワー制約が未同期" },
    symbol_constraints_unavailable: { en: "Symbol constraints unavailable", ja: "銘柄制約が利用不可" },
    identity_mapping_symbol_set_not_enumerated: { en: "Same-broker identity (symbol set not enumerated)", ja: "同一ブローカー恒等（銘柄集合未列挙）" },
  };
  const hit = map[reason];
  return hit ? hit[L] : reason;
}

function sizingSummary(f: CopyTradingFollower): string {
  switch (f.sizing_mode) {
    case "FIXED_LOT": return `Fixed ${f.fixed_lot ?? "—"}`;
    case "MULTIPLIER": return `× ${f.multiplier ?? "—"}`;
    case "BALANCE_PROPORTIONAL": return "Balance-proportional";
    case "RISK_BASED": return `Risk ${f.risk_per_trade_pct ?? "—"}%`;
    default: return f.sizing_mode;
  }
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    // Deterministic UTC format: identical on server and client, so no SSR/client locale+timezone
    // hydration mismatch (observed_at is already UTC from the backend).
    return new Date(iso).toISOString().slice(0, 16).replace("T", " ") + " UTC";
  } catch { return iso; }
}

function sltp(sl: string | null, tp: string | null): string {
  const s = sl && Number(sl) ? Number(sl) : null;
  const t = tp && Number(tp) ? Number(tp) : null;
  return `${s ?? "—"} / ${t ?? "—"}`;
}

const CELL: React.CSSProperties = { padding: "0.4rem 0.6rem", fontSize: "0.82rem", textAlign: "left" };
const HEAD: React.CSSProperties = { ...CELL, color: "#9db6d6", fontWeight: 600, borderBottom: "1px solid rgba(74,179,255,0.12)" };

export const CopyTradingContent: React.FC<{
  accountId: number;
  lang?: Lang;
  initialData?: CopyTradingOverview;   // preloaded data (SSR / preview harness) — skips the fetch
}> = ({ accountId, lang = "en", initialData }) => {
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [data, setData] = useState<CopyTradingOverview | null>(initialData ?? null);
  const [loaded, setLoaded] = useState(!!initialData);

  useEffect(() => {
    if (initialData) return;   // data supplied directly — do not fetch
    let active = true;   // unmount guard: never setState after teardown (prevents the #531-class CI failure)
    getCopyTradingOverview(accountId)
      .then((d) => { if (active) { setData(d); setLoaded(true); } })
      .catch(() => {
        // 404 / error => treat as DARK-off; show the calm not-enabled state, never a broken page.
        if (active) {
          setData({ enabled: false, master: null, followers: [], recent_events: [], recent_replications: [],
                    limitations: { live_follower_execution: "blocked", note: "" } });
          setLoaded(true);
        }
      });
    return () => { active = false; };
  }, [accountId, initialData]);

  const panel: React.CSSProperties = {
    marginTop: 18, border: "1px solid rgba(74,179,255,0.15)", borderRadius: 12,
    padding: "1.25rem 1.5rem", background: "rgba(255,255,255,0.02)",
  };

  // Permanent safety banner — the copier is observe/shadow only; no execution authority is reachable
  // from this UI. Kept as a fixed English label (matches the backend `banner` field) so it is always
  // unambiguous. Rendered on EVERY state including the pre-data loading frame (enabled, disabled,
  // not-configured, error, loading) so the SHADOW — EXECUTION OFF guarantee is never absent.
  const shadowBanner = data?.banner || "SHADOW — EXECUTION OFF";
  const bannerEl = (
    <div data-testid="copier-shadow-banner"
         style={{ border: "1px solid rgba(239,68,68,0.45)", background: "rgba(239,68,68,0.10)", borderRadius: 10,
                  padding: "0.55rem 0.9rem", marginBottom: "0.9rem", color: "#fca5a5", fontWeight: 700,
                  letterSpacing: 0.4, fontSize: "0.85rem" }}>
      {shadowBanner}
    </div>
  );

  if (!loaded) return <div style={panel}>{bannerEl}<p style={{ color: "#9db6d6", margin: 0 }}>{STR.loading[L]}</p></div>;

  return (
    <div style={panel}>
      {bannerEl}
      <h3 style={{ margin: "0 0 0.35rem", fontSize: "1.15rem" }}>{STR.title[L]}</h3>
      <p style={{ color: "#9db6d6", margin: "0 0 0.25rem", fontSize: "0.9rem", lineHeight: 1.5 }}>{STR.intro[L]}</p>
      <p style={{ color: "#6f86a6", margin: "0 0 1rem", fontSize: "0.8rem", fontStyle: "italic" }}>{STR.configNote[L]}</p>

      {/* Execution limitations — always visible and prominent. */}
      <div style={{ border: "1px solid rgba(234,179,8,0.35)", background: "rgba(234,179,8,0.08)", borderRadius: 10,
                    padding: "0.7rem 0.9rem", marginBottom: "1rem" }}>
        <strong style={{ color: "#fde68a", fontSize: "0.85rem" }}>⚠ {STR.limitationsTitle[L]}</strong>
        <div style={{ color: "#d9c88a", fontSize: "0.82rem", marginTop: 3 }}>
          {data?.limitations?.note ||
            (L === "ja"
              ? "ライブ・フォロワーの実行は有効化されていません。フォロワーは監視／シャドーのみで、実注文はデモ・フォロワーの個別承認済み経路でのみ行われます。"
              : "LIVE follower execution is not enabled. Followers run in observation/shadow mode; real orders occur only on DEMO followers via a separately authorized path.")}
        </div>
      </div>

      {!data?.enabled && <p style={{ color: "#9db6d6", fontSize: "0.9rem" }}>{STR.notEnabled[L]}</p>}

      {data?.enabled && !data.master && <p style={{ color: "#9db6d6", fontSize: "0.9rem" }}>{STR.notMaster[L]}</p>}

      {data?.enabled && data.master && (
        <>
          {/* Master */}
          <div style={{ marginBottom: "1rem" }}>
            <div style={{ color: "#9db6d6", fontSize: "0.75rem", textTransform: "uppercase", letterSpacing: 0.5 }}>
              {STR.master[L]}
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", marginTop: 4 }}>
              <span style={{ fontSize: "0.95rem" }}>#{data.master.account_id} · {data.master.broker || "—"}</span>
              <Pill color={data.master.environment === "live" ? "red" : "blue"}>
                {data.master.environment === "live" ? STR.live[L] : STR.demo[L]}
              </Pill>
              <Pill color={data.master.observe_enabled ? "green" : "gray"}>
                {data.master.observe_enabled ? STR.observing[L] : STR.notObserving[L]}
              </Pill>
              <span style={{ color: "#6f86a6", fontSize: "0.78rem" }}>
                {STR.lastObserved[L]}: {fmtTime(data.master.last_observed_at)}
              </span>
            </div>
          </div>

          {/* Followers */}
          <SectionTitle text={STR.followers[L]} />
          {data.followers.length === 0 && <p style={{ color: "#9db6d6", fontSize: "0.88rem" }}>{STR.noFollowers[L]}</p>}
          {data.followers.map((f) => (
            <div key={f.id} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "0.55rem",
                                     padding: "0.55rem 0", borderTop: "1px solid rgba(74,179,255,0.08)" }}>
              <span style={{ fontSize: "0.9rem" }}>#{f.follower_account_id} · {f.broker || "—"}</span>
              <Pill color={f.environment === "live" ? "red" : "blue"}>
                {f.environment === "live" ? STR.live[L] : STR.demo[L]}
              </Pill>
              <Pill color={f.enabled ? "green" : "gray"}>{f.enabled ? STR.enabled[L] : STR.disabled[L]}</Pill>
              <span style={{ color: "#9db6d6", fontSize: "0.8rem" }}>{STR.sizing[L]}: {sizingSummary(f)}</span>
              <span style={{ color: "#9db6d6", fontSize: "0.8rem" }}>{STR.policy[L]}: {f.disable_policy}</span>
              <span style={{ color: "#9db6d6", fontSize: "0.8rem" }}>{STR.sync[L]}: {f.sync_status}</span>
              {f.live_execution_blocked && <Pill color="red">{STR.liveBlocked[L]}</Pill>}
              {f.config_status === "blocked" && (
                <span data-testid="follower-not-ready">
                  <Pill color="yellow">
                    ⚠ {STR.notReady[L]}
                    {f.config_blocked_reason ? ` — ${blockedReasonText(f.config_blocked_reason, L)}` : ""}
                  </Pill>
                </span>
              )}
            </div>
          ))}

          {/* Observed events */}
          <SectionTitle text={STR.events[L]} />
          {data.recent_events.length === 0
            ? <p style={{ color: "#9db6d6", fontSize: "0.88rem" }}>{STR.noEvents[L]}</p>
            : (
              <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 4 }}>
                <thead><tr>
                  <th style={HEAD}>#</th><th style={HEAD}>{STR.colEvent[L]}</th><th style={HEAD}>{STR.colSymbol[L]}</th>
                  <th style={HEAD}>{STR.colSide[L]}</th><th style={HEAD}>{STR.colDelta[L]}</th><th style={HEAD}>{STR.colTime[L]}</th>
                </tr></thead>
                <tbody>
                  {data.recent_events.map((e) => (
                    <tr key={e.sequence}>
                      <td style={CELL}>{e.sequence}</td><td style={CELL}><Pill color="blue">{e.event_type}</Pill></td>
                      <td style={CELL}>{e.symbol}</td><td style={CELL}>{e.side}</td>
                      <td style={CELL}>{e.volume_delta}</td><td style={CELL}>{fmtTime(e.observed_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

          {/* Replication */}
          <SectionTitle text={STR.replication[L]} />
          {data.recent_replications.length === 0
            ? <p style={{ color: "#9db6d6", fontSize: "0.88rem" }}>{STR.noReplication[L]}</p>
            : (
              <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 4 }}>
                <thead><tr>
                  <th style={HEAD}>{STR.colEvent[L]}</th><th style={HEAD}>{STR.colFollower[L]}</th>
                  <th style={HEAD}>{STR.colOp[L]}</th><th style={HEAD}>{STR.colStatus[L]}</th>
                  <th style={HEAD}>{STR.colVol[L]}</th><th style={HEAD}>{STR.colSymbol[L]}</th><th style={HEAD}>{STR.colSltp[L]}</th>
                </tr></thead>
                <tbody>
                  {data.recent_replications.map((r: CopyTradingReplication) => (
                    <tr key={r.id}>
                      <td style={CELL}>#{r.event_sequence} {r.event_type}</td>
                      <td style={CELL}>#{r.follower_account_id}</td>
                      <td style={CELL}>{r.requested_op}</td>
                      <td style={CELL}>
                        <Pill color={statusColor(r.status)}>{statusLabel(r.status, L)}</Pill>
                        {r.reason ? <span style={{ color: "#c08a8a", fontSize: "0.72rem", marginLeft: 6 }}>{r.reason}</span> : null}
                      </td>
                      <td style={CELL}>{r.target_volume ?? "—"}</td>
                      <td style={CELL}>{r.follower_symbol || "—"}</td>
                      <td style={CELL}>{sltp(r.translated_sl, r.translated_tp)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          <p style={{ color: "#6f86a6", fontSize: "0.72rem", marginTop: "0.5rem", lineHeight: 1.5 }}>
            {STR.shadowLegend[L]}
          </p>
        </>
      )}
    </div>
  );
};

const SectionTitle: React.FC<{ text: string }> = ({ text }) => (
  <div style={{ margin: "1.1rem 0 0.2rem", color: "#c2d5ff", fontSize: "0.9rem", fontWeight: 600 }}>{text}</div>
);

export default CopyTradingContent;
