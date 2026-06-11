"use client";

// =============================================================================
// PX-2.4 — Dashboard Alignment: translate intelligence into human understanding.
//
// Aligns the live dashboard with PX Design Principles v1 + PX Dashboard
// Reference Design v1. Frontend-only — reuses existing read APIs, no backend,
// no new models/migrations, no execution, no mutations.
//
// Cards (DOM order = mobile priority): Trust ribbon · Performance Snapshot ·
// Market Focus (hero) · Attention · Research Evidence · Key Events ·
// Strategy Insights. Every visual answers a question; technical terms are
// translated to human language (technical state kept as secondary/tooltip).
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

// ─── RX-2 Reliability Core (Phase 1: read + acknowledge; advisory recommendations) ───
type RxHealth = { ok?: boolean; state?: string; can_trade?: boolean; reasons?: string[]; computed_at?: string };
type RxAlert = { id: number; severity: string; component: string; title: string; status: string; created_at: string };
type RxRec = { id: number; recommended_action: string; target_ref: string; rationale: string; status: string };
const TH_TONE: Record<string, { c: "green" | "yellow" | "red" | "gray"; label: string }> = {
  HEALTHY: { c: "green", label: "Healthy" }, DEGRADED: { c: "yellow", label: "Degraded" },
  IMPAIRED: { c: "yellow", label: "Impaired" }, DOWN: { c: "red", label: "Down" }, UNKNOWN: { c: "gray", label: "Unknown" },
};

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
  const q = avgQuality != null ? ` · average past quality ${avgQuality}/100` : "";
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
  const push = (v: string) => { if (v && !ordered.includes(v)) ordered.push(v); };
  if (last) push(last);
  [...assigned].forEach(push);
  COMMON.forEach(push);
  return ordered;
}

// ─── Small UI helpers ───
function Info({ text }: { text: string }) {
  return <i className="ti ti-info-circle" title={text} aria-label={text} role="img" style={{ fontSize: 13, color: "#64748b", cursor: "help", marginLeft: 4, verticalAlign: "middle" }} />;
}
function Sparkline({ values, color = "#86efac", w = 150, h = 30 }: { values: number[]; color?: string; w?: number; h?: number }) {
  if (!values || values.length < 2) return <div style={{ height: h }} aria-hidden />;
  const min = Math.min(...values), max = Math.max(...values), range = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - ((v - min) / range) * h}`).join(" ");
  return <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", maxWidth: w, height: h }} preserveAspectRatio="none" role="img" aria-label="equity trend"><polyline points={pts} fill="none" stroke={color} strokeWidth="2" /></svg>;
}
function MetricTile({ label, value, sub, subColor, info }: { label: string; value: React.ReactNode; sub?: string; subColor?: string; info?: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ ...microLabel, marginBottom: 2 }}>{label}{info && <Info text={info} />}</div>
      <div style={{ fontSize: "1.15rem", fontWeight: 700, color: "#f0f6ff", lineHeight: 1.2 }}>{value}</div>
      {sub && <div style={{ fontSize: "0.7rem", color: subColor || "#64748b" }}>{sub}</div>}
    </div>
  );
}

export default function DashboardPage() {
  const lang = useLang();
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

      const primary = (accts || []).find((a) => a.is_active) || (accts || [])[0];
      if (primary) apiFetch<Perf>(`/api/analytics/trade-history/?account_id=${primary.id}`, {}).then((p) => { setPerf(p); setSyncedAt(nowClock()); }).catch(() => setSyncedAt(nowClock()));
      else setSyncedAt(nowClock());
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
  const mood = marketMood(ms?.current_state, ctx);
  const top = (selection?.preferred_strategies || [])[0];
  const topFamily = (selection?.preferred_families || [])[0];
  const conf = researchConfidence(top?.kb_observations, top?.kb_avg_quality);
  const px = selection ? proxy(selection) : null;
  const reason = selection?.rationale?.[0] || (ms ? `${symbol} is ${mood.label.toLowerCase()} right now — ${moodDetail(ms.current_state, ctx)}.` : "");
  let risk = "Standard market risk applies.";
  if (ms?.current_state === "NEWS_SHOCK") risk = "A high-impact event is nearby — conditions can move quickly and setups are lower-quality right now.";
  else if ((selection?.warnings || []).length) risk = selection!.warnings![0];
  else if (top && (top.kb_observations || 0) < 3) risk = "Limited historical evidence for this pairing — treat as early context only.";

  // ── Key Events (from available news context; graceful empty state) ──
  const ccys = currenciesFor(symbol);
  const newsImpact = ctx?.news_impact && ctx.news_impact !== "NONE" ? ctx.news_impact : null;
  const newsEvidence = (ms?.supporting_evidence || []).find((e) => /event|impact|news|min/i.test(e));
  const hasEvent = !!newsImpact || ms?.current_state === "NEWS_SHOCK";

  // ── Attention flags ──
  const flags: { sev: "red" | "yellow"; text: string }[] = [];
  if (mt5?.last_status && mt5.last_status !== "SUCCESS") flags.push({ sev: "red", text: `MT5 status: ${mt5.last_status} — check the terminal` });
  if (ms?.current_state === "NEWS_SHOCK") flags.push({ sev: "red", text: `${symbol}: high-impact event nearby — setups are lower-quality` });
  if (normFlag) flags.push({ sev: "yellow", text: normFlag });
  (selection?.warnings || []).forEach((w) => { if (/news|warn|insufficient|limited/i.test(w)) flags.push({ sev: "yellow", text: `${symbol}: ${w}` }); });

  const setupComplete = strategies.length > 0 && accounts.length > 0;

  const SectionCard = useCallback(({ icon, title, info, children, action }: { icon: string; title: string; info?: string; children: React.ReactNode; action?: { href: string; label: string } }) => (
    <div style={glass}>
      <div style={{ ...secHeader, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span><i className={`ti ti-${icon}`} aria-hidden="true" style={{ marginRight: 6 }} />{title}{info && <Info text={info} />}</span>
        {action && <Link href={action.href} style={{ fontSize: "0.7rem", color: "#4ab3ff", textTransform: "none", fontWeight: 400, textDecoration: "none" }}>{action.label} →</Link>}
      </div>
      {children}
    </div>
  ), []);

  // Strategy Insights — observational note tied to current context (no advice)
  function insightNote(s: Strategy): string {
    const syms = (s.symbol_universe || "").toUpperCase();
    if (ms && symbol && syms.includes(symbol.toUpperCase())) {
      if (ms.current_state === "NEWS_SHOCK") return `Current context: ${symbol} has a high-impact event nearby — observed setups are lower-quality.`;
      return `Current context: ${symbol} is ${mood.label.toLowerCase()}. Worth reviewing in Strategy Lab.`;
    }
    if (normFlag) return "Research observation: higher-notional setups have shown elevated drawdown — worth reviewing sizing.";
    return "No recent observations flagged. Open Strategy Lab for fuller context.";
  }
  function health(s: Strategy): { t: string; c: "green" | "yellow" | "gray" } {
    const stage = stageFor(s.id);
    if (!s.is_active) return { t: "Idle", c: "gray" };
    if (stage === "LIVE") return { t: "Good", c: "green" };
    return { t: "Fair", c: "yellow" };
  }

  return (
    <div style={{ maxWidth: 1180, margin: "0 auto" }}>
      {/* Header — Today + warm voice */}
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: 8, marginBottom: "0.8rem" }}>
        <h1 style={{ fontSize: "1.5rem", margin: 0, color: "#f0f6ff", fontWeight: 600 }}>{greeting()}, Trader 👋 <span style={{ color: "#94a3b8", fontWeight: 400, fontSize: "1.1rem" }}>— what&apos;s worth your attention today</span></h1>
        {syncedAt && <span style={{ fontSize: "0.72rem", color: "#64748b" }}>as of {syncedAt}</span>}
      </div>

      {/* Trust ribbon (ambient) */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", fontSize: "0.76rem", color: "#94a3b8", border: "1px solid rgba(255,255,255,0.07)", borderRadius: 10, padding: "0.5rem 0.9rem", marginBottom: "0.9rem" }}>
        <span style={{ display: "flex", alignItems: "center", gap: 5, color: mt5?.last_status === "SUCCESS" ? "#86efac" : "#fbbf24" }}>
          <i className="ti ti-circle-filled" style={{ fontSize: 9 }} aria-hidden="true" />MT5 {mt5?.last_status === "SUCCESS" ? "connected" : (mt5?.last_status || "—")}
        </span>
        {primaryAcct && <span>· {primaryAcct.broker_name || "Broker"} · {primaryAcct.name}</span>}
        {mt5?.server && <span>· {mt5.server}</span>}
        <span>· Equity {money(equityRef, perf?.currency)}</span>
        {syncedAt && <span style={{ color: "#64748b" }}>· synced {syncedAt}</span>}
        <Link href="/trading/terminal-access" style={{ marginLeft: "auto", color: "#4ab3ff", textDecoration: "none", fontSize: "0.72rem" }}>Terminal →</Link>
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

      {/* Performance Snapshot — How am I doing? (6 interpreted metrics + curve) */}
      <div style={{ ...glass, marginBottom: "0.9rem" }}>
        <div style={{ ...secHeader, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span><i className="ti ti-gauge" aria-hidden="true" style={{ marginRight: 6 }} />How am I doing?</span>
          <Link href="/trading/trade-history" style={{ fontSize: "0.7rem", color: "#4ab3ff", textTransform: "none", fontWeight: 400, textDecoration: "none" }}>Detailed performance →</Link>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: "1rem", alignItems: "end" }}>
          <MetricTile label="Performance Trend" value={<span style={{ color: trend.color }}><i className={`ti ti-${trend.icon}`} aria-hidden="true" style={{ marginRight: 4 }} />{trend.label}</span>} sub="vs recent history" />
          <MetricTile label="Net PnL" value={<span style={{ color: trend.color }}>{netPnl == null ? "—" : money(netPnl, perf?.currency)}</span>} sub={pctOfEquity != null ? `${pctOfEquity >= 0 ? "+" : ""}${pctOfEquity.toFixed(2)}% of equity` : undefined} />
          <MetricTile label="Win Rate" value={stats ? `${stats.win_rate_pct}%` : "—"} sub={stats ? `${stats.wins ?? "—"} wins / ${stats.losses ?? "—"} losses` : undefined} subColor={wrLabel?.c} />
          <MetricTile label="Profit Factor" info="Gross profit ÷ gross loss across observed trades. Above 1.0 means winners outweigh losers." value={profitFactor == null ? "—" : profitFactor === Infinity ? "∞" : profitFactor.toFixed(2)} sub={pfLabel?.t} subColor={pfLabel?.c} />
          <MetricTile label="Max Drawdown" value={stats ? `${stats.max_drawdown_pct}%` : "—"} sub={ddLabel?.t} subColor={ddLabel?.c} />
          <MetricTile label="Expectancy" info="Average result per trade. In R, it is the average expressed in units of your average losing trade. Not a prediction." value={expMoney == null ? "—" : expR != null ? `${expR >= 0 ? "+" : ""}${expR.toFixed(2)}R` : money(expMoney, perf?.currency)} sub={expLabel?.t} subColor={expLabel?.c} />
        </div>
        <div style={{ marginTop: "0.8rem", borderTop: "1px solid rgba(255,255,255,0.06)", paddingTop: "0.7rem" }}>
          <div style={{ ...microLabel, marginBottom: 4 }}>Equity curve (observed)</div>
          {equitySeries.length > 1 ? <Sparkline values={equitySeries} color={trend.color} w={1100} h={44} />
            : <div style={muted}>{t(lang, "legal.microDisclaimer")}</div>}
        </div>
      </div>

      {/* Market Focus — HERO (What deserves attention? Why? Now what?) */}
      <div style={{ ...heroGlass, marginBottom: "0.9rem" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 10, marginBottom: "0.8rem" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={secHeader}><i className="ti ti-target" aria-hidden="true" style={{ marginRight: 6 }} />Market Focus</span>
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={selStyle} aria-label="Select market">
              {options.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <span style={{ fontSize: "0.7rem", color: "#64748b" }}>Change market</span>
          </div>
          <span style={{ fontSize: "0.7rem", color: "#64748b" }}>{selLoading ? "analysing…" : selAt ? `updated as of ${selAt}` : ""}</span>
        </div>

        {selLoading || !bootLoaded ? <div style={muted}>Analysing {symbol}…</div>
          : !selection ? <div style={muted}>Couldn&apos;t analyse {symbol} right now. Try another market.</div>
          : <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: "1.1rem" }}>
              {/* Market Mood (human) */}
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
                  <span style={{ fontSize: "1.1rem", color: "#f0f6ff", fontWeight: 600 }}>{symbol}</span>
                  {px && <Badge color={px.color}>{px.label}</Badge>}
                </div>
                <div style={{ ...microLabel }}>Market Mood<Info text="A plain-English read of current conditions from market-state research. It is context, not a signal or prediction." /></div>
                <div style={{ fontSize: "1.25rem", fontWeight: 700, color: mood.color, marginBottom: 2 }}>
                  <i className={`ti ti-${mood.icon}`} aria-hidden="true" style={{ marginRight: 6 }} />{mood.label}
                </div>
                {ms && <div style={{ fontSize: "0.68rem", color: "#64748b", marginBottom: 8 }} title={moodDetail(ms.current_state, ctx)}>technical: {ms.current_state} · {ms.confidence} confidence</div>}
                {top ? <div style={{ fontSize: "0.85rem", color: "#e9f4ff" }}>
                  <span style={muted}>Suggested research</span><br />
                  <span style={{ fontWeight: 500 }}>{topFamily?.label || top.family}</span> — {top.name}
                </div> : <div style={muted}>No clear strategy fit in this state — watching.</div>}
              </div>
              {/* Why + Risk */}
              <div>
                <div style={microLabel}>Why this matters</div>
                <div style={{ fontSize: "0.82rem", color: "#b7c5dd", marginBottom: 8 }}>{reason}</div>
                <div style={microLabel}>Main risk</div>
                <div style={{ fontSize: "0.82rem", color: "#b7c5dd" }}>{risk}</div>
              </div>
              {/* Research confidence + action */}
              <div>
                <div style={microLabel}>Research Confidence<Info text="How much similar historical evidence GuvFX has seen for this kind of setup. It is not a prediction or a probability of profit." /></div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <Badge color={conf.color}>{conf.label}</Badge>
                </div>
                <div style={{ fontSize: "0.76rem", color: "#94a3b8", marginBottom: 12 }}>{conf.note}</div>
                <Link href="/analytics/strategy-lab" style={actionLink}>Research this setup →</Link>
              </div>
            </div>}
        <div style={{ fontSize: "0.68rem", color: "#475569", marginTop: "0.8rem" }}>Research context — not a trade signal, prediction, or recommendation. &quot;Worth researching&quot; means conditions align for further study, nothing more.</div>
      </div>

      {/* Attention / risks */}
      {flags.length > 0 && (
        <div style={{ ...glass, marginBottom: "0.9rem", borderColor: flags.some((f) => f.sev === "red") ? "rgba(248,113,113,0.3)" : "rgba(251,191,36,0.25)" }}>
          <div style={secHeader}><i className="ti ti-alert-triangle" aria-hidden="true" style={{ marginRight: 6 }} />Needs attention</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            {flags.slice(0, 5).map((f, i) => (
              <div key={i} style={{ fontSize: "0.8rem", color: "#b7c5dd", display: "flex", gap: 7, alignItems: "flex-start" }}>
                <i className="ti ti-point-filled" aria-hidden="true" style={{ color: f.sev === "red" ? "#fca5a5" : "#fbbf24", fontSize: 12, marginTop: 2 }} />{f.text}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Research Evidence + Key Events (symbol-tied) */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: "0.9rem", marginBottom: "0.9rem" }}>
        {/* Research Evidence — why this setup matters (tied to Market Focus symbol) */}
        <SectionCard icon="database" title={`Research Evidence (${symbol})`} info="Why GuvFX considers this idea worth researching for the selected market." action={{ href: "/analytics/strategy-lab", label: "Open Strategy Lab" }}>
          {!selection ? <div style={muted}>Select a market to see its research evidence.</div>
            : <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div>
                  <div style={microLabel}>Research Confidence</div>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}><Badge color={conf.color}>{conf.label}</Badge></div>
                  <div style={{ fontSize: "0.76rem", color: "#94a3b8", marginTop: 3 }}>{conf.note}</div>
                </div>
                {top && <div style={{ fontSize: "0.82rem", color: "#e9f4ff" }}>
                  <span style={muted}>Suggested approach</span><br /><span style={{ fontWeight: 500 }}>{topFamily?.label || top.family}</span>{top.name ? ` — ${top.name}` : ""}
                </div>}
                <div>
                  <div style={microLabel}>Historical context</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                    {(selection.rationale && selection.rationale.length ? selection.rationale : (ms?.supporting_evidence || [])).slice(0, 3).map((r, i) => (
                      <div key={i} style={{ fontSize: "0.8rem", color: "#b7c5dd", display: "flex", gap: 7 }}><i className="ti ti-check" aria-hidden="true" style={{ color: "#86efac", fontSize: 13, marginTop: 1 }} />{r}</div>
                    ))}
                    {(!selection.rationale?.length && !(ms?.supporting_evidence || []).length) && <div style={muted}>No supporting research recorded yet for {symbol}.</div>}
                  </div>
                </div>
              </div>}
        </SectionCard>

        {/* Key Events — what external factors may affect this market */}
        <SectionCard icon="calendar-event" title={`Key Events (${symbol})`} info="External factors that may affect this market, from available market-context research.">
          <div style={{ ...microLabel, marginBottom: 6 }}>Relevant to: {ccys.join(" · ")}</div>
          {hasEvent
            ? <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <Badge color={ms?.current_state === "NEWS_SHOCK" ? "red" : "yellow"}>{newsImpact || "HIGH"} impact</Badge>
                  <span style={{ fontSize: "0.82rem", color: "#e9f4ff" }}>{ms?.current_state === "NEWS_SHOCK" ? "High-impact event nearby" : "Elevated news context"}</span>
                </div>
                {newsEvidence && <div style={{ fontSize: "0.8rem", color: "#b7c5dd" }}>{newsEvidence}</div>}
                <div style={{ fontSize: "0.72rem", color: "#64748b" }}>Conditions can move quickly around high-impact events — research-context only.</div>
              </div>
            : <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div style={muted}>No major scheduled events found for this market right now.</div>
                <div style={{ fontSize: "0.72rem", color: "#475569" }}>Based on current market-context research. Always check your economic calendar before trading.</div>
              </div>}
        </SectionCard>
      </div>

      {/* Strategy Insights — observations about my strategies (not advice) */}
      <SectionCard icon="chart-dots" title="Strategy Insights" info="Observations about your strategies from research context. Observational only — not advice." action={{ href: "/strategies", label: "View all" }}>
        {strategies.length === 0 ? <div style={muted}>No strategies yet. <Link href="/strategies/create" style={{ color: "#4ab3ff", textDecoration: "none" }}>Create one →</Link></div>
          : <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {strategies.slice(0, 4).map((s) => {
                const h = health(s); const stage = stageFor(s.id);
                return (
                  <div key={s.id} style={{ borderTop: "1px solid rgba(255,255,255,0.05)", paddingTop: 8 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 6 }}>
                      <Link href={`/strategies/${s.id}`} style={{ fontSize: "0.85rem", color: "#e9f4ff", textDecoration: "none", fontWeight: 500 }}>{s.name}</Link>
                      <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <span style={{ fontSize: "0.68rem", color: "#64748b" }}>{s.symbol_universe || "—"}</span>
                        {stage && <Badge color={stage === "LIVE" ? "green" : "yellow"}>{stage}</Badge>}
                        <Badge color={h.c}>{h.t}</Badge>
                      </span>
                    </div>
                    <div style={{ fontSize: "0.78rem", color: "#94a3b8", marginTop: 4 }}>{insightNote(s)}</div>
                  </div>
                );
              })}
            </div>}
      </SectionCard>

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
