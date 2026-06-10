"use client";

// =============================================================================
// PX-2 — Trading Intelligence Command Center
// Intelligence-first dashboard. Frontend-only; reuses existing read-only APIs:
//   /api/backtests/strategy-selection/  (embeds market_state)  → Market Intel,
//        Research Opportunities, Attention
//   /api/analytics/trade-history/       → Command Strip, Performance, Review
//   /api/mt5/status/                    → Trust ribbon, sync attention
//   /api/backtests/feature-attribution/ → Attention (normalisation), warnings
//   /api/backtests/research-knowledge/  → Knowledge Base highlight
//   /api/strategies/strategies/, /assignments/, /api/trading/accounts/
// No execution, no new engines, no mutations.
// =============================================================================

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";
import { apiFetch } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";

// ─────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────
type Strategy = { id: number; name: string; symbol_universe?: string; is_active?: boolean; template?: string };
type Account = { id: number; name: string; broker_name?: string; is_active?: boolean };
type Assignment = { strategy_id: number; account_id: number; stage?: string };
type Mt5Status = { login?: string; server?: string; last_status?: string | null };
type ObservedStats = {
  total_trades: number; win_rate_pct: number; max_drawdown_pct: number;
  net_pnl_total: number; longest_loss_streak?: number;
};
type BalancePoint = { balance_after_trade: number };
type TradeRow = { trade_closed?: string; net_pnl_money?: number; symbol?: string; strategy_name?: string };
type Perf = {
  mt5_balance_current?: number | null; mt5_equity_current?: number | null;
  currency?: string; observed_stats?: ObservedStats; balance_series?: BalancePoint[]; trades?: TradeRow[];
};
type MarketState = { current_state: string; confidence: string; supporting_evidence: string[] };
type Selection = {
  ok?: boolean; market_state?: MarketState;
  preferred_families?: { family: string; label: string; suitability: string }[];
  preferred_strategies?: { name: string; family: string; suitability: string; kb_avg_quality: number | null; kb_observations: number }[];
  confidence?: string; warnings?: string[];
};
type KBEntry = { symbol: string; template: string; timeframe: string; avg_score: number; confidence: string; confidence_score: number; run_count: number };

// ─────────────────────────────────────────────────────────────────
// Styles
// ─────────────────────────────────────────────────────────────────
const glass: React.CSSProperties = {
  borderRadius: 14, border: "1px solid rgba(74,179,255,0.12)",
  background: "linear-gradient(135deg, rgba(10,15,40,0.95) 0%, rgba(5,8,22,0.98) 100%)",
  padding: "1.1rem 1.25rem",
};
const secHeader: React.CSSProperties = {
  fontSize: "0.72rem", color: "#94a3b8", textTransform: "uppercase",
  letterSpacing: "0.05em", fontWeight: 600, marginBottom: "0.7rem",
};
const muted: React.CSSProperties = { fontSize: "0.78rem", color: "#94a3b8" };

function money(n: number | null | undefined, ccy = "") {
  if (n == null) return "—";
  return (n < 0 ? "-" : "") + (ccy ? ccy + " " : "$") + Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function stateColor(state: string): "green" | "yellow" | "red" | "blue" | "gray" {
  if (state === "NEWS_SHOCK" || state === "RISK_OFF") return "red";
  if (state.includes("EXHAUSTION") || state === "VOLATILITY_EXPANSION") return "yellow";
  if (state === "TREND_EXPANSION" || state === "RISK_ON" || state === "RANGE_EXPANSION") return "blue";
  return "gray";
}

function Sparkline({ values, color = "#86efac" }: { values: number[]; color?: string }) {
  if (!values || values.length < 2) return <div style={{ height: 34 }} />;
  const w = 160, h = 34, min = Math.min(...values), max = Math.max(...values), range = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - ((v - min) / range) * h}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: w, height: h }} role="img" aria-label="equity trend">
      <polyline points={pts} fill="none" stroke={color} strokeWidth="2" />
    </svg>
  );
}

function parseWatched(strategies: Strategy[]): string[] {
  const set = new Set<string>();
  for (const s of strategies) {
    (s.symbol_universe || "").split(/[,;\s]+/).forEach((x) => { const v = x.trim(); if (v) set.add(v); });
  }
  let arr = [...set];
  if (arr.length === 0) arr = ["EURUSD", "XAUUSD", "USDCAD", "BTCUSD"];
  return arr.slice(0, 4);
}

// ─────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────
export default function DashboardPage() {
  const lang = useLang();

  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [mt5, setMt5] = useState<Mt5Status | null>(null);
  const [perf, setPerf] = useState<Perf | null>(null);
  const [watched, setWatched] = useState<string[]>([]);
  const [selections, setSelections] = useState<Record<string, Selection | "loading" | null>>({});
  const [normFlag, setNormFlag] = useState<string | null>(null);
  const [kb, setKb] = useState<{ strongest: KBEntry[]; highest_confidence: KBEntry[] } | null>(null);
  const [syncedAt, setSyncedAt] = useState<string>("");
  const [bootLoaded, setBootLoaded] = useState(false);

  // ── Boot: fast endpoints ──
  useEffect(() => {
    (async () => {
      const [strats, accts, asn, status, knowledge, attr] = await Promise.all([
        apiFetch<Strategy[]>("/api/strategies/strategies/", {}).catch(() => []),
        apiFetch<Account[]>("/api/trading/accounts/", {}).catch(() => []),
        apiFetch<Assignment[]>("/api/strategies/assignments/", {}).catch(() => []),
        apiFetch<Mt5Status>("/api/mt5/status/", {}).catch(() => null),
        apiFetch<{ ok: boolean; strongest: KBEntry[]; highest_confidence: KBEntry[] }>("/api/backtests/research-knowledge/?top_n=1", {}).catch(() => null),
        apiFetch<{ ok: boolean; normalisation_attribution?: Record<string, { avg_max_drawdown: number; weak_rate: number; observation_count: number }>; warnings?: string[] }>("/api/backtests/feature-attribution/?min_count=3", {}).catch(() => null),
      ]);
      setStrategies(strats || []);
      setAccounts(accts || []);
      setAssignments(asn || []);
      setMt5(status);
      if (knowledge?.ok) setKb({ strongest: knowledge.strongest || [], highest_confidence: knowledge.highest_confidence || [] });
      if (attr?.ok) {
        const tw = attr.normalisation_attribution?.["true"];
        const fa = attr.normalisation_attribution?.["false"];
        if (tw && fa && tw.observation_count >= 3 && tw.avg_max_drawdown >= fa.avg_max_drawdown * 2) {
          setNormFlag(`High-notional setups show elevated drawdown (${tw.avg_max_drawdown}% vs ${fa.avg_max_drawdown}%).`);
        }
      }
      const w = parseWatched(strats || []);
      setWatched(w);
      setSelections(Object.fromEntries(w.map((s) => [s, "loading" as const])));
      setBootLoaded(true);

      // Primary account performance (first active, else first)
      const primary = (accts || []).find((a) => a.is_active) || (accts || [])[0];
      if (primary) {
        apiFetch<Perf>(`/api/analytics/trade-history/?account_id=${primary.id}`, {})
          .then((p) => { setPerf(p); setSyncedAt(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })); })
          .catch(() => {});
      } else {
        setSyncedAt(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      }
    })();
  }, []);

  // ── Progressive: strategy-selection per watched symbol (slow, bar-fetch) ──
  useEffect(() => {
    if (!bootLoaded || watched.length === 0) return;
    watched.forEach((sym) => {
      apiFetch<Selection>(`/api/backtests/strategy-selection/?symbol=${encodeURIComponent(sym)}&timeframe=H1`, {})
        .then((res) => setSelections((prev) => ({ ...prev, [sym]: res?.ok ? res : null })))
        .catch(() => setSelections((prev) => ({ ...prev, [sym]: null })));
    });
  }, [bootLoaded, watched]);

  // ── Derive ──
  const primaryAcct = accounts.find((a) => a.is_active) || accounts[0];
  const stats = perf?.observed_stats;
  const equitySeries = (perf?.balance_series || []).map((p) => p.balance_after_trade);
  const netPnl = stats?.net_pnl_total ?? null;
  const stageFor = (sid: number) => assignments.find((a) => a.strategy_id === sid)?.stage;

  // Attention flags (from selections + mt5 + attribution)
  const flags: { sev: "red" | "yellow"; text: string }[] = [];
  if (mt5 && mt5.last_status && mt5.last_status !== "SUCCESS") flags.push({ sev: "red", text: `MT5 status: ${mt5.last_status} — check terminal` });
  Object.entries(selections).forEach(([sym, sel]) => {
    if (sel && sel !== "loading") {
      if (sel.market_state?.current_state === "NEWS_SHOCK") flags.push({ sev: "red", text: `${sym}: NEWS_SHOCK — high-impact event nearby` });
      (sel.warnings || []).forEach((w) => { if (/news|warn|insufficient/i.test(w)) flags.push({ sev: "yellow", text: `${sym}: ${w}` }); });
    }
  });
  if (normFlag) flags.push({ sev: "yellow", text: normFlag });

  // Opportunities: best candidate per symbol (suitability-ranked, dedup)
  const opportunities = Object.entries(selections)
    .filter(([, s]) => s && s !== "loading")
    .map(([sym, s]) => {
      const sel = s as Selection;
      const top = (sel.preferred_strategies || [])[0];
      return top ? { sym, name: top.name, family: top.family, suitability: top.suitability, q: top.kb_avg_quality, n: top.kb_observations, state: sel.market_state?.current_state } : null;
    })
    .filter(Boolean) as { sym: string; name: string; family: string; suitability: string; q: number | null; n: number; state?: string }[];
  opportunities.sort((a, b) => ({ HIGH: 3, MEDIUM: 2, LOW: 1 }[b.suitability] || 0) - ({ HIGH: 3, MEDIUM: 2, LOW: 1 }[a.suitability] || 0));

  // Onboarding (only if incomplete)
  const setupComplete = strategies.length > 0 && accounts.length > 0;
  const recentTrades = (perf?.trades || []).slice(-3).reverse();
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
      {/* ── Trust ribbon ── */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", fontSize: "0.76rem", color: "#94a3b8", border: "1px solid rgba(255,255,255,0.07)", borderRadius: 10, padding: "0.5rem 0.9rem", marginBottom: "0.9rem" }}>
        <span style={{ display: "flex", alignItems: "center", gap: 5, color: mt5?.last_status === "SUCCESS" ? "#86efac" : "#fbbf24" }}>
          <i className="ti ti-circle-filled" style={{ fontSize: 9 }} aria-hidden="true" />
          MT5 {mt5?.last_status === "SUCCESS" ? "connected" : (mt5?.last_status || "—")}
        </span>
        {primaryAcct && <span>· {primaryAcct.broker_name || "Broker"} · {primaryAcct.name}</span>}
        {mt5?.server && <span>· {mt5.server}</span>}
        <span>· Equity {money(perf?.mt5_equity_current ?? perf?.mt5_balance_current, perf?.currency)}</span>
        {syncedAt && <span style={{ color: "#64748b" }}>· synced {syncedAt}</span>}
        <Link href="/trading/terminal-access" style={{ marginLeft: "auto", color: "#4ab3ff", textDecoration: "none", fontSize: "0.72rem" }}>Terminal →</Link>
      </div>

      {/* ── Command strip ── */}
      <div style={{ ...glass, display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "1rem", marginBottom: "0.9rem" }}>
        <div>
          <div style={muted}>{t(lang, "dashboard.title")} · Net PnL (observed)</div>
          <div style={{ fontSize: "1.9rem", fontWeight: 700, color: netPnl == null ? "#64748b" : netPnl >= 0 ? "#86efac" : "#fca5a5" }}>
            {netPnl == null ? "—" : (netPnl >= 0 ? "▲ " : "▼ ") + money(netPnl, perf?.currency)}
          </div>
          <div style={{ fontSize: "0.72rem", color: "#64748b" }}>{t(lang, "legal.microDisclaimer")}</div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={muted}>Balance {money(perf?.mt5_balance_current, perf?.currency)} · Equity {money(perf?.mt5_equity_current ?? perf?.mt5_balance_current, perf?.currency)}</div>
          <Sparkline values={equitySeries} color={netPnl != null && netPnl < 0 ? "#fca5a5" : "#86efac"} />
        </div>
      </div>

      {/* ── Above fold: Market Intel · Opportunities · Attention ── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "0.9rem", marginBottom: "0.9rem" }}>
        {/* Market Intelligence */}
        <SectionCard icon="radar-2" title="Market Intelligence" action={{ href: "/analytics/strategy-lab", label: "Strategy Lab" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {watched.map((sym) => {
              const sel = selections[sym];
              const ms = sel && sel !== "loading" ? sel.market_state : undefined;
              return (
                <div key={sym} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <Link href={`/analytics/strategy-lab`} style={{ fontSize: "0.85rem", color: "#e9f4ff", textDecoration: "none" }}>{sym}</Link>
                  {sel === "loading" || sel === undefined ? <span style={{ fontSize: "0.72rem", color: "#64748b" }}>analysing…</span>
                    : ms ? <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <Badge color={stateColor(ms.current_state)}>{ms.current_state}</Badge>
                        <span style={{ fontSize: "0.68rem", color: "#64748b" }}>{ms.confidence}</span>
                      </span>
                    : <span style={{ fontSize: "0.72rem", color: "#64748b" }}>—</span>}
                </div>
              );
            })}
          </div>
        </SectionCard>

        {/* Research Opportunities */}
        <SectionCard icon="bulb" title="Research Opportunities" action={{ href: "/analytics/strategy-lab", label: "Research" }}>
          {opportunities.length === 0 ? <div style={muted}>{Object.values(selections).some((s) => s === "loading") ? "Scanning watched symbols…" : "No clear candidates right now."}</div>
            : <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {opportunities.slice(0, 3).map((o, i) => (
                  <div key={i}>
                    <div style={{ fontSize: "0.85rem", color: "#e9f4ff", fontWeight: 500 }}>{o.sym} → {o.name}</div>
                    <div style={{ fontSize: "0.72rem", color: "#94a3b8" }}>
                      {o.family} · {o.suitability}{o.q != null ? ` · hist. quality ${o.q} (n=${o.n})` : " · limited history"}{o.state ? ` · ${o.state}` : ""}
                    </div>
                  </div>
                ))}
                <div style={{ display: "flex", gap: 6 }}>
                  <Link href="/analytics/strategy-lab" style={{ fontSize: "0.72rem", border: "1px solid rgba(74,179,255,0.3)", borderRadius: 6, padding: "2px 8px", color: "#4ab3ff", textDecoration: "none" }}>Open Strategy Lab</Link>
                  <Link href="/backtests" style={{ fontSize: "0.72rem", border: "1px solid rgba(74,179,255,0.3)", borderRadius: 6, padding: "2px 8px", color: "#4ab3ff", textDecoration: "none" }}>Run Backtest</Link>
                </div>
              </div>}
        </SectionCard>

        {/* Attention */}
        <SectionCard icon="alert-triangle" title="Attention">
          {flags.length === 0 ? <div style={muted}>{bootLoaded ? "No flags right now." : "Loading…"}</div>
            : <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {flags.slice(0, 6).map((f, i) => (
                  <div key={i} style={{ fontSize: "0.78rem", color: "#b7c5dd", display: "flex", gap: 7, alignItems: "flex-start" }}>
                    <i className="ti ti-point-filled" aria-hidden="true" style={{ color: f.sev === "red" ? "#fca5a5" : "#fbbf24", fontSize: 12, marginTop: 2 }} />
                    <span>{f.text}</span>
                  </div>
                ))}
              </div>}
        </SectionCard>
      </div>

      {/* ── Below fold: Performance · Review Queue ── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: "0.9rem", marginBottom: "0.9rem" }}>
        <SectionCard icon="chart-line" title="Performance Snapshot" action={{ href: "/trading/trade-history", label: "Full history" }}>
          <Sparkline values={equitySeries} color={netPnl != null && netPnl < 0 ? "#fca5a5" : "#86efac"} />
          <div style={{ ...muted, marginTop: 8 }}>
            {stats ? <>Net {money(stats.net_pnl_total, perf?.currency)} · hit rate {stats.win_rate_pct}% · max DD {stats.max_drawdown_pct}% · {stats.total_trades} trades</> : "No trade data yet."}
            {perf?.mt5_balance_current != null && <span style={{ color: "#64748b" }}> (vs MT5 ref)</span>}
          </div>
        </SectionCard>

        <SectionCard icon="clipboard-check" title={`Review Queue${recentTrades.length ? ` · ${recentTrades.length}` : ""}`} action={{ href: "/trading/trade-history", label: "Review" }}>
          {recentTrades.length === 0 ? <div style={muted}>No recent closed trades to review.</div>
            : <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {recentTrades.map((tr, i) => {
                  const pnl = tr.net_pnl_money ?? 0;
                  return (
                    <div key={i} style={{ fontSize: "0.8rem", color: "#b7c5dd" }}>
                      <span style={{ color: pnl >= 0 ? "#86efac" : "#fca5a5" }}>{pnl >= 0 ? "▲ " : "▼ "}{money(pnl, perf?.currency)}</span>
                      {" "}{tr.symbol || "—"}{tr.strategy_name ? ` · ${tr.strategy_name}` : ""}
                    </div>
                  );
                })}
                <div style={{ fontSize: "0.7rem", color: "#64748b", fontStyle: "italic" }}>
                  Review prompt: did decision quality match the outcome? A good setup can lose; a poor setup can win.
                </div>
              </div>}
        </SectionCard>
      </div>

      {/* ── Below fold: Strategy Health · Knowledge Base ── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: "0.9rem", marginBottom: "0.9rem" }}>
        <SectionCard icon="heart-rate-monitor" title="Strategy Health" action={{ href: "/strategies", label: "All strategies" }}>
          {strategies.length === 0 ? <div style={muted}>No strategies yet. <Link href="/strategies/create" style={{ color: "#4ab3ff", textDecoration: "none" }}>Create one →</Link></div>
            : <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {strategies.slice(0, 5).map((s) => {
                  const stage = stageFor(s.id);
                  return (
                    <div key={s.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <Link href={`/strategies/${s.id}`} style={{ fontSize: "0.82rem", color: "#e9f4ff", textDecoration: "none" }}>{s.name}</Link>
                      <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <span style={{ fontSize: "0.68rem", color: "#64748b" }}>{s.symbol_universe || "—"}</span>
                        {stage ? <Badge color={stage === "LIVE" ? "green" : "yellow"}>{stage}</Badge>
                          : <Badge color={s.is_active ? "blue" : "gray"}>{s.is_active ? "Active" : "Idle"}</Badge>}
                      </span>
                    </div>
                  );
                })}
              </div>}
        </SectionCard>

        <SectionCard icon="database" title="Knowledge Base Highlight" action={{ href: "/analytics/strategy-lab", label: "Knowledge Base" }}>
          {!strongest && !topConf ? <div style={muted}>Run research to populate the knowledge base.</div>
            : <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {strongest && <div>
                  <div style={{ fontSize: "0.7rem", color: "#86efac", textTransform: "uppercase", letterSpacing: "0.04em" }}>Strongest</div>
                  <div style={{ fontSize: "0.82rem", color: "#e9f4ff" }}>{strongest.symbol}/{strongest.template}/{strongest.timeframe} · score {strongest.avg_score} <span style={{ color: "#64748b" }}>(n={strongest.run_count})</span></div>
                </div>}
                {topConf && <div>
                  <div style={{ fontSize: "0.7rem", color: "#4ab3ff", textTransform: "uppercase", letterSpacing: "0.04em" }}>Highest confidence</div>
                  <div style={{ fontSize: "0.82rem", color: "#e9f4ff" }}>{topConf.symbol}/{topConf.template}/{topConf.timeframe} · {topConf.confidence} ({topConf.confidence_score})</div>
                </div>}
              </div>}
        </SectionCard>
      </div>

      {/* ── Onboarding (only if incomplete) ── */}
      {bootLoaded && !setupComplete && (
        <div style={{ ...glass, marginBottom: "0.5rem" }}>
          <div style={secHeader}><i className="ti ti-rocket" aria-hidden="true" style={{ marginRight: 6 }} />Get set up</div>
          <div style={{ display: "flex", gap: "1.25rem", flexWrap: "wrap", fontSize: "0.82rem" }}>
            <span style={{ color: strategies.length ? "#86efac" : "#94a3b8" }}>{strategies.length ? "✓" : "○"} <Link href="/strategies/create" style={{ color: "inherit", textDecoration: "none" }}>Create a strategy</Link></span>
            <span style={{ color: accounts.length ? "#86efac" : "#94a3b8" }}>{accounts.length ? "✓" : "○"} <Link href="/accounts" style={{ color: "inherit", textDecoration: "none" }}>Link an account</Link></span>
            <span style={{ color: "#94a3b8" }}>○ <Link href="/backtests" style={{ color: "inherit", textDecoration: "none" }}>Run a backtest</Link></span>
          </div>
        </div>
      )}

      <div style={{ fontSize: "0.7rem", color: "#475569", padding: "0.5rem 0" }}>
        Trading Intelligence — research context from historical observations and strategy criteria. Not a prediction, signal, or recommendation to trade.
      </div>
    </div>
  );
}
