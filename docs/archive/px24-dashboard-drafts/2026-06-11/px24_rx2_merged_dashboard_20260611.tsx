"use client";

// =============================================================================
// PX-2.4 — Dashboard Alignment: translate intelligence into human understanding.
//
// Aligns the live dashboard with PX Design Principles v1 + PX Dashboard
// Reference Design v1. Frontend-only — reuses existing read APIs, no backend,
// no new models/migrations, no execution, no mutations.
//
// Cards (DOM order = mobile priority): Header + status card · Performance
// Snapshot · Market Focus + Your Strategies · Intelligence Row (Opportunity
// Radar · Key Events · Research Evidence). Every visual
// answers a question; technical terms are translated to human language
// (technical state kept as secondary/tooltip).
// =============================================================================

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";
import { apiFetch } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";

// ─── Types ───
type Strategy = { id: number; name: string; symbol_universe?: string; is_active?: boolean };
type Account = { id: number; name: string; broker_name?: string; is_active?: boolean };
type Assignment = { strategy_id: number; account_id: number; stage?: string };
type Mt5Status = { login?: string; server?: string; last_status?: string | null };
type ObservedStats = { total_trades: number; win_rate_pct: number; max_drawdown_pct: number; net_pnl_total: number; longest_loss_streak?: number; wins?: number; losses?: number };
type BalancePoint = { balance_after_trade: number; net_pnl_money?: number };
type Perf = { mt5_balance_current?: number | null; mt5_equity_current?: number | null; currency?: string; observed_stats?: ObservedStats; balance_series?: BalancePoint[] };
type MarketStateCtx = { trend_state?: string; volatility_state?: string; volatility_expansion?: string; breakout_state?: string; risk_tone?: string | null; news_impact?: string; regime?: string; asset_class?: string };
type MarketState = { current_state: string; confidence: string; supporting_evidence: string[]; context?: MarketStateCtx };
type PrefStrategy = { name: string; family: string; suitability: string; kb_avg_quality: number | null; kb_observations: number };
type Selection = {
  ok?: boolean; market_state?: MarketState;
  preferred_families?: { family: string; label: string; suitability: string }[];
  preferred_strategies?: PrefStrategy[];
  confidence?: string; rationale?: string[]; warnings?: string[];
};
// ─── RX-2 Reliability Core (Phase 1: read + acknowledge; advisory recommendations) ───
type RxHealth = { ok?: boolean; state?: string; can_trade?: boolean; reasons?: string[]; computed_at?: string };
type RxAlert = { id: number; severity: string; component: string; title: string; status: string; created_at: string };
type RxRec = { id: number; recommended_action: string; target_ref: string; rationale: string; status: string };
const TH_TONE: Record<string, { c: "green" | "yellow" | "red" | "gray"; label: string }> = {
  HEALTHY: { c: "green", label: "Healthy" }, DEGRADED: { c: "yellow", label: "Degraded" },
  IMPAIRED: { c: "yellow", label: "Impaired" }, DOWN: { c: "red", label: "Down" }, UNKNOWN: { c: "gray", label: "Unknown" },
};

// ─── Style ───
const glass: React.CSSProperties = { borderRadius: 14, border: "1px solid rgba(74,179,255,0.12)", background: "linear-gradient(135deg, rgba(10,15,40,0.95) 0%, rgba(5,8,22,0.98) 100%)", padding: "1.1rem 1.25rem" };
const heroGlass: React.CSSProperties = { ...glass, border: "1px solid rgba(74,179,255,0.28)", background: "linear-gradient(135deg, rgba(13,20,52,0.96) 0%, rgba(6,10,26,0.98) 100%)" };
const secHeader: React.CSSProperties = { fontSize: "0.72rem", color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.05em", fontWeight: 600, marginBottom: "0.7rem" };
const muted: React.CSSProperties = { fontSize: "0.78rem", color: "#94a3b8" };
const microLabel: React.CSSProperties = { fontSize: "0.68rem", color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 3 };
const selStyle: React.CSSProperties = { padding: "0.4rem 0.7rem", background: "#0f172a", border: "1px solid #334155", borderRadius: 8, color: "#e5f4ff", fontSize: "0.9rem" };
const actionLink: React.CSSProperties = { fontSize: "0.8rem", border: "1px solid rgba(74,179,255,0.4)", borderRadius: 8, padding: "5px 12px", color: "#4ab3ff", textDecoration: "none", display: "inline-block" };

const COMMON = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "USDCAD", "BTCUSD", ".US30Cash"];
const LS_KEY = "guvfx.focus.symbol";

function money(n: number | null | undefined, ccy = "") {
  if (n == null) return "—";
  return (n < 0 ? "-" : "") + (ccy ? ccy + " " : "$") + Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 });
}
function nowClock() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
function greeting() { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; }

// ─── Phase 2: Human translation layer (market state → Market Mood) ───
type Mood = { label: string; color: string; badge: "green" | "yellow" | "red" | "blue" | "gray"; icon: string };
function marketMood(state: string | undefined, ctx?: MarketStateCtx): Mood {
  const dir = ctx?.trend_state || "";
  const up = dir.includes("up"), down = dir.includes("down");
  switch (state) {
    case "NEWS_SHOCK": return { label: "High Risk", color: "#fca5a5", badge: "red", icon: "alert-triangle" };
    case "VOLATILITY_EXPANSION": return { label: "Active · Volatile", color: "#fbbf24", badge: "yellow", icon: "activity" };
    case "VOLATILITY_CONTRACTION": return { label: "Quiet", color: "#93c5fd", badge: "blue", icon: "wave-saw-tool" };
    case "TREND_EXPANSION": return up ? { label: "Trending · Bullish", color: "#86efac", badge: "green", icon: "trending-up" }
      : down ? { label: "Trending · Bearish", color: "#fca5a5", badge: "red", icon: "trending-down" }
      : { label: "Trending", color: "#93c5fd", badge: "blue", icon: "trending-up" };
    case "TREND_EXHAUSTION": return { label: "Sideways · Momentum Fading", color: "#fbbf24", badge: "yellow", icon: "wave-sine" };
    case "RANGE_COMPRESSION": return { label: "Quiet", color: "#93c5fd", badge: "blue", icon: "arrows-minimize" };
    case "RANGE_EXPANSION": return { label: "Active", color: "#fbbf24", badge: "yellow", icon: "arrows-maximize" };
    case "RISK_ON": return { label: "Risk-On", color: "#86efac", badge: "green", icon: "mood-smile" };
    case "RISK_OFF": return { label: "Risk-Off", color: "#fca5a5", badge: "red", icon: "mood-nervous" };
    default: return { label: "Unclear", color: "#94a3b8", badge: "gray", icon: "help-circle" };
  }
}
// Short human phrase for the technical state (small metadata line)
function stateHuman(state: string): string {
  switch (state) {
    case "NEWS_SHOCK": return "news event nearby";
    case "VOLATILITY_EXPANSION": return "volatility picking up";
    case "VOLATILITY_CONTRACTION": return "volatility settling";
    case "TREND_EXPANSION": return "trend strengthening";
    case "TREND_EXHAUSTION": return "momentum fading";
    case "RANGE_COMPRESSION": return "range tightening";
    case "RANGE_EXPANSION": return "range widening";
    case "RISK_ON": return "risk appetite rising";
    case "RISK_OFF": return "risk appetite falling";
    default: return state.replace(/_/g, " ").toLowerCase();
  }
}
// Plain-English secondary detail from technical context (shown small / tooltip only)
function moodDetail(state: string | undefined, ctx?: MarketStateCtx): string {
  const parts: string[] = [];
  if (state) parts.push(state.replace(/_/g, " ").toLowerCase());
  if (ctx?.risk_tone) parts.push(ctx.risk_tone === "RISK_ON" ? "risk-on tone" : "risk-off tone");
  return parts.join(" · ");
}

// ─── Phase 5: Research confidence (observation count → human) ───
type Conf = { label: string; color: "green" | "blue" | "yellow" | "gray"; note: string };
function researchConfidence(n: number | undefined | null, avgQuality?: number | null): Conf {
  const c = n || 0;
  const obs = `${c} similar historical observation${c === 1 ? "" : "s"}`;
  const q = avgQuality != null ? ` · average past quality ${avgQuality}%` : "";
  if (c === 0) return { label: "No evidence yet", color: "gray", note: "No similar historical observations recorded for this setup yet." };
  if (c <= 2) return { label: "Limited evidence", color: "yellow", note: `Based on ${obs}${q}. Treat as early/indicative only.` };
  if (c <= 9) return { label: "Moderate confidence", color: "blue", note: `Based on ${obs}${q}.` };
  return { label: "Stronger confidence", color: "green", note: `Based on ${obs}${q}.` };
}

// ─── Symbol → relevant currencies (Key Events context) ───
function currenciesFor(sym: string): string[] {
  const s = (sym || "").toUpperCase();
  if (s.startsWith("XAU") || s.includes("GOLD")) return ["USD", "Gold / macro"];
  if (s.startsWith("BTC") || s.includes("US30") || s.includes("NAS") || s.includes("SPX")) return ["USD", "Risk sentiment"];
  const m = s.match(/^([A-Z]{3})([A-Z]{3})$/);
  if (m) return [m[1], m[2]];
  return ["USD"];
}

// ─── Layer 3: Human market identity (display only — raw symbol stays in data logic) ───
const CCY_FLAG: Record<string, string> = { USD: "🇺🇸", EUR: "🇪🇺", GBP: "🇬🇧", JPY: "🇯🇵", CAD: "🇨🇦", AUD: "🇦🇺", NZD: "🇳🇿", CHF: "🇨🇭" };
const MARKET_IDS: { test: RegExp; name: string; glyph: string; cls: "index" | "gold" | "fx" | "btc" | "eth" }[] = [
  { test: /US30/i, name: "Dow Jones", glyph: "🇺🇸", cls: "index" },
  { test: /NAS100|NDX|USTEC/i, name: "Nasdaq 100", glyph: "🇺🇸", cls: "index" },
  { test: /SPX|US500/i, name: "S&P 500", glyph: "🇺🇸", cls: "index" },
  { test: /^XAU|GOLD/i, name: "Gold", glyph: "🪙", cls: "gold" },
  { test: /^EURUSD/i, name: "Euro / US Dollar", glyph: "🇪🇺 🇺🇸", cls: "fx" },
  { test: /^GBPUSD/i, name: "British Pound / US Dollar", glyph: "🇬🇧 🇺🇸", cls: "fx" },
  { test: /^USDJPY/i, name: "US Dollar / Japanese Yen", glyph: "🇺🇸 🇯🇵", cls: "fx" },
  { test: /^USDCAD/i, name: "US Dollar / Canadian Dollar", glyph: "🇺🇸 🇨🇦", cls: "fx" },
  { test: /^AUDUSD/i, name: "Australian Dollar / US Dollar", glyph: "🇦🇺 🇺🇸", cls: "fx" },
  { test: /^NZDUSD/i, name: "New Zealand Dollar / US Dollar", glyph: "🇳🇿 🇺🇸", cls: "fx" },
  { test: /^USDCHF/i, name: "US Dollar / Swiss Franc", glyph: "🇺🇸 🇨🇭", cls: "fx" },
  { test: /^BTC/i, name: "Bitcoin / US Dollar", glyph: "₿", cls: "btc" },
  { test: /^ETH/i, name: "Ethereum / US Dollar", glyph: "Ξ", cls: "eth" },
];
const MARKET_BG: Record<string, string> = {
  index: "linear-gradient(135deg, rgba(59,130,246,0.16) 0%, rgba(127,29,29,0.12) 100%)",
  gold: "linear-gradient(135deg, rgba(212,175,55,0.18) 0%, rgba(120,80,20,0.10) 100%)",
  fx: "linear-gradient(135deg, rgba(74,179,255,0.14) 0%, rgba(15,23,42,0.35) 100%)",
  btc: "linear-gradient(135deg, rgba(247,147,26,0.15) 0%, rgba(60,35,8,0.14) 100%)",
  eth: "linear-gradient(135deg, rgba(139,92,246,0.15) 0%, rgba(30,20,60,0.18) 100%)",
};
function marketIdentity(sym: string): { name: string; glyph: string; bg: string } {
  const hit = MARKET_IDS.find((m) => m.test.test(sym));
  if (hit) return { name: hit.name, glyph: hit.glyph, bg: MARKET_BG[hit.cls] };
  const m = (sym || "").toUpperCase().match(/^([A-Z]{3})([A-Z]{3})$/);
  if (m && CCY_FLAG[m[1]] && CCY_FLAG[m[2]]) return { name: `${m[1]} / ${m[2]}`, glyph: `${CCY_FLAG[m[1]]} ${CCY_FLAG[m[2]]}`, bg: MARKET_BG.fx };
  return { name: sym, glyph: "", bg: MARKET_BG.fx };
}

// ─── Layer 3: Simple mood (Bull / Sideways / Bear) — display mapping over existing state ───
function simpleMood(state: string | undefined, ctx?: MarketStateCtx): { label: "Bull" | "Sideways" | "Bear"; color: string; badge: "green" | "blue" | "red"; desc: string; why: string } {
  const dir = ctx?.trend_state || "";
  const up = dir.includes("up"), down = dir.includes("down");
  if ((state === "TREND_EXPANSION" && up) || state === "RISK_ON")
    return { label: "Bull", color: "#86efac", badge: "green", desc: "Buyers are currently in control and trend conditions remain supportive. Price has been holding upward structure rather than giving it back.", why: "Market pressure is leaning upward. Momentum-based research may deserve attention, but confirmation still matters." };
  if ((state === "TREND_EXPANSION" && down) || state === "RISK_OFF")
    return { label: "Bear", color: "#fca5a5", badge: "red", desc: "Sellers are currently in control and downside pressure remains dominant. Price has been losing ground rather than recovering it.", why: "Market pressure is leaning downward. Defensive or downside research may deserve attention, but confirmation still matters." };
  return { label: "Sideways", color: "#93c5fd", badge: "blue", desc: "Buyers and sellers are balanced and price is spending more time inside established ranges than breaking away from them.", why: "Buyers and sellers are roughly balanced — price is not showing a clear directional bias. Confirmation matters more than direction right now." };
}

// ─── GuvFX Mood Icons — proprietary inline SVG set (shared ring, wave motif varies) ───
function MoodIcon({ mood, size = 40 }: { mood: "Bull" | "Sideways" | "Bear"; size?: number }) {
  // Muted institutional palette — intentionally softer than state text colors
  const c = mood === "Bull" ? "#5fc88f" : mood === "Bear" ? "#e07a6e" : "#d4a843";
  const motif =
    mood === "Bull" ? "M10 27 C14 27, 16 21.5, 20 18.5 C24 15.5, 26 13.5, 30 12.5"
    : mood === "Bear" ? "M10 12.5 C14 12.5, 16 18, 20 21 C24 24, 26 26.5, 30 27.5"
    : "M9 20 C12 13.5, 16 13.5, 20 20 C24 26.5, 28 26.5, 31 20";
  return (
    <svg width={size} height={size} viewBox="0 0 40 40" role="img" aria-label={`${mood} market mood`} style={{ display: "block" }}>
      <circle cx="20" cy="20" r="17" fill="rgba(255,255,255,0.02)" stroke={c} strokeOpacity="0.4" strokeWidth="1.5" />
      <path d={motif} fill="none" stroke={c} strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// Opportunity proxy (NOT a signal): Watching / Developing / Worth Researching
function proxy(sel: Selection): { label: string; color: "green" | "blue" | "gray" } {
  const state = sel.market_state?.current_state;
  const top = (sel.preferred_strategies || [])[0];
  if (!top || state === "NEWS_SHOCK") return { label: "Watching", color: "gray" };
  const hasEvidence = (top.kb_observations || 0) >= 3 && top.kb_avg_quality != null;
  if (top.suitability === "HIGH" && hasEvidence) return { label: "Worth researching", color: "green" };
  if (top.suitability === "HIGH" || top.suitability === "MEDIUM") return { label: "Developing", color: "blue" };
  return { label: "Watching", color: "gray" };
}

function symbolOptions(strategies: Strategy[], last: string | null): string[] {
  const assigned = new Set<string>();
  for (const s of strategies) (s.symbol_universe || "").split(/[,;\s]+/).forEach((x) => { const v = x.trim(); if (v) assigned.add(v); });
  const ordered: string[] = [];
  // Strip list-literal artifacts from symbol_universe (e.g. "['EURUSD']" → "EURUSD")
  const push = (raw: string) => { const v = raw.replace(/[[\]'"]/g, ""); if (v && !ordered.includes(v)) ordered.push(v); };
  if (last) push(last);
  [...assigned].forEach(push);
  COMMON.forEach(push);
  return ordered;
}

// ─── Small UI helpers ───
function Info({ text }: { text: string }) {
  return <i className="ti ti-info-circle" title={text} aria-label={text} role="img" style={{ fontSize: 13, color: "#64748b", cursor: "help", marginLeft: 4, verticalAlign: "middle" }} />;
}
// Visible help dot (Tabler webfont is missing site-wide, so icon-font classes render empty)
function InfoDot({ text }: { text: string }) {
  return (
    <span title={text} aria-label={text} role="img" style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 13, height: 13, borderRadius: 999, border: "1px solid #475569", color: "#94a3b8", fontSize: 9, fontFamily: "Georgia, serif", fontStyle: "italic", lineHeight: 1, marginLeft: 5, verticalAlign: "middle", cursor: "help", flexShrink: 0 }}>i</span>
  );
}
// Small section glyph (visible text symbol — same webfont constraint)
function SecGlyph({ sym, color }: { sym: string; color: string }) {
  return <span aria-hidden="true" style={{ color, marginRight: 5, fontSize: "0.72rem" }}>{sym}</span>;
}
function Sparkline({ values, color = "#86efac", w = 150, h = 30 }: { values: number[]; color?: string; w?: number; h?: number }) {
  if (!values || values.length < 2) return <div style={{ height: h }} aria-hidden />;
  const min = Math.min(...values), max = Math.max(...values), range = max - min || 1;
  // Inset vertically so the thicker stroke isn't clipped at extremes
  const yFor = (v: number) => 3 + (h - 6) - ((v - min) / range) * (h - 6);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${yFor(v)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: h, display: "block" }} preserveAspectRatio="none" role="img" aria-label="equity trend">
      <line x1="0" y1={yFor(values[0])} x2={w} y2={yFor(values[0])} stroke="rgba(148,163,184,0.25)" strokeWidth="1" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
      <polyline points={pts} fill="none" stroke={color} strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
function MetricTile({ label, value, sub, subColor, info }: { label: string; value: React.ReactNode; sub?: string; subColor?: string; info?: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ ...microLabel, fontSize: "0.64rem", marginBottom: 3, whiteSpace: "nowrap" }}>{label}{info && <Info text={info} />}</div>
      <div style={{ fontSize: "1.05rem", fontWeight: 700, color: "#f0f6ff", lineHeight: 1.2, letterSpacing: "-0.01em", whiteSpace: "nowrap" }}>{value}</div>
      {sub && <div style={{ fontSize: "0.68rem", color: subColor || "#64748b", marginTop: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{sub}</div>}
    </div>
  );
}

export default function DashboardPage() {
  const lang = useLang();
  const [firstName, setFirstName] = useState<string | null>(null);
  const [dailyPnl, setDailyPnl] = useState<number | null>(null);
  // Per-strategy 30D stats from the existing daily-pnl endpoint (totals only)
  const [stratPerf, setStratPerf] = useState<Record<number, { net_pnl: number; win_rate: number; trades: number }>>({});
  // Opportunity Radar — cross-market rows from the existing strategy-selection endpoint
  const [radarRows, setRadarRows] = useState<{ sym: string; category: string; catColor: "green" | "blue" | "gray" | "red" | "yellow"; note: string; conf: string; confLevel: number }[]>([]);

  // RX-2 Reliability Core (read + acknowledge)
  const [tradingHealth, setTradingHealth] = useState<RxHealth | null>(null);
  const [alerts, setAlerts] = useState<RxAlert[]>([]);
  const [recs, setRecs] = useState<RxRec[]>([]);
  const loadReliability = useCallback(() => {
    apiFetch<RxHealth>("/api/reliability/trading-health/", {}).then((h) => setTradingHealth(h || null)).catch(() => setTradingHealth(null));
    apiFetch<{ alerts: RxAlert[] }>("/api/reliability/alerts/?status=open", {}).then((r) => setAlerts(r?.alerts || [])).catch(() => setAlerts([]));
    apiFetch<{ recommendations: RxRec[] }>("/api/reliability/recommendations/?status=open", {}).then((r) => setRecs(r?.recommendations || [])).catch(() => setRecs([]));
  }, []);
  useEffect(() => { loadReliability(); }, [loadReliability]);
  const acknowledge = useCallback(async (id: number) => {
    try { await apiFetch(`/api/reliability/alerts/${id}/acknowledge/`, { method: "POST", body: JSON.stringify({}) }); } catch { /* ignore */ }
    loadReliability();
  }, [loadReliability]);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [mt5, setMt5] = useState<Mt5Status | null>(null);
  const [perf, setPerf] = useState<Perf | null>(null);
  const [normFlag, setNormFlag] = useState<string | null>(null);
  const [syncedAt, setSyncedAt] = useState("");
  const [bootLoaded, setBootLoaded] = useState(false);

  const [options, setOptions] = useState<string[]>([]);
  const [symbol, setSymbol] = useState("");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [selLoading, setSelLoading] = useState(false);
  const [selAt, setSelAt] = useState("");

  // ── Boot (fast reads) ──
  useEffect(() => {
    (async () => {
      const [strats, accts, asn, status, attr] = await Promise.all([
        apiFetch<Strategy[]>("/api/strategies/strategies/", {}).catch(() => []),
        apiFetch<Account[]>("/api/trading/accounts/", {}).catch(() => []),
        apiFetch<Assignment[]>("/api/strategies/assignments/", {}).catch(() => []),
        apiFetch<Mt5Status>("/api/mt5/status/", {}).catch(() => null),
        apiFetch<{ ok: boolean; normalisation_attribution?: Record<string, { avg_max_drawdown: number; observation_count: number }> }>("/api/backtests/feature-attribution/?min_count=3", {}).catch(() => null),
      ]);
      setStrategies(strats || []); setAccounts(accts || []); setAssignments(asn || []); setMt5(status);
      // Best-effort personalization (AuthGate already validated the session)
      apiFetch<{ first_name?: string }>("/api/auth/me/", {})
        .then((me) => { if (me?.first_name?.trim()) setFirstName(me.first_name.trim()); })
        .catch(() => { /* fallback to "Trader" */ });
      if (attr?.ok) {
        const tw = attr.normalisation_attribution?.["true"], fa = attr.normalisation_attribution?.["false"];
        if (tw && fa && tw.observation_count >= 3 && tw.avg_max_drawdown >= fa.avg_max_drawdown * 2) setNormFlag(`High-notional setups have shown elevated drawdown in research (${tw.avg_max_drawdown}% vs ${fa.avg_max_drawdown}%).`);
      }
      let last: string | null = null;
      try { last = window.localStorage.getItem(LS_KEY); } catch { last = null; }
      const opts = symbolOptions(strats || [], last);
      setOptions(opts);
      setSymbol(last || opts[0] || "EURUSD");
      setBootLoaded(true);

      // Opportunity Radar — scan available research context across markets (existing endpoint, no new backend)
      opts.slice(0, 5).forEach((rs) => {
        apiFetch<Selection>(`/api/backtests/strategy-selection/?symbol=${encodeURIComponent(rs)}&timeframe=H1`, {})
          .then((res) => {
            if (!res?.ok) return;
            const st = res.market_state?.current_state;
            const rctx = res.market_state?.context;
            const sm = simpleMood(st, rctx);
            const topS = (res.preferred_strategies || [])[0];
            const p = proxy(res);
            const breakoutish = st === "VOLATILITY_EXPANSION" || st === "RANGE_EXPANSION";
            // Fixed radar vocabulary: Setup / Breakout / Watching / News Risk / Range
            let category: string, catColor: "green" | "blue" | "gray" | "red" | "yellow";
            if (st === "NEWS_SHOCK") { category = "News Risk"; catColor = "red"; }
            else if (p.label === "Worth researching") { category = "Setup"; catColor = "green"; }
            else if (breakoutish) { category = "Breakout"; catColor = "yellow"; }
            else if (sm.label === "Sideways") { category = "Range"; catColor = "blue"; }
            else { category = "Watching"; catColor = "gray"; }
            const note = st === "NEWS_SHOCK" ? "High-impact event nearby" : breakoutish ? "Volatility expanding" : sm.label !== "Sideways" ? "Trend conditions align" : "Range conditions dominant";
            const rc = researchConfidence(topS?.kb_observations, topS?.kb_avg_quality);
            const confWord = rc.label === "No evidence yet" ? "None" : rc.label.split(" ")[0];
            const confLevel = rc.label === "Stronger confidence" ? 3 : rc.label === "Moderate confidence" ? 2 : rc.label === "Limited evidence" ? 1 : 0;
            setRadarRows((prev) => (prev.some((x) => x.sym === rs) ? prev : [...prev, { sym: rs, category, catColor, note, conf: confWord, confLevel }]));
          })
          .catch(() => { /* radar row simply absent */ });
      });

      const primary = (accts || []).find((a) => a.is_active) || (accts || [])[0];
      if (primary) {
        apiFetch<Perf>(`/api/analytics/trade-history/?account_id=${primary.id}`, {}).then((p) => { setPerf(p); setSyncedAt(nowClock()); }).catch(() => setSyncedAt(nowClock()));
        // Today's PnL from the existing daily aggregation endpoint (UTC day of close_time)
        apiFetch<{ series?: { date: string; net_pnl: number }[] }>(`/api/analytics/daily-pnl/?account_id=${primary.id}&days=1`, {})
          .then((d) => {
            const today = new Date().toISOString().slice(0, 10);
            const row = (d?.series || []).find((s) => s.date === today);
            setDailyPnl(row ? row.net_pnl : 0);
          })
          .catch(() => setDailyPnl(null));
        // 30D per-strategy stats for the Your Strategies card (max 3, existing endpoint)
        (strats || []).slice(0, 3).forEach((s) => {
          apiFetch<{ totals?: { net_pnl: number; win_rate: number; trades: number } }>(`/api/analytics/daily-pnl/?account_id=${primary.id}&strategy_id=${s.id}&days=30`, {})
            .then((d) => { if (d?.totals) setStratPerf((prev) => ({ ...prev, [s.id]: d.totals! })); })
            .catch(() => { /* placeholders remain */ });
        });
      } else setSyncedAt(nowClock());
    })();
  }, []);

  // ── Market Focus: fetch selection on symbol change (persist to localStorage) ──
  useEffect(() => {
    if (!symbol) return;
    try { window.localStorage.setItem(LS_KEY, symbol); } catch { /* ignore */ }
    setSelLoading(true); setSelection(null);
    apiFetch<Selection>(`/api/backtests/strategy-selection/?symbol=${encodeURIComponent(symbol)}&timeframe=H1`, {})
      .then((res) => { setSelection(res?.ok ? res : null); setSelAt(nowClock()); })
      .catch(() => setSelection(null))
      .finally(() => setSelLoading(false));
  }, [symbol]);

  // ── Derived: performance ──
  const primaryAcct = accounts.find((a) => a.is_active) || accounts[0];
  const stats = perf?.observed_stats;
  const series = perf?.balance_series || [];
  const equitySeries = series.map((p) => p.balance_after_trade);
  const pnls = series.map((p) => (typeof p.net_pnl_money === "number" ? p.net_pnl_money : 0));
  const netPnl = stats?.net_pnl_total ?? null;
  const equityRef = perf?.mt5_equity_current ?? perf?.mt5_balance_current ?? null;
  const rising = equitySeries.length > 1 && equitySeries[equitySeries.length - 1] >= equitySeries[0];

  // Profit factor + expectancy derived from per-trade PnL (no raw metric shown unlabelled)
  const grossWin = pnls.filter((v) => v > 0).reduce((a, b) => a + b, 0);
  const lossArr = pnls.filter((v) => v < 0);
  const grossLoss = Math.abs(lossArr.reduce((a, b) => a + b, 0));
  const profitFactor = grossLoss > 0 ? grossWin / grossLoss : (grossWin > 0 ? Infinity : null);
  const avgLoss = lossArr.length ? grossLoss / lossArr.length : 0;
  const expMoney = pnls.length ? pnls.reduce((a, b) => a + b, 0) / pnls.length : null;
  const expR = avgLoss > 0 && expMoney != null ? expMoney / avgLoss : null;
  const pctOfEquity = netPnl != null && equityRef ? (netPnl / equityRef) * 100 : null;

  const trend = netPnl == null ? { label: "No data", color: "#64748b", badge: "gray" as const, icon: "minus" }
    : netPnl > 0 && rising ? { label: "Improving", color: "#86efac", badge: "green" as const, icon: "trending-up" }
    : netPnl < 0 ? { label: "Needs Attention", color: "#fca5a5", badge: "red" as const, icon: "trending-down" }
    : { label: "Stable", color: "#fbbf24", badge: "yellow" as const, icon: "minus" };

  const pfLabel = profitFactor == null ? null : profitFactor === Infinity || profitFactor >= 1.3 ? { t: "Good", c: "#86efac" } : profitFactor >= 1.0 ? { t: "Moderate", c: "#fbbf24" } : { t: "Low", c: "#fca5a5" };
  const ddLabel = stats ? (stats.max_drawdown_pct < 10 ? { t: "Low", c: "#86efac" } : stats.max_drawdown_pct < 25 ? { t: "Moderate", c: "#fbbf24" } : { t: "High", c: "#fca5a5" }) : null;
  const expLabel = expMoney == null ? null : expMoney >= 0 ? { t: "Positive", c: "#86efac" } : { t: "Negative", c: "#fca5a5" };
  const wrLabel = stats ? (stats.win_rate_pct >= 50 ? { c: "#86efac" } : { c: "#fbbf24" }) : null;

  const stageFor = (sid: number) => assignments.find((a) => a.strategy_id === sid)?.stage;

  // ── Derived: Market Focus ──
  const ms = selection?.market_state;
  const ctx = ms?.context;
  const smood = simpleMood(ms?.current_state, ctx);
  const top = (selection?.preferred_strategies || [])[0];
  const topFamily = (selection?.preferred_families || [])[0];
  const conf = researchConfidence(top?.kb_observations, top?.kb_avg_quality);
  const px = selection ? proxy(selection) : null;
  let risk = "Standard market risk applies.";
  if (ms?.current_state === "NEWS_SHOCK") risk = "A high-impact event is nearby — conditions can move quickly and setups are lower-quality right now.";
  else if ((selection?.warnings || []).length) risk = selection!.warnings![0];
  else if (top && (top.kb_observations || 0) < 3) risk = "Limited historical evidence for this pairing — treat as early context only.";

  // ── Key Events (from available news context; graceful empty state) ──
  const ccys = currenciesFor(symbol);
  const newsImpact = ctx?.news_impact && ctx.news_impact !== "NONE" ? ctx.news_impact : null;
  const newsEvidence = (ms?.supporting_evidence || []).find((e) => /event|impact|news|min/i.test(e));
  const hasEvent = !!newsImpact || ms?.current_state === "NEWS_SHOCK";

  const setupComplete = strategies.length > 0 && accounts.length > 0;

  const SectionCard = useCallback(({ icon, title, info, children, action, style }: { icon: string; title: string; info?: string; children: React.ReactNode; action?: { href: string; label: string }; style?: React.CSSProperties }) => (
    <div style={{ ...glass, ...style }}>
      <div style={{ ...secHeader, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span><i className={`ti ti-${icon}`} aria-hidden="true" style={{ marginRight: 6 }} />{title}{info && <Info text={info} />}</span>
        {action && <Link href={action.href} style={{ fontSize: "0.7rem", color: "#4ab3ff", textTransform: "none", fontWeight: 400, textDecoration: "none" }}>{action.label} →</Link>}
      </div>
      {children}
    </div>
  ), []);

  function health(s: Strategy): { t: string; c: "green" | "yellow" | "gray" } {
    const stage = stageFor(s.id);
    if (!s.is_active) return { t: "Idle", c: "gray" };
    if (stage === "LIVE") return { t: "Good", c: "green" };
    return { t: "Fair", c: "yellow" };
  }

  return (
    <div style={{ maxWidth: 1180, margin: "0 auto" }}>
      {/* Header — greeting (dominant) + Account Status Summary card */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "1.25rem", marginBottom: "1.5rem" }}>
        <div>
          <h1 style={{ fontSize: "1.9rem", margin: 0, color: "#f0f6ff", fontWeight: 650, letterSpacing: "-0.01em" }}>{greeting()}, {firstName || "Trader"} 👋</h1>
          <p style={{ margin: "0.45rem 0 0", fontSize: "0.95rem", color: "#94a3b8", fontWeight: 400 }}>Here&apos;s your edge today.</p>
        </div>

        {/* Account Status Summary */}
        <div style={{ borderRadius: 14, border: "1px solid rgba(255,255,255,0.09)", background: "rgba(10,15,35,0.6)", padding: "1.15rem 0.3rem", display: "flex", alignItems: "stretch" }}>
          <div style={{ padding: "0 1.5rem", borderRight: "1px solid rgba(255,255,255,0.045)", minWidth: 180 }}>
            <div style={microLabel}><i className="ti ti-server" aria-hidden="true" style={{ marginRight: 5 }} />MT5</div>
            <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: mt5?.last_status ? "0.95rem" : "0.85rem", fontWeight: mt5?.last_status ? 600 : 500, color: mt5?.last_status === "SUCCESS" ? "#86efac" : mt5?.last_status ? "#fca5a5" : "#8b9bb4" }}>
              <span aria-hidden="true" style={{ width: 8, height: 8, borderRadius: 999, flexShrink: 0, background: mt5?.last_status === "SUCCESS" ? "#34d399" : mt5?.last_status ? "#f87171" : "#64748b" }} />
              {mt5?.last_status === "SUCCESS" ? "Connected" : mt5?.last_status ? "Disconnected" : "Status unavailable"}
            </div>
            {primaryAcct && <div style={{ fontSize: "0.73rem", color: "#8b9bb4", marginTop: 4 }}>{primaryAcct.broker_name || "Broker"} · {primaryAcct.name}</div>}
          </div>
          <div style={{ padding: "0 1.5rem", borderRight: "1px solid rgba(255,255,255,0.045)", minWidth: 110 }}>
            <div style={microLabel}><i className="ti ti-wallet" aria-hidden="true" style={{ marginRight: 5 }} />Equity</div>
            <div style={{ fontSize: "0.95rem", fontWeight: 600, color: "#f0f6ff" }}>{money(equityRef, perf?.currency)}</div>
          </div>
          <div style={{ padding: "0 1.5rem", borderRight: "1px solid rgba(255,255,255,0.045)", minWidth: 120 }}>
            <div style={microLabel}><i className={`ti ti-trending-${dailyPnl != null && dailyPnl < 0 ? "down" : "up"}`} aria-hidden="true" style={{ marginRight: 5 }} />Daily PnL</div>
            <div style={{ fontSize: "0.95rem", fontWeight: 600, color: dailyPnl == null ? "#f0f6ff" : dailyPnl < 0 ? "#fca5a5" : "#86efac" }}>
              {dailyPnl == null ? "—" : `${dailyPnl >= 0 ? "+" : "-"}${perf?.currency ? perf.currency + " " : "$"}${Math.abs(dailyPnl).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
            </div>
          </div>
          <div style={{ padding: "0 1.5rem", display: "flex", flexDirection: "column", justifyContent: "space-between", minWidth: 130 }}>
            <div>
              <div style={microLabel}><i className="ti ti-clock" aria-hidden="true" style={{ marginRight: 5 }} />Updated</div>
              <div style={{ fontSize: "0.95rem", fontWeight: 600, color: "#cbd5e1" }}>{syncedAt ? `as of ${syncedAt}` : "syncing…"}</div>
            </div>
            <Link href="/trading/terminal-access" style={{ color: "#4ab3ff", textDecoration: "none", fontSize: "0.7rem", marginTop: 6 }}>Terminal →</Link>
          </div>
        </div>
      </div>

      {/* RX-2 Trading Health (reliability) — ambient system trust. Read-only + acknowledge. */}
      {tradingHealth && tradingHealth.ok !== false && (
        <div style={{ ...glass, marginBottom: "0.9rem", borderColor: (TH_TONE[tradingHealth.state || "UNKNOWN"]?.c === "red") ? "rgba(248,113,113,0.35)" : (TH_TONE[tradingHealth.state || "UNKNOWN"]?.c === "yellow") ? "rgba(251,191,36,0.28)" : "rgba(74,179,255,0.12)" }}>
          <div style={{ ...secHeader, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span><i className="ti ti-heartbeat" aria-hidden="true" style={{ marginRight: 6 }} />Trading Health<Info text="Whether GuvFX can actually trade right now (MT5 connected/logged-in, data fresh, execution healthy) — not just whether processes are running." /></span>
            {tradingHealth.computed_at && <span style={{ fontSize: "0.7rem", color: "#64748b", textTransform: "none", fontWeight: 400 }}>as of {new Date(tradingHealth.computed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: alerts.length || recs.length ? 10 : 0 }}>
            <Badge color={TH_TONE[tradingHealth.state || "UNKNOWN"]?.c || "gray"}>{TH_TONE[tradingHealth.state || "UNKNOWN"]?.label || tradingHealth.state}</Badge>
            <span style={{ fontSize: "0.82rem", color: tradingHealth.can_trade ? "#86efac" : "#fca5a5" }}>{tradingHealth.can_trade ? "Able to trade" : "Trading capability impaired"}</span>
            {(tradingHealth.reasons || []).length > 0 && <span style={{ fontSize: "0.78rem", color: "#94a3b8" }}>· {(tradingHealth.reasons || []).slice(0, 2).join(" · ")}</span>}
          </div>
          {alerts.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: recs.length ? 8 : 0 }}>
              <div style={microLabel}>Open alerts</div>
              {alerts.slice(0, 4).map((a) => (
                <div key={a.id} style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", fontSize: "0.8rem", color: "#b7c5dd" }}>
                  <Badge color={a.severity === "CRITICAL" ? "red" : a.severity === "WARN" ? "yellow" : "gray"}>{a.severity}</Badge>
                  <span>{a.title}</span>
                  <button onClick={() => acknowledge(a.id)} style={{ ...actionLink, padding: "2px 9px", fontSize: "0.72rem", cursor: "pointer", background: "transparent" }}>Acknowledge</button>
                </div>
              ))}
            </div>
          )}
          {recs.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              <div style={microLabel}>Recommended actions <span style={{ textTransform: "none", color: "#64748b" }}>(advisory — operator-performed, not automatic)</span></div>
              {recs.slice(0, 4).map((r) => (
                <div key={r.id} style={{ fontSize: "0.78rem", color: "#94a3b8", display: "flex", gap: 7 }}>
                  <i className="ti ti-tool" aria-hidden="true" style={{ color: "#4ab3ff", fontSize: 13, marginTop: 1 }} /><span><span style={{ color: "#e9f4ff" }}>{r.recommended_action}</span> {r.target_ref ? `(${r.target_ref})` : ""} — {r.rationale}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Performance Snapshot — How am I doing? (thin single-strip command bar) */}
      <div style={{ ...glass, padding: "0.85rem 1.5rem 0.95rem", marginBottom: "0.9rem" }}>
        <div style={{ ...secHeader, display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.6rem" }}>
          <span><i className="ti ti-gauge" aria-hidden="true" style={{ marginRight: 6 }} />How am I doing?</span>
          <Link href="/trading/trade-history" style={{ fontSize: "0.7rem", color: "#4ab3ff", textTransform: "none", fontWeight: 400, textDecoration: "none" }}>Detailed performance →</Link>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "1.4rem", flexWrap: "wrap" }}>
          {/* Trend — emotional anchor (inline, still dominant) */}
          <div style={{ minWidth: 165, paddingRight: "1.4rem", borderRight: "1px solid rgba(255,255,255,0.045)" }}>
            <div style={{ ...microLabel, fontSize: "0.64rem", marginBottom: 3, whiteSpace: "nowrap" }}>Performance Trend</div>
            <div style={{ fontSize: "1.3rem", fontWeight: 700, lineHeight: 1.15, letterSpacing: "-0.01em", color: trend.color, whiteSpace: "nowrap" }}>
              <i className={`ti ti-${trend.icon}`} aria-hidden="true" style={{ marginRight: 5 }} />{trend.label}
            </div>
            <div style={{ fontSize: "0.68rem", color: "#64748b", marginTop: 2, whiteSpace: "nowrap" }}>
              {netPnl != null ? `Observed PnL: ${money(netPnl, perf?.currency)}${stats?.total_trades ? ` (${stats.total_trades} trades)` : ""}` : "No observed trades yet"}
            </div>
          </div>

          {/* Secondary metrics — dense strip */}
          <div style={{ flex: 1, minWidth: 300, display: "flex", alignItems: "center", gap: "1.4rem", flexWrap: "wrap" }}>
            <MetricTile label="Net PnL" value={<span style={{ color: netPnl == null ? "#f0f6ff" : netPnl < 0 ? "#fca5a5" : "#86efac" }}>{netPnl == null ? "—" : money(netPnl, perf?.currency)}</span>} sub={pctOfEquity != null ? `${pctOfEquity >= 0 ? "+" : ""}${pctOfEquity.toFixed(2)}% of equity` : undefined} />
            <MetricTile label="Win Rate" value={stats ? `${stats.win_rate_pct}%` : "—"} sub={stats ? `${stats.wins ?? "—"}W / ${stats.losses ?? "—"}L` : undefined} subColor={wrLabel?.c} />
            <MetricTile label="Profit Factor" info="Gross profit ÷ gross loss across observed trades. Above 1.0 means winners outweigh losers." value={profitFactor == null ? "—" : profitFactor === Infinity ? "∞" : profitFactor.toFixed(2)} sub={pfLabel?.t} subColor={pfLabel?.c} />
            <MetricTile label="Max Drawdown" value={stats ? `${stats.max_drawdown_pct}%` : "—"} sub={ddLabel?.t} subColor={ddLabel?.c} />
            <MetricTile label="Expectancy" info="Average result per trade. In R, it is the average expressed in units of your average losing trade. Not a prediction." value={expMoney == null ? "—" : expR != null ? `${expR >= 0 ? "+" : ""}${expR.toFixed(2)}R` : money(expMoney, perf?.currency)} sub={expLabel?.t} subColor={expLabel?.c} />
          </div>

          {/* Equity curve — compact context */}
          <div style={{ width: 250, minWidth: 200, paddingLeft: "1.4rem", borderLeft: "1px solid rgba(255,255,255,0.045)" }}>
            <div style={{ ...microLabel, fontSize: "0.64rem", marginBottom: 4 }}>Equity Curve (Observed)</div>
            {equitySeries.length > 1 ? <Sparkline values={equitySeries} color={trend.color} w={230} h={42} />
              : <div style={{ ...muted, fontSize: "0.68rem" }}>{t(lang, "legal.microDisclaimer")}</div>}
          </div>
        </div>
      </div>

      {/* Market Focus + Your Strategies — two-card desktop row */}
      <div style={{ display: "flex", gap: "0.9rem", alignItems: "stretch", flexWrap: "wrap", marginBottom: "0.9rem" }}>
      {/* Market Focus — HERO (What deserves attention? Why? Now what?) */}
      <div style={{ ...heroGlass, flex: "1 1 560px", minWidth: 320, display: "flex", flexDirection: "column" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 10, marginBottom: "0.8rem" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={secHeader}><i className="ti ti-target" aria-hidden="true" style={{ marginRight: 6 }} />Market Focus</span>
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={selStyle} aria-label="Select market">
              {options.map((s) => { const id = marketIdentity(s); return <option key={s} value={s}>{`${id.glyph ? id.glyph + "  " : ""}${id.name !== s ? `${id.name} — ${s}` : s}`}</option>; })}
            </select>
            <span style={{ fontSize: "0.7rem", color: "#64748b" }}>Change market</span>
          </div>
          <span style={{ fontSize: "0.7rem", color: "#64748b" }}>{selLoading ? "analysing…" : selAt ? `updated as of ${selAt}` : ""}</span>
        </div>

        {selLoading || !bootLoaded ? <div style={muted}>Analysing {marketIdentity(symbol).name}…</div>
          : !selection ? <div style={muted}>Couldn&apos;t analyse {marketIdentity(symbol).name} right now. Try another market.</div>
          : <div style={{ display: "flex", flexWrap: "wrap", alignItems: "stretch" }}>
              {/* Zone 1 — Market identity (compact, fully used) */}
              <div style={{ width: 150, minWidth: 140, borderRadius: 12, border: "1px solid rgba(255,255,255,0.07)", background: marketIdentity(symbol).bg, padding: "0.7rem", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", textAlign: "center", gap: 4 }}>
                {marketIdentity(symbol).glyph && <div style={{ fontSize: "1.3rem", lineHeight: 1 }} aria-hidden="true">{marketIdentity(symbol).glyph}</div>}
                <div style={{ fontSize: "0.92rem", fontWeight: 650, color: "#f0f6ff", lineHeight: 1.25 }}>{marketIdentity(symbol).name}</div>
                {marketIdentity(symbol).name !== symbol && <div style={{ fontSize: "0.62rem", color: "#64748b", letterSpacing: "0.04em" }}>{symbol}</div>}
                <div style={{ marginTop: 3 }}><MoodIcon mood={smood.label} size={36} /></div>
                {selAt && <div style={{ fontSize: "0.6rem", color: "#64748b", marginTop: 2 }}>as of {selAt}</div>}
              </div>
              {/* Zone 2 — Mood + Why this matters */}
              <div style={{ flex: "1 1 230px", minWidth: 210, margin: "0 0 0 1.1rem", paddingRight: "1.1rem", borderRight: "1px solid rgba(255,255,255,0.06)", display: "flex", flexDirection: "column", justifyContent: "space-between", gap: 9 }}>
                <div>
                  <div style={{ ...microLabel, display: "flex", alignItems: "center" }}><span style={{ marginRight: 5 }}><MoodIcon mood={smood.label} size={14} /></span>Market Mood<InfoDot text="Simple translation of current market conditions. It is not a trade instruction." /></div>
                  <div style={{ fontSize: "1.25rem", fontWeight: 700, color: smood.color, marginBottom: 2 }}>{smood.label}</div>
                  <div style={{ fontSize: "0.78rem", color: "#b7c5dd", marginBottom: 3 }}>{smood.desc}</div>
                  {ms && <div style={{ fontSize: "0.64rem", color: "#64748b" }} title={moodDetail(ms.current_state, ctx)}>{ms.confidence} confidence · {stateHuman(ms.current_state)}</div>}
                </div>
                <div style={{ borderTop: "1px solid rgba(255,255,255,0.05)", paddingTop: 9 }}>
                  <div style={microLabel}><i className="ti ti-checks" aria-hidden="true" style={{ marginRight: 5, color: "#94a3b8" }} />Why this matters<InfoDot text="Explains why the current market state deserves attention." /></div>
                  <div style={{ fontSize: "0.78rem", color: "#b7c5dd" }}>{smood.why}</div>
                </div>
              </div>
              {/* Zone 3 — Worth Researching + Confidence + Risk/action */}
              <div style={{ flex: "1 1 270px", minWidth: 250, paddingLeft: "1.1rem", display: "flex", flexDirection: "column", justifyContent: "space-between", gap: 9 }}>
                <div>
                  <div style={microLabel}><i className="ti ti-flask" aria-hidden="true" style={{ marginRight: 5, color: "#a78bfa" }} />Worth Researching<InfoDot text="Research direction only. Not a recommendation to trade." /></div>
                  {top ? <div style={{ fontSize: "0.84rem", color: "#e9f4ff", display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <span><span style={{ fontWeight: 600 }}>{topFamily?.label || top.family}</span><span style={{ fontSize: "0.74rem", color: "#94a3b8" }}> · Focus: {top.name}</span></span>
                    {px && <Badge color={px.color}>{px.label}</Badge>}
                  </div> : <div style={{ ...muted, fontSize: "0.78rem" }}>No clear strategy fit in this state — watching.</div>}
                </div>
                <div style={{ borderTop: "1px solid rgba(255,255,255,0.05)", paddingTop: 9 }}>
                  <div style={microLabel}><i className="ti ti-chart-bar" aria-hidden="true" style={{ marginRight: 5, color: "#a78bfa" }} />Research Confidence<InfoDot text="How much historical evidence supports this context." /></div>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <Badge color={conf.color}>{conf.label}</Badge>
                    <span style={{ fontSize: "0.72rem", color: "#94a3b8" }}>{conf.note}</span>
                  </div>
                </div>
                <div style={{ borderTop: "1px solid rgba(255,255,255,0.05)", paddingTop: 9, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
                  <div style={{ flex: 1, minWidth: 150 }}>
                    <div style={microLabel}><i className="ti ti-alert-triangle" aria-hidden="true" style={{ marginRight: 5, color: "#fbbf24" }} />Main risk<InfoDot text="Highlights what could make this research less reliable." /></div>
                    <div style={{ fontSize: "0.76rem", color: "#b7c5dd" }}>{risk}</div>
                  </div>
                  <Link href="/analytics/strategy-lab" style={actionLink}>Research →</Link>
                </div>
              </div>
            </div>}
        <div style={{ fontSize: "0.68rem", color: "#475569", marginTop: "auto", paddingTop: "0.8rem" }}>Research context — not a trade signal, prediction, or recommendation. &quot;Worth researching&quot; means conditions align for further study, nothing more.</div>
      </div>

      {/* Your Strategies — fixed right card */}
      <div style={{ ...glass, flex: "0 1 350px", minWidth: 300, display: "flex", flexDirection: "column" }}>
        <div style={{ ...secHeader, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span>Your Strategies<InfoDot text="Strategies on your account. Status reflects assignment stage — not a performance judgment." /></span>
          <Link href="/strategies" style={{ fontSize: "0.7rem", color: "#4ab3ff", textTransform: "none", fontWeight: 400, textDecoration: "none" }}>View all →</Link>
        </div>
        {strategies.length === 0 ? (
          <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 10, textAlign: "center", padding: "1.2rem 0" }}>
            <div style={muted}>No strategies yet.</div>
            <Link href="/strategies/create" style={actionLink}>Create a strategy →</Link>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", flex: 1, justifyContent: "flex-start" }}>
            {/* Stat column headers — once, not per row */}
            <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "0 0.1rem 0.35rem" }}>
              <div style={{ flex: 1, minWidth: 0 }} />
              <div style={{ display: "flex", alignItems: "center", gap: 12, paddingLeft: 12 }}>
                <div style={{ minWidth: 52, textAlign: "right", fontSize: "0.58rem", color: "#64748b", textTransform: "uppercase", letterSpacing: "0.04em" }}>PnL 30D</div>
                <div style={{ minWidth: 52, textAlign: "right", fontSize: "0.58rem", color: "#64748b", textTransform: "uppercase", letterSpacing: "0.04em" }}>W/L</div>
                <div style={{ minWidth: 44, textAlign: "right", fontSize: "0.58rem", color: "#64748b", textTransform: "uppercase", letterSpacing: "0.04em" }}>Health</div>
              </div>
              <span aria-hidden="true" style={{ width: 7 }} />
            </div>
            {strategies.slice(0, 3).map((s, i) => {
              const h = health(s);
              const syms = (s.symbol_universe || "").replace(/[[\]'"]/g, "").split(/[,;\s]+/).filter(Boolean);
              const mkts = syms.slice(0, 2).map((x) => marketIdentity(x).name).join(" · ") || "No markets assigned";
              const stage = stageFor(s.id);
              const sp = stratPerf[s.id];
              return (
                <Link key={s.id} href={`/strategies/${s.id}`} style={{ textDecoration: "none", display: "flex", alignItems: "center", gap: 12, padding: "0.55rem 0.1rem", borderTop: "1px solid rgba(255,255,255,0.05)" }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: "0.85rem", fontWeight: 600, color: "#e9f4ff", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{s.name}</div>
                    <div style={{ fontSize: "0.68rem", color: "#94a3b8", marginTop: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{mkts}{stage ? ` · ${stage}` : ""}</div>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 12, borderLeft: "1px solid rgba(255,255,255,0.05)", paddingLeft: 12 }}>
                    <div style={{ minWidth: 52, textAlign: "right", fontSize: "0.76rem", fontWeight: 600, color: !sp || !sp.trades ? "#94a3b8" : sp.net_pnl < 0 ? "#fca5a5" : "#86efac" }}>{!sp || !sp.trades ? "—" : money(sp.net_pnl, perf?.currency)}</div>
                    <div style={{ minWidth: 52, textAlign: "right", fontSize: "0.76rem", fontWeight: 600, color: "#e9f4ff" }}>{!sp || !sp.trades ? "—" : `${sp.win_rate}%`}</div>
                    <div style={{ minWidth: 44, textAlign: "right" }}><Badge color={h.c}>{h.t}</Badge></div>
                  </div>
                  <span aria-hidden="true" style={{ color: "#64748b", fontSize: "1rem", lineHeight: 1 }}>›</span>
                </Link>
              );
            })}
          </div>
        )}
        {/* Bottom action — always present so the card feels fixed */}
        <div style={{ marginTop: "auto", borderTop: "1px solid rgba(255,255,255,0.06)", paddingTop: "0.65rem", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: "0.68rem", color: "#64748b" }}>{strategies.length > 3 ? `+${strategies.length - 3} more` : ""}</span>
          <Link href="/strategies" style={actionLink}>Manage strategies →</Link>
        </div>
      </div>
      </div>

      {/* Intelligence Row — Radar (all markets, hero) · Key Events (selected) · Research Evidence (selected) */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.9rem", marginBottom: "0.9rem", alignItems: "stretch" }}>

        {/* Opportunity Radar — ALL markets (UX shell; rows from existing research context only) */}
        <SectionCard icon="radar" title="Opportunity Radar" info="Opportunities and alerts across all markets. Research context only — not trade signals." style={{ flex: "2 1 420px", minWidth: 360 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap", marginBottom: 6 }}>
            <span style={{ fontSize: "0.7rem", color: "#64748b" }}>Opportunities and alerts across all markets</span>
            <span style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
              {["All", "Research", "Breakouts", "Watchlist", "News Risk"].map((tb, i) => (
                <span key={tb} title={i ? "Coming soon" : undefined} style={{ fontSize: "0.64rem", padding: "2px 9px", borderRadius: 999, border: `1px solid ${i ? "rgba(255,255,255,0.07)" : "rgba(74,179,255,0.45)"}`, color: i ? "#475569" : "#4ab3ff", background: i ? "transparent" : "rgba(74,179,255,0.08)" }}>{tb}</span>
              ))}
            </span>
          </div>
          {radarRows.length ? (
            <div style={{ display: "flex", flexDirection: "column" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, paddingBottom: 4 }}>
                <div style={{ width: 118, minWidth: 100, fontSize: "0.58rem", color: "#64748b", textTransform: "uppercase", letterSpacing: "0.04em" }}>Market</div>
                <div style={{ minWidth: 88, fontSize: "0.58rem", color: "#64748b", textTransform: "uppercase", letterSpacing: "0.04em" }}>Category</div>
                <div style={{ flex: 1, fontSize: "0.58rem", color: "#64748b", textTransform: "uppercase", letterSpacing: "0.04em" }}>Signal context</div>
                <div style={{ minWidth: 70, textAlign: "right", fontSize: "0.58rem", color: "#64748b", textTransform: "uppercase", letterSpacing: "0.04em" }}>Confidence</div>
              </div>
              {radarRows.slice(0, 5).map((r) => (
                <div key={r.sym} style={{ display: "flex", alignItems: "center", gap: 10, padding: "0.42rem 0", borderTop: "1px solid rgba(255,255,255,0.05)" }}>
                  <div style={{ width: 118, minWidth: 100, display: "flex", alignItems: "center", gap: 6 }}>
                    {marketIdentity(r.sym).glyph && <span aria-hidden="true" style={{ fontSize: "0.82rem", lineHeight: 1 }}>{marketIdentity(r.sym).glyph}</span>}
                    <span style={{ fontSize: "0.78rem", fontWeight: 600, color: "#e9f4ff", whiteSpace: "nowrap" }}>{r.sym.replace(/^\./, "").replace(/cash$/i, "").toUpperCase()}</span>
                  </div>
                  <div style={{ minWidth: 88 }}><Badge color={r.catColor}>{r.category}</Badge></div>
                  <div style={{ flex: 1, fontSize: "0.72rem", color: "#94a3b8" }}>{r.note}</div>
                  <div style={{ minWidth: 70, textAlign: "right" }} title={`${r.conf} confidence`}>
                    <div aria-hidden="true" style={{ fontSize: "0.66rem", letterSpacing: 2, color: "#8fb7e8", lineHeight: 1.1 }}>
                      {"●".repeat(r.confLevel)}<span style={{ color: "rgba(255,255,255,0.16)" }}>{"●".repeat(3 - r.confLevel)}</span>
                    </div>
                    <div style={{ fontSize: "0.6rem", color: "#64748b", marginTop: 1 }}>{r.conf}</div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ padding: "0.8rem 0", textAlign: "center" }}>
              <div style={{ fontSize: "0.8rem", color: "#94a3b8" }}>{bootLoaded ? "Opportunity Intelligence coming soon" : "Scanning available research context…"}</div>
              <div style={{ fontSize: "0.7rem", color: "#475569", marginTop: 3 }}>No opportunities currently meet review criteria.</div>
            </div>
          )}
        </SectionCard>

        {/* Key Events — SELECTED market only */}
        <SectionCard icon="calendar-event" title={`Key Events (${marketIdentity(symbol).name})`} info="External factors that may affect the selected market, from available market-context research." style={{ flex: "1 1 250px", minWidth: 250, display: "flex", flexDirection: "column" }}>
          <div style={{ ...microLabel, marginBottom: 7 }}>Relevant to: {ccys.join(" · ")}</div>
          {hasEvent
            ? <div style={{ borderTop: "1px solid rgba(255,255,255,0.05)", paddingTop: 9, display: "flex", alignItems: "flex-start", gap: 10 }}>
                <Badge color={ms?.current_state === "NEWS_SHOCK" ? "red" : "yellow"}>{(newsImpact || "HIGH") + " impact"}</Badge>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: "0.82rem", color: "#e9f4ff", fontWeight: 500 }}>{ms?.current_state === "NEWS_SHOCK" ? "High-impact event nearby" : "Elevated news context"}</div>
                  {newsEvidence && <div style={{ fontSize: "0.74rem", color: "#94a3b8", marginTop: 2 }}>{newsEvidence}</div>}
                  <div style={{ fontSize: "0.68rem", color: "#64748b", marginTop: 5 }}>Conditions can move quickly around high-impact events — research context only.</div>
                </div>
              </div>
            : <div style={{ borderTop: "1px solid rgba(255,255,255,0.05)", paddingTop: 9 }}>
                <div style={muted}>No major scheduled events found for this market right now.</div>
                <div style={{ fontSize: "0.7rem", color: "#475569", marginTop: 4 }}>Based on current market-context research. Always check your economic calendar before trading.</div>
              </div>}
          <div style={{ marginTop: "auto", borderTop: "1px solid rgba(255,255,255,0.05)", paddingTop: 8, display: "flex", alignItems: "center", gap: 7 }}>
            <span title="Coming soon" style={{ fontSize: "0.74rem", color: "#475569", cursor: "default" }}>View full calendar →</span>
            <span style={{ fontSize: "0.58rem", color: "#475569", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 999, padding: "1px 7px" }}>Soon</span>
          </div>
        </SectionCard>

        {/* Research Evidence — SELECTED market only ("Why we like this idea") */}
        <SectionCard icon="database" title={`Research Evidence (${marketIdentity(symbol).name})`} info="Why GuvFX considers the selected market worth researching." style={{ flex: "1 1 250px", minWidth: 250, display: "flex", flexDirection: "column" }}>
          {!selection ? <div style={muted}>Select a market to see its research evidence.</div>
            : <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ ...microLabel }}>Why we like this idea</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <div style={{ fontSize: "0.78rem", color: top?.kb_observations ? "#b7c5dd" : "#64748b", display: "flex", gap: 7 }}>
                    <i className={top?.kb_observations ? "ti ti-check" : "ti ti-circle-dashed"} aria-hidden="true" style={{ color: top?.kb_observations ? "#86efac" : "#64748b", fontSize: 13, marginTop: 1 }} />
                    {top?.kb_observations ? `${top.kb_observations} similar historical condition${top.kb_observations === 1 ? "" : "s"} observed` : "Historical evidence unavailable"}
                  </div>
                  <div style={{ fontSize: "0.78rem", color: top?.kb_avg_quality != null ? "#b7c5dd" : "#64748b", display: "flex", gap: 7 }}>
                    <i className={top?.kb_avg_quality != null ? "ti ti-check" : "ti ti-circle-dashed"} aria-hidden="true" style={{ color: top?.kb_avg_quality != null ? "#86efac" : "#64748b", fontSize: 13, marginTop: 1 }} />
                    {top?.kb_avg_quality != null ? `Average historical quality ${top.kb_avg_quality}%` : "Waiting for additional observations"}
                  </div>
                  <div style={{ fontSize: "0.78rem", color: "#b7c5dd", display: "flex", gap: 7 }}>
                    <i className={(selection.warnings || []).length ? "ti ti-alert-triangle" : "ti ti-check"} aria-hidden="true" style={{ color: (selection.warnings || []).length ? "#fbbf24" : "#86efac", fontSize: 13, marginTop: 1 }} />
                    {(selection.warnings || []).length ? `${selection.warnings!.length} caution${selection.warnings!.length === 1 ? "" : "s"} noted in current research` : "No major conflicting observations"}
                  </div>
                </div>
                <div style={{ borderTop: "1px solid rgba(255,255,255,0.05)", paddingTop: 8 }}>
                  <div style={microLabel}>Research Confidence</div>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <Badge color={conf.color}>{conf.label}</Badge>
                    <span style={{ fontSize: "0.72rem", color: "#94a3b8" }}>{conf.note}</span>
                  </div>
                </div>
              </div>}
          <div style={{ marginTop: "auto", borderTop: "1px solid rgba(255,255,255,0.05)", paddingTop: 8 }}>
            <Link href="/analytics/strategy-lab" style={{ fontSize: "0.74rem", color: "#4ab3ff", textDecoration: "none" }}>Research this setup →</Link>
          </div>
        </SectionCard>

      </div>

      {/* Onboarding (only if incomplete) */}
      {bootLoaded && !setupComplete && (
        <div style={{ ...glass, marginTop: "0.9rem" }}>
          <div style={secHeader}><i className="ti ti-rocket" aria-hidden="true" style={{ marginRight: 6 }} />Get set up</div>
          <div style={{ display: "flex", gap: "1.25rem", flexWrap: "wrap", fontSize: "0.82rem" }}>
            <span style={{ color: strategies.length ? "#86efac" : "#94a3b8" }}>{strategies.length ? "✓" : "○"} <Link href="/strategies/create" style={{ color: "inherit", textDecoration: "none" }}>Create a strategy</Link></span>
            <span style={{ color: accounts.length ? "#86efac" : "#94a3b8" }}>{accounts.length ? "✓" : "○"} <Link href="/accounts" style={{ color: "inherit", textDecoration: "none" }}>Link an account</Link></span>
            <span style={{ color: "#94a3b8" }}>○ <Link href="/backtests" style={{ color: "inherit", textDecoration: "none" }}>Run a backtest</Link></span>
          </div>
        </div>
      )}

      <div style={{ fontSize: "0.7rem", color: "#475569", padding: "0.7rem 0" }}>Trading Intelligence — research context from historical observations and strategy criteria. Not a prediction, signal, or recommendation to trade.</div>
    </div>
  );
}
