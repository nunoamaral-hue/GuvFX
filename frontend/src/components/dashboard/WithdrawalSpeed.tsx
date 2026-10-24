"use client";

import React, { useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { WithdrawalSpeed as Speed, SpeedSeriesPoint } from "@/types/withdrawals";

/** WITHDRAWAL SPEED (WS3) — read-only, owner-scoped "how fast are my withdrawals processed" analytics.
 *
 * Observation only: GuvFX never initiates, confirms, cancels or moves a withdrawal; this renders what was observed in
 * the member's authorized notification emails. DARK-safe: the endpoint returns 404 while the member is outside the
 * WS3 cohort (BROKER_WITHDRAWAL_SPEED_* ), so the whole section self-hides (renders null). Truthful by contract:
 * the "typical" figure is the MEDIAN of confidently-completed withdrawals and is presented as typical only at/above
 * the minimum sample (a 1–2 sample state is shown but labelled "not yet typical"); in-progress ages are LIVE and
 * never mixed into completed figures; durations are "observed email-processing time", not fund movement. Accessible:
 * the trend chart is matched by an equivalent data table and focusable points; nothing depends on colour alone.
 */
type Lang = "en" | "ja";
const PATH = "/api/broker-intelligence/withdrawals/speed/";

const STR = {
  title: { en: "Withdrawal processing speed", ja: "出金処理スピード" },
  typical: { en: "Typical withdrawal processing time", ja: "通常の出金処理時間" },
  median: { en: "median", ja: "中央値" },
  average: { en: "average", ja: "平均" },
  samples: { en: "completed observations", ja: "完了観測数" },
  observedFrom: {
    en: "Observed from notification emails — the interval between the request and completion emails, not fund arrival.",
    ja: "通知メールから観測 — 申請メールと完了メールの間隔であり、資金の着金ではありません。",
  },
  emptyTitle: { en: "Waiting for the first completed withdrawal", ja: "最初の完了出金を待っています" },
  empty: {
    en: "No completed withdrawal has been observed yet, so there is no processing time to show. As soon as a request and its completion are both observed, the typical time will appear here.",
    ja: "完了した出金はまだ観測されていないため、処理時間は表示できません。申請と完了の両方が観測され次第、通常の処理時間がここに表示されます。",
  },
  insufficient: {
    en: "Based on {n} observation(s) — shown for transparency, not yet a representative typical time (needs {min}).",
    ja: "{n}件の観測に基づく — 透明性のため表示していますが、代表的な通常時間にはまだ達していません（{min}件必要）。",
  },
  trendH: { en: "Processing time per completed withdrawal", ja: "完了出金ごとの処理時間" },
  trendEmpty: { en: "No completed withdrawals to chart yet.", ja: "まだグラフ化できる完了出金はありません。" },
  tableToggleShow: { en: "Show data table", ja: "データ表を表示" },
  tableToggleHide: { en: "Hide data table", ja: "データ表を隠す" },
  colDate: { en: "Completed", ja: "完了日時" },
  colBroker: { en: "Broker", ja: "ブローカー" },
  colDuration: { en: "Processing time", ja: "処理時間" },
  colMatch: { en: "Match", ja: "照合" },
  matchReference: { en: "reference", ja: "参照番号" },
  matchHeuristic: { en: "heuristic", ja: "推定" },
  brokersH: { en: "By broker", ja: "ブローカー別" },
  brokersNote: {
    en: "One member's observed emails — not an industry benchmark across all users.",
    ja: "一人のメンバーが観測したメールに基づくもので、全ユーザーの業界ベンチマークではありません。",
  },
  qInsufficient: { en: "Insufficient data", ja: "データ不足" },
  qLimited: { en: "Limited sample", ja: "サンプル少" },
  fastest: { en: "fastest", ja: "最速" },
  slowest: { en: "slowest", ja: "最遅" },
  nSamples: { en: "{n} completed", ja: "完了{n}件" },
  inProgressH: { en: "In progress", ja: "進行中" },
  inProgressNote: {
    en: "Live elapsed time since the request was observed — not counted in the typical time above.",
    ja: "申請を観測してからの経過時間（上記の通常時間には含まれません）。",
  },
  elapsed: { en: "elapsed", ja: "経過" },
  accountUnresolved: { en: "Account unresolved", ja: "口座未特定" },
  quarantined: {
    en: "{n} record(s) with an impossible time (completion before request) were excluded.",
    ja: "{n}件の不正な時間（申請前の完了）のレコードを除外しました。",
  },
  observedTiming: {
    en: "{n} completed withdrawal(s) have only observed (ingestion) timing and are not included in the typical time or chart.",
    ja: "{n}件の完了出金は観測（取り込み）時刻のみのため、通常時間やグラフには含めていません。",
  },
  untimed: {
    en: "{n} completed withdrawal(s) could not be timed (a request or completion email was not observed).",
    ja: "{n}件の完了出金は時間を計測できませんでした（申請または完了メールが観測されていません）。",
  },
  heuristicCaveat: {
    en: "Includes {n} match(es) correlated by amount, currency and timing rather than a broker reference.",
    ja: "{n}件はブローカー参照番号ではなく金額・通貨・タイミングで照合した一致を含みます。",
  },
  chartAria: {
    en: "Bar chart of processing time per completed withdrawal; an equivalent data table follows.",
    ja: "完了出金ごとの処理時間の棒グラフ。同等のデータ表が続きます。",
  },
};

const card: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.09)", borderRadius: 14, padding: "1.1rem 1.2rem",
  background: "rgba(12,18,40,0.6)",
};
const meta: React.CSSProperties = { fontSize: "0.72rem", color: "#8fa0b7" };
const statLabel: React.CSSProperties = { ...meta, marginBottom: 2 };
const h3: React.CSSProperties = { margin: "0 0 2px", fontSize: "0.82rem", color: "#dbe6f5", fontWeight: 600 };

// Accessible broker palette (sufficient contrast on the dark card). Colour is NEVER the only cue: the legend and the
// data table name every broker in text, and each chart bar carries its broker in its aria-label/title.
const PALETTE = ["#5ad19a", "#6aa8ff", "#f2c14e", "#c88bf0", "#e8886f", "#55c7d6", "#9db0c7"];

function humanDuration(seconds: number | null, lang: Lang): string {
  if (seconds === null || seconds === undefined) return lang === "ja" ? "該当なし" : "N/A";
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return lang === "ja" ? `${s}秒` : `${s}s`;
  const mins = Math.round(s / 60);
  if (mins < 90) return lang === "ja" ? `${mins}分` : `${mins} min`;
  const hours = Math.round(s / 3600);
  if (hours < 48) return lang === "ja" ? `${hours}時間` : `${hours} h`;
  const days = Math.round(s / 86400);
  return lang === "ja" ? `${days}日` : `${days} d`;
}

function shortDate(iso: string | null, lang: Lang): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  // Render in UTC (never the viewer's local zone) so every viewer sees the SAME instant the durations are derived
  // from, and label it "UTC" — consistent with the API's UTC timestamps and the "observed from emails" semantics.
  return d.toLocaleString(lang === "ja" ? "ja-JP" : "en-GB",
    { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) + " UTC";
}

/** Accessible, responsive SVG bar chart: one bar per completed withdrawal (x = completion order/date, y = duration).
 * role="img" + aria-label; each bar is focusable with its own aria-label + <title> (hover/focus). The matching data
 * table below is the colour-independent equivalent. No animation (reduced-motion safe by construction). */
const TrendChart: React.FC<{ series: SpeedSeriesPoint[]; colorFor: (b: string) => string; lang: Lang }>
  = ({ series, colorFor, lang }) => {
  const W = 680, H = 220, padL = 48, padR = 12, padT = 12, padB = 44;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const maxDur = Math.max(1, ...series.map((p) => p.duration_seconds));
  // "nice" top: round up to a tidy ceiling so the y-axis labels are readable.
  const niceTop = (() => {
    const pow = Math.pow(10, Math.floor(Math.log10(maxDur)));
    return Math.ceil(maxDur / pow) * pow || maxDur;
  })();
  const n = series.length;
  const bw = Math.min(56, (plotW / Math.max(1, n)) * 0.6);
  const step = plotW / Math.max(1, n);
  const yTicks = [0, niceTop / 2, niceTop];

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" preserveAspectRatio="xMidYMid meet"
         aria-label={STR.chartAria[lang]} data-testid="speed-trend-svg"
         style={{ display: "block", maxWidth: "100%" }}>
      {/* y grid + ticks (human-readable durations) */}
      {yTicks.map((t, i) => {
        const y = padT + plotH - (t / niceTop) * plotH;
        return (
          <g key={i}>
            <line x1={padL} y1={y} x2={W - padR} y2={y} stroke="rgba(255,255,255,0.08)" strokeWidth={1} />
            <text x={padL - 6} y={y + 3} textAnchor="end" fontSize={10} fill="#8fa0b7">
              {humanDuration(t, lang)}
            </text>
          </g>
        );
      })}
      {/* bars */}
      {series.map((p, i) => {
        const x = padL + step * i + (step - bw) / 2;
        const bh = (p.duration_seconds / niceTop) * plotH;
        const y = padT + plotH - bh;
        const label = `${p.broker || "—"}, ${shortDate(p.completed_at, lang)}, ${humanDuration(p.duration_seconds, lang)}`;
        // The SVG is a single atomic graphic (role="img" + aria-label on the <svg>); the colour-independent,
        // screen-reader + keyboard path is the equivalent data table below (toggled via a focusable button). Bars are
        // therefore NOT individually focusable (a focusable-but-pruned node would be reachable yet unannounced); the
        // <title> is a mouse-hover affordance only.
        return (
          <g key={p.withdrawal_id} data-testid="speed-bar">
            <title>{label}</title>
            <rect x={x} y={y} width={bw} height={Math.max(2, bh)} rx={3} fill={colorFor(p.broker || "")} />
          </g>
        );
      })}
      {/* x baseline + first/last date labels (avoid crowding) */}
      <line x1={padL} y1={padT + plotH} x2={W - padR} y2={padT + plotH} stroke="rgba(255,255,255,0.18)" strokeWidth={1} />
      {n > 0 && (
        <text x={padL} y={H - 24} textAnchor="start" fontSize={9} fill="#8fa0b7">
          {shortDate(series[0].completed_at, lang)}
        </text>
      )}
      {n > 1 && (
        <text x={W - padR} y={H - 24} textAnchor="end" fontSize={9} fill="#8fa0b7">
          {shortDate(series[n - 1].completed_at, lang)}
        </text>
      )}
    </svg>
  );
};

export const WithdrawalSpeed: React.FC<{ lang?: Lang }> = ({ lang = "en" }) => {
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [data, setData] = useState<Speed | null>(null);
  const [hidden, setHidden] = useState(false);
  const [showTable, setShowTable] = useState(false);

  useEffect(() => {
    let live = true;
    apiFetch<Speed>(PATH, {})
      .then((d) => {
        if (!live) return;
        if (d && (d as Speed).headline && (d as Speed).metric) setData(d);
        else setHidden(true);
      })
      .catch(() => { if (live) setHidden(true); });
    return () => { live = false; };
  }, []);

  const colorFor = useMemo(() => {
    const brokers = data ? Array.from(new Set(data.series.map((p) => p.broker || ""))) : [];
    const map = new Map<string, string>();
    brokers.forEach((b, i) => map.set(b, PALETTE[i % PALETTE.length]));
    return (b: string) => map.get(b) || PALETTE[PALETTE.length - 1];
  }, [data]);

  if (hidden) return null;
  if (!data) return null;

  const { headline, summary, series, brokers, in_progress } = data;
  const tmpl = (s: string, vars: Record<string, string | number>) =>
    s.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? ""));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.9rem" }} data-testid="withdrawal-speed">
      {/* A — prominent typical-processing-time card */}
      <div style={{ ...card, borderColor: "rgba(90,209,154,0.28)" }} data-testid="speed-headline">
        <div style={{ ...statLabel, color: "#7ee3b0" }}>
          <i className="ti ti-clock-bolt" aria-hidden="true" style={{ marginRight: 6 }} />{STR.typical[L]}
        </div>

        {headline.state === "empty" ? (
          <div data-testid="speed-empty" style={{ marginTop: 6 }}>
            <div style={{ color: "#e9f4ff", fontSize: "1rem", fontWeight: 600 }}>{STR.emptyTitle[L]}</div>
            <p style={{ ...meta, lineHeight: 1.6, margin: "6px 0 0" }}>{STR.empty[L]}</p>
          </div>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginTop: 4, flexWrap: "wrap" }}>
              <div style={{ fontSize: "2rem", fontWeight: 700, color: "#5ad19a", lineHeight: 1.1 }}
                   data-testid="speed-median">
                {humanDuration(headline.median_seconds, L)}
              </div>
              <div style={meta}>{STR.median[L]}</div>
              {headline.average_seconds !== null && (
                <div style={{ ...meta, marginLeft: 8 }} data-testid="speed-average">
                  {STR.average[L]}: {humanDuration(headline.average_seconds, L)}
                </div>
              )}
            </div>
            <div style={{ ...meta, marginTop: 4 }} data-testid="speed-sample-count">
              {headline.sample_count} {STR.samples[L]}
            </div>
            {headline.state === "insufficient" && (
              <div style={{ ...meta, color: "#c9b56b", marginTop: 6 }} data-testid="speed-insufficient">
                {tmpl(STR.insufficient[L], { n: headline.sample_count, min: headline.min_sample })}
              </div>
            )}
          </>
        )}
        {headline.state !== "empty" && headline.heuristic_count > 0 && (
          <p style={{ ...meta, color: "#c9b56b", margin: "6px 0 0" }} data-testid="speed-heuristic">
            {tmpl(STR.heuristicCaveat[L], { n: headline.heuristic_count })}
          </p>
        )}
        <p style={{ ...meta, lineHeight: 1.6, margin: "8px 0 0" }}>{STR.observedFrom[L]}</p>
        {summary.observed_timing_count > 0 && (
          <p style={{ ...meta, color: "#c9b56b", margin: "6px 0 0" }} data-testid="speed-observed-timing">
            {tmpl(STR.observedTiming[L], { n: summary.observed_timing_count })}
          </p>
        )}
        {summary.untimed_count > 0 && (
          <p style={{ ...meta, margin: "6px 0 0" }} data-testid="speed-untimed">
            {tmpl(STR.untimed[L], { n: summary.untimed_count })}
          </p>
        )}
        {summary.quarantined_impossible_count > 0 && (
          <p style={{ ...meta, color: "#c9b56b", margin: "6px 0 0" }} data-testid="speed-quarantined">
            {tmpl(STR.quarantined[L], { n: summary.quarantined_impossible_count })}
          </p>
        )}
      </div>

      {/* B — trend chart + accessible equivalent data table */}
      <div style={card} data-testid="speed-trend">
        <h3 style={h3}>{STR.trendH[L]}</h3>
        {series.length === 0 ? (
          <div style={{ ...meta, padding: "10px 0", textAlign: "center" }} data-testid="speed-trend-empty">
            {STR.trendEmpty[L]}
          </div>
        ) : (
          <>
            {/* legend — broker names in TEXT (never colour alone) */}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 12, margin: "4px 0 8px" }} data-testid="speed-legend">
              {Array.from(new Set(series.map((p) => p.broker || ""))).map((b) => (
                <span key={b || "—"} style={{ ...meta, display: "inline-flex", alignItems: "center", gap: 5 }}>
                  <span aria-hidden="true" style={{ width: 10, height: 10, borderRadius: 2, background: colorFor(b),
                    display: "inline-block" }} />
                  {b || "—"}
                </span>
              ))}
            </div>
            <TrendChart series={series} colorFor={colorFor} lang={L} />
            <button type="button" onClick={() => setShowTable((v) => !v)} data-testid="speed-table-toggle"
              style={{ ...meta, marginTop: 8, background: "none", border: "1px solid rgba(255,255,255,0.14)",
                borderRadius: 8, padding: "4px 8px", color: "#b8c7db", cursor: "pointer" }}
              aria-expanded={showTable}>
              {showTable ? STR.tableToggleHide[L] : STR.tableToggleShow[L]}
            </button>
            {showTable && (
              <table data-testid="speed-table" style={{ width: "100%", marginTop: 8, borderCollapse: "collapse",
                fontSize: "0.78rem", color: "#cfe0f5" }}>
                <caption style={{ ...meta, textAlign: "left", marginBottom: 4 }}>{STR.trendH[L]}</caption>
                <thead>
                  <tr style={{ textAlign: "left", color: "#8fa0b7" }}>
                    <th scope="col" style={{ padding: "3px 6px" }}>{STR.colDate[L]}</th>
                    <th scope="col" style={{ padding: "3px 6px" }}>{STR.colBroker[L]}</th>
                    <th scope="col" style={{ padding: "3px 6px" }}>{STR.colDuration[L]}</th>
                    <th scope="col" style={{ padding: "3px 6px" }}>{STR.colMatch[L]}</th>
                  </tr>
                </thead>
                <tbody>
                  {series.map((p) => (
                    <tr key={p.withdrawal_id} style={{ borderTop: "1px solid rgba(255,255,255,0.06)" }}>
                      <td style={{ padding: "3px 6px" }}>{shortDate(p.completed_at, L)}</td>
                      <td style={{ padding: "3px 6px" }}>
                        {p.broker || "—"}
                        {p.account_unresolved && <span style={{ ...meta, marginLeft: 6 }}>({STR.accountUnresolved[L]})</span>}
                      </td>
                      <td style={{ padding: "3px 6px" }}>{humanDuration(p.duration_seconds, L)}</td>
                      <td style={{ padding: "3px 6px" }}>
                        {p.match_basis === "reference" ? STR.matchReference[L] : STR.matchHeuristic[L]}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>

      {/* C — by-broker comparison (bar meters), designed for future broker-performance dimensions */}
      {brokers.length > 0 && (
        <div style={card} data-testid="speed-brokers">
          <h3 style={h3}>{STR.brokersH[L]}</h3>
          <p style={{ ...meta, margin: "0 0 10px" }}>{STR.brokersNote[L]}</p>
          {brokers.map((b) => {
            const top = Math.max(1, ...brokers.map((x) => x.slowest_seconds || 0));
            const pct = b.median_seconds !== null ? Math.max(3, (b.median_seconds / top) * 100) : 0;
            const quality = b.data_quality === "sufficient" ? null
              : (b.data_quality === "limited" ? STR.qLimited[L] : STR.qInsufficient[L]);
            return (
              <div key={b.broker || "—"} style={{ marginBottom: 12 }} data-testid="speed-broker-row">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                  <span style={{ color: "#e9f4ff", fontSize: "0.85rem" }}>{b.broker || "—"}</span>
                  <span style={meta}>{tmpl(STR.nSamples[L], { n: b.sample_count })}</span>
                </div>
                {b.data_quality === "sufficient" && b.median_seconds !== null ? (
                  <>
                    <div style={{ height: 8, borderRadius: 4, background: "rgba(255,255,255,0.07)", margin: "5px 0",
                      overflow: "hidden" }}>
                      <div style={{ width: `${pct}%`, height: "100%", borderRadius: 4, background: colorFor(b.broker || "") }} />
                    </div>
                    <div style={meta}>
                      {STR.median[L]}: {humanDuration(b.median_seconds, L)} · {STR.average[L]}: {humanDuration(b.average_seconds, L)}
                      {" · "}{STR.fastest[L]}: {humanDuration(b.fastest_seconds, L)} · {STR.slowest[L]}: {humanDuration(b.slowest_seconds, L)}
                    </div>
                  </>
                ) : (
                  <div style={{ ...meta, color: "#c9b56b", marginTop: 4 }} data-testid="speed-broker-lowdata">{quality}</div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* D — in-progress live ages (never mixed into completed figures) */}
      {in_progress.length > 0 && (
        <div style={card} data-testid="speed-inprogress">
          <h3 style={h3}>{STR.inProgressH[L]}</h3>
          <p style={{ ...meta, margin: "0 0 8px" }}>{STR.inProgressNote[L]}</p>
          {in_progress.map((r) => (
            <div key={r.withdrawal_id} data-testid="speed-inprogress-row"
              style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline",
                borderTop: "1px solid rgba(255,255,255,0.06)", padding: "6px 0" }}>
              <span style={{ color: "#cfe0f5", fontSize: "0.82rem" }}>
                {r.broker || "—"}
                {r.account_unresolved && <span style={{ ...meta, marginLeft: 6 }}>({STR.accountUnresolved[L]})</span>}
                {r.amount && <span style={{ ...meta, marginLeft: 6 }}>{r.amount} {r.currency}</span>}
              </span>
              <span style={{ color: "#fbbf24", fontSize: "0.82rem" }}>
                {humanDuration(r.elapsed_seconds, L)} {STR.elapsed[L]}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default WithdrawalSpeed;
