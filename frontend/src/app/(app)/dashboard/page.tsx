"use client";

// =============================================================================
// PX-2.1 — Dashboard refinement: Attention, Meaning & Action.
// Interactive Market Focus card (symbol dropdown + localStorage) reusing
// /api/backtests/strategy-selection/ (embeds market_state). Frontend-only;
// all reads, no execution, no new engines, no mutations.
//
// PX Design Principles v1: Today-focused · attention before info · every
// visual answers a question · What/Why/Now-what · opportunity is hero ·
// market state is supporting evidence · learning > bookkeeping · ambient
// trust · time mandatory · dashboard ≠ Strategy Lab · context before detail.
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
type BalancePoint = { balance_after_trade: number };
type Perf = { mt5_balance_current?: number | null; mt5_equity_current?: number | null; currency?: string; observed_stats?: ObservedStats; balance_series?: BalancePoint[] };
type MarketState = { current_state: string; confidence: string; supporting_evidence: string[] };
type Selection = {
  ok?: boolean; market_state?: MarketState;
  preferred_families?: { family: string; label: string; suitability: string }[];
  preferred_strategies?: { name: string; family: string; suitability: string; kb_avg_quality: number | null; kb_observations: number }[];
  confidence?: string; rationale?: string[]; warnings?: string[];
};
type KBEntry = { symbol: string; template: string; timeframe: string; avg_score: number; confidence: string; confidence_score: number; run_count: number };

// ─── Style ───
const glass: React.CSSProperties = { borderRadius: 14, border: "1px solid rgba(74,179,255,0.12)", background: "linear-gradient(135deg, rgba(10,15,40,0.95) 0%, rgba(5,8,22,0.98) 100%)", padding: "1.1rem 1.25rem" };
const heroGlass: React.CSSProperties = { ...glass, border: "1px solid rgba(74,179,255,0.28)" };
const secHeader: React.CSSProperties = { fontSize: "0.72rem", color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.05em", fontWeight: 600, marginBottom: "0.7rem" };
const muted: React.CSSProperties = { fontSize: "0.78rem", color: "#94a3b8" };
const selStyle: React.CSSProperties = { padding: "0.4rem 0.7rem", background: "#0f172a", border: "1px solid #334155", borderRadius: 8, color: "#e5f4ff", fontSize: "0.9rem" };

const COMMON = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "USDCAD", "BTCUSD", ".US30Cash"];
const LS_KEY = "guvfx.focus.symbol";

function money(n: number | null | undefined, ccy = "") {
  if (n == null) return "—";
  return (n < 0 ? "-" : "") + (ccy ? ccy + " " : "$") + Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 });
}
function stateColor(s: string): "green" | "yellow" | "red" | "blue" | "gray" {
  if (s === "NEWS_SHOCK" || s === "RISK_OFF") return "red";
  if (s.includes("EXHAUSTION") || s === "VOLATILITY_EXPANSION") return "yellow";
  if (s === "TREND_EXPANSION" || s === "RISK_ON" || s === "RANGE_EXPANSION") return "blue";
  return "gray";
}
function nowClock() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }

function Sparkline({ values, color = "#86efac" }: { values: number[]; color?: string }) {
  if (!values || values.length < 2) return <div style={{ height: 30 }} aria-hidden />;
  const w = 150, h = 30, min = Math.min(...values), max = Math.max(...values), range = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - ((v - min) / range) * h}`).join(" ");
  return <svg viewBox={`0 0 ${w} ${h}`} style={{ width: w, height: h }} role="img" aria-label="equity trend"><polyline points={pts} fill="none" stroke={color} strokeWidth="2" /></svg>;
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

export default function DashboardPage() {
  const lang = useLang();
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [mt5, setMt5] = useState<Mt5Status | null>(null);
  const [perf, setPerf] = useState<Perf | null>(null);
  const [kb, setKb] = useState<{ strongest: KBEntry[]; highest_confidence: KBEntry[] } | null>(null);
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
      const [strats, accts, asn, status, knowledge, attr] = await Promise.all([
        apiFetch<Strategy[]>("/api/strategies/strategies/", {}).catch(() => []),
        apiFetch<Account[]>("/api/trading/accounts/", {}).catch(() => []),
        apiFetch<Assignment[]>("/api/strategies/assignments/", {}).catch(() => []),
        apiFetch<Mt5Status>("/api/mt5/status/", {}).catch(() => null),
        apiFetch<{ ok: boolean; strongest: KBEntry[]; highest_confidence: KBEntry[] }>("/api/backtests/research-knowledge/?top_n=1", {}).catch(() => null),
        apiFetch<{ ok: boolean; normalisation_attribution?: Record<string, { avg_max_drawdown: number; observation_count: number }> }>("/api/backtests/feature-attribution/?min_count=3", {}).catch(() => null),
      ]);
      setStrategies(strats || []); setAccounts(accts || []); setAssignments(asn || []); setMt5(status);
      if (knowledge?.ok) setKb({ strongest: knowledge.strongest || [], highest_confidence: knowledge.highest_confidence || [] });
      if (attr?.ok) {
        const tw = attr.normalisation_attribution?.["true"], fa = attr.normalisation_attribution?.["false"];
        if (tw && fa && tw.observation_count >= 3 && tw.avg_max_drawdown >= fa.avg_max_drawdown * 2) setNormFlag(`High-notional setups show elevated drawdown (${tw.avg_max_drawdown}% vs ${fa.avg_max_drawdown}%).`);
      }
      // Symbol selection: localStorage → first assigned → EURUSD
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

  // ── Market Focus: fetch selection on symbol change ──
  useEffect(() => {
    if (!symbol) return;
    try { window.localStorage.setItem(LS_KEY, symbol); } catch { /* ignore */ }
    setSelLoading(true); setSelection(null);
    apiFetch<Selection>(`/api/backtests/strategy-selection/?symbol=${encodeURIComponent(symbol)}&timeframe=H1`, {})
      .then((res) => { setSelection(res?.ok ? res : null); setSelAt(nowClock()); })
      .catch(() => setSelection(null))
      .finally(() => setSelLoading(false));
  }, [symbol]);

  // ── Derived ──
  const primaryAcct = accounts.find((a) => a.is_active) || accounts[0];
  const stats = perf?.observed_stats;
  const equitySeries = (perf?.balance_series || []).map((p) => p.balance_after_trade);
  const netPnl = stats?.net_pnl_total ?? null;
  const rising = equitySeries.length > 1 && equitySeries[equitySeries.length - 1] >= equitySeries[0];
  const trend = netPnl == null ? { label: "No data", color: "#64748b", icon: "minus" }
    : netPnl > 0 && rising ? { label: "Improving", color: "#86efac", icon: "trending-up" }
    : netPnl < 0 ? { label: "Declining", color: "#fca5a5", icon: "trending-down" }
    : { label: "Stable", color: "#fbbf24", icon: "minus" };
  const stageFor = (sid: number) => assignments.find((a) => a.strategy_id === sid)?.stage;

  // Market Focus reason / risk
  const ms = selection?.market_state;
  const top = (selection?.preferred_strategies || [])[0];
  const topFamily = (selection?.preferred_families || [])[0];
  const reason = selection?.rationale?.[0] || (ms ? `${symbol} is in ${ms.current_state} (${ms.confidence} confidence).` : "");
  let risk = "Standard market risk applies.";
  if (ms?.current_state === "NEWS_SHOCK") risk = "A high-impact event is nearby — setups are lower-quality right now.";
  else if ((selection?.warnings || []).length) risk = selection!.warnings![0];
  else if (top && (top.kb_observations || 0) < 3) risk = "Limited historical evidence for this pairing — treat cautiously.";
  const px = selection ? proxy(selection) : null;

  // Attention
  const flags: { sev: "red" | "yellow"; text: string }[] = [];
  if (mt5?.last_status && mt5.last_status !== "SUCCESS") flags.push({ sev: "red", text: `MT5 status: ${mt5.last_status} — check terminal` });
  if (ms?.current_state === "NEWS_SHOCK") flags.push({ sev: "red", text: `${symbol}: high-impact event nearby — setups lower-quality` });
  if (normFlag) flags.push({ sev: "yellow", text: normFlag });
  (selection?.warnings || []).forEach((w) => { if (/news|warn|insufficient/i.test(w)) flags.push({ sev: "yellow", text: `${symbol}: ${w}` }); });

  // Learning insights (deterministic, from observed stats — not raw rows)
  const lessons: string[] = [];
  if (stats) {
    if ((stats.longest_loss_streak || 0) >= 3) lessons.push(`Longest losing streak: ${stats.longest_loss_streak} trades — review whether discipline and position sizing held through it.`);
    if (stats.max_drawdown_pct >= 20) lessons.push(`Max observed drawdown ${stats.max_drawdown_pct}% — consider whether position-size normalization would steady the curve.`);
    if (stats.win_rate_pct < 50 && (stats.net_pnl_total || 0) > 0) lessons.push(`Win rate is ${stats.win_rate_pct}% yet net is positive — reward-to-risk is carrying results more than hit rate.`);
    if (stats.win_rate_pct >= 60 && (stats.net_pnl_total || 0) < 0) lessons.push(`Win rate is ${stats.win_rate_pct}% but net is negative — losers may be larger than winners; review exits.`);
  }
  const setupComplete = strategies.length > 0 && accounts.length > 0;
  const strongest = kb?.strongest?.[0];
  const topConf = kb?.highest_confidence?.[0];

  const SectionCard = useCallback(({ icon, title, children, action }: { icon: string; title: string; children: React.ReactNode; action?: { href: string; label: string } }) => (
    <div style={glass}>
      <div style={{ ...secHeader, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span><i className={`ti ti-${icon}`} aria-hidden="true" style={{ marginRight: 6 }} />{title}</span>
        {action && <Link href={action.href} style={{ fontSize: "0.7rem", color: "#4ab3ff", textTransform: "none", fontWeight: 400, textDecoration: "none" }}>{action.label} →</Link>}
      </div>
      {children}
    </div>
  ), []);

  return (
    <div style={{ maxWidth: 1180, margin: "0 auto" }}>
      {/* Header (Today + voice) */}
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: 8, marginBottom: "0.8rem" }}>
        <h1 style={{ fontSize: "1.5rem", margin: 0, color: "#f0f6ff", fontWeight: 600 }}>Here&apos;s what&apos;s worth your attention today</h1>
        {syncedAt && <span style={{ fontSize: "0.72rem", color: "#64748b" }}>as of {syncedAt}</span>}
      </div>

      {/* 1. Trust ribbon (ambient) */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", fontSize: "0.76rem", color: "#94a3b8", border: "1px solid rgba(255,255,255,0.07)", borderRadius: 10, padding: "0.5rem 0.9rem", marginBottom: "0.9rem" }}>
        <span style={{ display: "flex", alignItems: "center", gap: 5, color: mt5?.last_status === "SUCCESS" ? "#86efac" : "#fbbf24" }}>
          <i className="ti ti-circle-filled" style={{ fontSize: 9 }} aria-hidden="true" />MT5 {mt5?.last_status === "SUCCESS" ? "connected" : (mt5?.last_status || "—")}
        </span>
        {primaryAcct && <span>· {primaryAcct.broker_name || "Broker"} · {primaryAcct.name}</span>}
        {mt5?.server && <span>· {mt5.server}</span>}
        <span>· Equity {money(perf?.mt5_equity_current ?? perf?.mt5_balance_current, perf?.currency)}</span>
        {syncedAt && <span style={{ color: "#64748b" }}>· synced {syncedAt}</span>}
        <Link href="/trading/terminal-access" style={{ marginLeft: "auto", color: "#4ab3ff", textDecoration: "none", fontSize: "0.72rem" }}>Terminal →</Link>
      </div>

      {/* 2. Performance snapshot strip (How am I doing?) */}
      <div style={{ ...glass, display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "1rem", marginBottom: "0.9rem" }}>
        <div>
          <div style={muted}>How am I doing? · observed net PnL</div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: "1.8rem", fontWeight: 700, color: trend.color }}><i className={`ti ti-${trend.icon}`} aria-hidden="true" style={{ fontSize: "1.3rem", marginRight: 4 }} />{netPnl == null ? "—" : money(netPnl, perf?.currency)}</span>
            <Badge color={trend.label === "Improving" ? "green" : trend.label === "Declining" ? "red" : "gray"}>{trend.label}</Badge>
          </div>
          <div style={{ fontSize: "0.72rem", color: "#64748b" }}>{stats ? `${stats.win_rate_pct}% hit rate · ${stats.max_drawdown_pct}% max drawdown · ${stats.total_trades} trades` : t(lang, "legal.microDisclaimer")}</div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={muted}>Balance {money(perf?.mt5_balance_current, perf?.currency)}</div>
          <Sparkline values={equitySeries} color={trend.color} />
          <div style={{ fontSize: "0.68rem", color: "#475569" }}>observed vs MT5 reference</div>
        </div>
      </div>

      {/* 3. Market Focus — HERO (What deserves attention? Why? Now what?) */}
      <div style={{ ...heroGlass, marginBottom: "0.9rem" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 10, marginBottom: "0.8rem" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={secHeader}><i className="ti ti-target" aria-hidden="true" style={{ marginRight: 6 }} />Market Focus</span>
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={selStyle} aria-label="Select symbol">
              {options.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <span style={{ fontSize: "0.7rem", color: "#64748b" }}>{selLoading ? "analysing…" : selAt ? `updated as of ${selAt}` : ""}</span>
        </div>

        {selLoading || !bootLoaded ? <div style={muted}>Analysing {symbol}…</div>
          : !selection ? <div style={muted}>Couldn&apos;t analyse {symbol} right now. Try another symbol.</div>
          : <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: "1rem" }}>
              {/* What */}
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
                  <span style={{ fontSize: "1.1rem", color: "#f0f6ff", fontWeight: 600 }}>{symbol}</span>
                  {ms && <Badge color={stateColor(ms.current_state)}>{ms.current_state}</Badge>}
                  {px && <Badge color={px.color}>{px.label}</Badge>}
                </div>
                {top ? <div style={{ fontSize: "0.85rem", color: "#e9f4ff" }}>
                  Suggested research: <span style={{ fontWeight: 500 }}>{top.name}</span>
                  <div style={muted}>{topFamily?.label || top.family}{top.kb_avg_quality != null ? ` · historical quality ${top.kb_avg_quality} (n=${top.kb_observations})` : " · limited history"}</div>
                </div> : <div style={muted}>No clear strategy fit in this state — watching.</div>}
              </div>
              {/* Why + Now what */}
              <div>
                <div style={{ fontSize: "0.7rem", color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 3 }}>Why this matters</div>
                <div style={{ fontSize: "0.82rem", color: "#b7c5dd", marginBottom: 8 }}>{reason}</div>
                <div style={{ fontSize: "0.7rem", color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 3 }}>Main risk</div>
                <div style={{ fontSize: "0.82rem", color: "#b7c5dd", marginBottom: 10 }}>{risk}</div>
                <Link href="/analytics/strategy-lab" style={{ fontSize: "0.8rem", border: "1px solid rgba(74,179,255,0.4)", borderRadius: 8, padding: "5px 12px", color: "#4ab3ff", textDecoration: "none" }}>Research this setup →</Link>
              </div>
            </div>}
        <div style={{ fontSize: "0.68rem", color: "#475569", marginTop: "0.8rem" }}>Research context — not a trade signal, prediction, or recommendation. &quot;Worth researching&quot; means conditions align for further study, nothing more.</div>
      </div>

      {/* 4. Attention / risks */}
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

      {/* 5 + 6. Strategy Health · Learning insights */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: "0.9rem", marginBottom: "0.9rem" }}>
        <SectionCard icon="heart-rate-monitor" title="Strategy health" action={{ href: "/strategies", label: "All strategies" }}>
          {strategies.length === 0 ? <div style={muted}>No strategies yet. <Link href="/strategies/create" style={{ color: "#4ab3ff", textDecoration: "none" }}>Create one →</Link></div>
            : <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {strategies.slice(0, 5).map((s) => {
                  const stage = stageFor(s.id);
                  return <div key={s.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <Link href={`/strategies/${s.id}`} style={{ fontSize: "0.82rem", color: "#e9f4ff", textDecoration: "none" }}>{s.name}</Link>
                    <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <span style={{ fontSize: "0.68rem", color: "#64748b" }}>{s.symbol_universe || "—"}</span>
                      {stage ? <Badge color={stage === "LIVE" ? "green" : "yellow"}>{stage}</Badge> : <Badge color={s.is_active ? "blue" : "gray"}>{s.is_active ? "Active" : "Idle"}</Badge>}
                    </span>
                  </div>;
                })}
              </div>}
        </SectionCard>

        <SectionCard icon="bulb" title="Learning insights" action={{ href: "/trading/trade-history", label: "Trade history" }}>
          {lessons.length > 0
            ? <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {lessons.map((l, i) => <div key={i} style={{ fontSize: "0.8rem", color: "#b7c5dd", display: "flex", gap: 7 }}><i className="ti ti-bulb" aria-hidden="true" style={{ color: "#fbbf24", fontSize: 13, marginTop: 1 }} />{l}</div>)}
              </div>
            : <div style={muted}>Once more trades are reviewed, GuvFX will highlight lessons here — good setups with poor outcomes, sizing issues, and patterns worth learning from.</div>}
        </SectionCard>
      </div>

      {/* 7. Evidence highlight */}
      <SectionCard icon="database" title="Best idea from research" action={{ href: "/analytics/strategy-lab", label: "Knowledge base" }}>
        {!strongest && !topConf ? <div style={muted}>Run research in Strategy Lab to build the knowledge base.</div>
          : <div style={{ display: "flex", flexWrap: "wrap", gap: "1.5rem" }}>
              {strongest && <div><div style={{ fontSize: "0.7rem", color: "#86efac", textTransform: "uppercase", letterSpacing: "0.04em" }}>Strongest</div><div style={{ fontSize: "0.82rem", color: "#e9f4ff" }}>{strongest.symbol}/{strongest.template}/{strongest.timeframe} · score {strongest.avg_score} <span style={{ color: "#64748b" }}>(n={strongest.run_count})</span></div></div>}
              {topConf && <div><div style={{ fontSize: "0.7rem", color: "#4ab3ff", textTransform: "uppercase", letterSpacing: "0.04em" }}>Highest confidence</div><div style={{ fontSize: "0.82rem", color: "#e9f4ff" }}>{topConf.symbol}/{topConf.template}/{topConf.timeframe} · {topConf.confidence} ({topConf.confidence_score})</div></div>}
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
