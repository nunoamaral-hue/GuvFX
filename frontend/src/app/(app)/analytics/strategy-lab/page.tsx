"use client";

import { useEffect, useState, useCallback } from "react";
import { apiFetch } from "@/lib/api";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";

// ─────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────

type Template = {
  name: string;
  description: string;
  default_params: Record<string, number>;
};

type RegimeData = {
  current_regime: string;
  regime_pct: Record<string, number>;
  persistence: Record<string, number>;
  transition_matrix: { probabilities: Record<string, Record<string, number>> };
};

type Comparison = {
  baseline_trades: number;
  filtered_trades: number;
  skipped_trades: number;
  baseline_net_profit: number;
  filtered_net_profit: number;
  net_profit_diff: number;
  baseline_profit_factor: number;
  filtered_profit_factor: number;
  profit_factor_diff: number;
  baseline_win_rate: number;
  filtered_win_rate: number;
  baseline_max_drawdown: number;
  filtered_max_drawdown: number;
  improvement_pct: number;
  filter_verdict: string;
};

type OptResult = {
  rank: number;
  params: Record<string, number>;
  score: number;
  trade_count: number;
  net_profit: number;
  win_rate: number;
  profit_factor: number;
  max_drawdown: number;
};

// B16 — Feature framework: normalised market context
type FeatureContext = {
  available?: boolean;
  trend?: { ema_slope: number; ema_distance: number; trend_strength: number; trend_state: string };
  volatility?: { atr_value: number; atr_percentile: number; volatility_state: string; volatility_expansion: string };
  session?: { session_bucket: string; asian_range: number; london_range: number; ny_range: number };
  structure?: { distance_to_20_high: number; distance_to_50_high: number; breakout_state: string };
  normalisation?: {
    tick_size: number; tick_value: number; contract_size: number;
    notional_per_001_lot: number; atr_dollars_per_001_lot: number;
    pnl_model: string; position_size_warning: boolean;
    position_size_warning_reasons?: string[]; warning_text?: string;
  };
  snapshot?: { trend_state: string; volatility_state: string; session_profile: string; breakout_state: string; position_size_warning: boolean };
  // B16.5 — economic event context (factual metadata only)
  news?: {
    impact: string;
    event_relevance?: string;
    event_type?: string;
    event_name?: string;
    currency?: string;
    minutes_to_event?: number;
    minutes_since_event?: number;
    is_upcoming?: boolean;
  };
};

type WFResult = {
  rank: number;
  params: Record<string, number>;
  train_score: number;
  validation_score: number;
  degradation_pct: number;
  robust: boolean;
};

// ─────────────────────────────────────────────────────────────────
// Styles
// ─────────────────────────────────────────────────────────────────

const glass: React.CSSProperties = {
  borderRadius: 14, border: "1px solid rgba(74,179,255,0.12)",
  background: "linear-gradient(135deg, rgba(10,15,40,0.95) 0%, rgba(5,8,22,0.98) 100%)",
  padding: "1.25rem", marginBottom: "1rem",
};
const th: React.CSSProperties = { padding: "0.4rem 0.75rem", fontSize: "0.75rem", color: "#94a3b8", textAlign: "left" as const, borderBottom: "1px solid rgba(74,179,255,0.08)" };
const td: React.CSSProperties = { padding: "0.4rem 0.75rem", fontSize: "0.85rem", color: "#e9f4ff", borderBottom: "1px solid rgba(255,255,255,0.03)" };
const numTd: React.CSSProperties = { ...td, fontFamily: "monospace", textAlign: "right" as const };
const secHeader: React.CSSProperties = { fontSize: "0.78rem", color: "#94a3b8", textTransform: "uppercase" as const, letterSpacing: "0.05em", fontWeight: 600, marginBottom: "0.6rem" };

function pct(n: number) { return n.toFixed(1) + "%"; }
function dollar(n: number) { return (n >= 0 ? "$" : "-$") + Math.abs(n).toFixed(2); }

// ─────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────

export default function StrategyLabPage() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState("rsi_mean_reversion");
  const [symbol, setSymbol] = useState("EURUSD");
  const [timeframe, setTimeframe] = useState("H1");

  const [regime, setRegime] = useState<RegimeData | null>(null);
  const [regimePerf, setRegimePerf] = useState<Record<string, { trades: number; net_profit: number; win_rate: number; profit_factor: number }> | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [featureContext, setFeatureContext] = useState<FeatureContext | null>(null);
  const [optResults, setOptResults] = useState<OptResult[]>([]);
  const [wfResults, setWfResults] = useState<WFResult[]>([]);
  const [optWarnings, setOptWarnings] = useState<string[]>([]);

  // Recommendations state
  type RecItem = {
    category: string; priority: string; confidence: string;
    title: string; evidence: string[]; suggested_next_action: string;
  };
  const [recommendations, setRecommendations] = useState<RecItem[]>([]);
  const [recSummary, setRecSummary] = useState<Record<string, number>>({});

  // B14: Knowledge Base state
  type KBEntry = {
    symbol: string; template: string; timeframe: string;
    run_count: number; avg_score: number; avg_pf: number;
    avg_drawdown: number; avg_return: number; avg_win_rate: number;
    confidence: string; confidence_score: number;
    robustness_pct: number; best_score: number; worst_score: number;
    last_score: number; last_observed: string;
    wf_run_count: number; wf_robust_pct: number;
    score_stddev: number;
    regime_distribution: Record<string, number>;
  };
  const [kbStrongest, setKbStrongest] = useState<KBEntry[]>([]);
  const [kbMostTested, setKbMostTested] = useState<KBEntry[]>([]);
  const [kbHighConf, setKbHighConf] = useState<KBEntry[]>([]);
  const [kbTotalObs, setKbTotalObs] = useState(0);
  const [kbTotalCombos, setKbTotalCombos] = useState(0);

  const [loading, setLoading] = useState("");
  const [error, setError] = useState("");

  // Fetch templates
  useEffect(() => {
    apiFetch<{ templates: Template[] }>("/api/backtests/templates/", {})
      .then((d) => setTemplates(d.templates || []))
      .catch(() => {});
  }, []);

  // Run all analyses
  const runAll = useCallback(async () => {
    setLoading("Running analyses...");
    setError("");
    setRegime(null); setRegimePerf(null); setComparison(null); setFeatureContext(null);
    setOptResults([]); setWfResults([]); setOptWarnings([]);
    setRecommendations([]); setRecSummary({});
    setKbStrongest([]); setKbMostTested([]); setKbHighConf([]);
    setKbTotalObs(0); setKbTotalCombos(0);

    try {
      // 1. Regime analysis + filtered backtest
      setLoading("Regime analysis...");
      const regimeRes = await apiFetch<RegimeData & { ok: boolean }>("/api/backtests/regime-analysis/", {
        method: "POST",
        body: JSON.stringify({ symbol, timeframe, bar_count: 1000, lookback: 20, k: 1.0 }),
      });
      if (regimeRes.ok) setRegime(regimeRes);

      // 2. Regime filter comparison
      setLoading("Regime filter comparison...");
      const blocked = selectedTemplate === "ema_trend" ? ["SIDEWAYS"] : ["BEAR"];
      const allowed = ["BULL", "SIDEWAYS", "BEAR"].filter((r) => !blocked.includes(r));
      const filterRes = await apiFetch<{
        ok: boolean;
        comparison: Comparison;
        feature_context: FeatureContext;
        regime_skip_analysis: Record<string, unknown>;
      }>("/api/backtests/regime-filter/", {
        method: "POST",
        body: JSON.stringify({
          template_name: selectedTemplate, symbol, timeframe, bar_count: 1000,
          regime_filter: { allowed_entry_regimes: allowed },
        }),
      });
      if (filterRes.ok) {
        setComparison(filterRes.comparison);
        if (filterRes.feature_context?.available) setFeatureContext(filterRes.feature_context);
      }

      // 3. Quick backtest for regime performance
      setLoading("Strategy backtest...");
      const btRes = await apiFetch<{ ok: boolean; comparison: Comparison }>("/api/backtests/regime-filter/", {
        method: "POST",
        body: JSON.stringify({
          template_name: selectedTemplate, symbol, timeframe, bar_count: 1000,
          regime_filter: { allowed_entry_regimes: ["BULL", "SIDEWAYS", "BEAR"] }, // no filter = baseline
        }),
      });
      // Extract regime perf from baseline metrics if available
      // (regime data is in the baseline backtest metrics)

      // 4. Optimisation (small grid)
      setLoading("Optimising parameters...");
      const tmpl = templates.find((t) => t.name === selectedTemplate);
      const defaultP = tmpl?.default_params || {};
      // Build small param grid from defaults
      const grid: Record<string, number[]> = {};
      for (const [k, v] of Object.entries(defaultP)) {
        if (typeof v === "number" && v > 0) {
          grid[k] = [Math.round(v * 0.7), v, Math.round(v * 1.3)].filter((x) => x > 0);
        }
      }

      const optRes = await apiFetch<{
        ok: boolean;
        top_results: OptResult[];
        walk_forward: WFResult[];
        warnings: string[];
      }>("/api/backtests/optimise/", {
        method: "POST",
        body: JSON.stringify({
          template_name: selectedTemplate, symbol, timeframe, bar_count: 1000,
          score_metric: "profit_factor", param_grid: grid, walk_forward: true, top_n: 5,
        }),
      });
      if (optRes.ok) {
        setOptResults(optRes.top_results || []);
        setWfResults(optRes.walk_forward || []);
        setOptWarnings(optRes.warnings || []);
      }

      // 5. Research recommendations
      setLoading("Generating recommendations...");
      try {
        const recRes = await apiFetch<{
          ok: boolean;
          recommendations: RecItem[];
          summary: Record<string, number>;
        }>("/api/backtests/research-recommendations/", {
          method: "POST",
          body: JSON.stringify({
            scope: "all",
            symbols: [symbol],
            templates: [selectedTemplate],
            include_regime: true,
            include_portfolio: false,
          }),
        });
        if (recRes.ok) {
          setRecommendations(recRes.recommendations || []);
          setRecSummary(recRes.summary || {});
        }
      } catch { /* non-blocking */ }

      // 6. Knowledge Base (fetch after observations have been recorded)
      setLoading("Loading knowledge base...");
      try {
        const kbRes = await apiFetch<{
          ok: boolean;
          strongest: KBEntry[];
          most_tested: KBEntry[];
          highest_confidence: KBEntry[];
          total_observations: number;
          total_combinations: number;
        }>(`/api/backtests/research-knowledge/?symbol=${symbol}&top_n=5`, {});
        if (kbRes.ok) {
          setKbStrongest(kbRes.strongest || []);
          setKbMostTested(kbRes.most_tested || []);
          setKbHighConf(kbRes.highest_confidence || []);
          setKbTotalObs(kbRes.total_observations || 0);
          setKbTotalCombos(kbRes.total_combinations || 0);
        }
      } catch { /* non-blocking */ }

      setLoading("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Analysis failed");
      setLoading("");
    }
  }, [selectedTemplate, symbol, timeframe, templates]);

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Strategy Lab</h1>
      <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "0.25rem" }}>
        Research Mode — simulated results using MT5 OHLC data
      </p>
      <p style={{ fontSize: "0.72rem", color: "#64748b", marginBottom: "1.25rem" }}>
        Results may differ from MT5 Strategy Tester or live execution. Not financial advice.
      </p>

      {/* ── Controls ── */}
      <div style={{ ...glass, display: "flex", gap: "1rem", alignItems: "flex-end", flexWrap: "wrap" }}>
        <div>
          <label style={{ display: "block", fontSize: "0.78rem", color: "#94a3b8", marginBottom: 4 }}>Template</label>
          <select value={selectedTemplate} onChange={(e) => setSelectedTemplate(e.target.value)}
            style={{ padding: "0.45rem 0.7rem", background: "#0f172a", border: "1px solid #334155", borderRadius: 6, color: "#e5f4ff", fontSize: "0.85rem" }}>
            {templates.map((t) => <option key={t.name} value={t.name}>{t.description.split(".")[0]}</option>)}
          </select>
        </div>
        <div>
          <label style={{ display: "block", fontSize: "0.78rem", color: "#94a3b8", marginBottom: 4 }}>Symbol</label>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}
            style={{ padding: "0.45rem 0.7rem", background: "#0f172a", border: "1px solid #334155", borderRadius: 6, color: "#e5f4ff", fontSize: "0.85rem" }}>
            <option>EURUSD</option><option>GBPUSD</option><option>XAUUSD</option>
          </select>
        </div>
        <div>
          <label style={{ display: "block", fontSize: "0.78rem", color: "#94a3b8", marginBottom: 4 }}>Timeframe</label>
          <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}
            style={{ padding: "0.45rem 0.7rem", background: "#0f172a", border: "1px solid #334155", borderRadius: 6, color: "#e5f4ff", fontSize: "0.85rem" }}>
            <option>M15</option><option>M30</option><option>H1</option><option>H4</option>
          </select>
        </div>
        <Button onClick={runAll} disabled={!!loading}>
          {loading || "Run Analysis"}
        </Button>
      </div>

      {error && <div style={{ ...glass, borderColor: "rgba(248,113,113,0.3)", color: "#f87171", fontSize: "0.85rem" }}>{error}</div>}

      {/* ── Market Context (B16 Feature Framework) ── */}
      {featureContext?.available && (
        <div style={glass}>
          <div style={secHeader}>Market Context <span style={{ textTransform: "none", color: "#64748b", fontWeight: 400 }}>— normalised research features</span></div>
          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", marginBottom: "0.75rem" }}>
            <Badge color={featureContext.snapshot?.trend_state?.includes("up") ? "green" : featureContext.snapshot?.trend_state?.includes("down") ? "red" : "gray"}>
              trend: {featureContext.snapshot?.trend_state || "—"}
            </Badge>
            <Badge color={featureContext.snapshot?.volatility_state === "high" ? "yellow" : featureContext.snapshot?.volatility_state === "low" ? "gray" : "blue"}>
              volatility: {featureContext.snapshot?.volatility_state || "—"} ({featureContext.volatility?.volatility_expansion})
            </Badge>
            <Badge color="blue">breakout: {featureContext.snapshot?.breakout_state || "—"}</Badge>
            <Badge color="gray">session: {featureContext.snapshot?.session_profile || "—"}</Badge>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: "0.5rem", marginBottom: "0.75rem" }}>
            <div style={{ fontSize: "0.78rem", color: "#b7c5dd" }}>
              <div style={{ color: "#64748b", fontSize: "0.7rem" }}>TREND</div>
              strength {featureContext.trend?.trend_strength} · slope {featureContext.trend?.ema_slope} · dist {featureContext.trend?.ema_distance} ATR
            </div>
            <div style={{ fontSize: "0.78rem", color: "#b7c5dd" }}>
              <div style={{ color: "#64748b", fontSize: "0.7rem" }}>VOLATILITY</div>
              ATR {featureContext.volatility?.atr_value} · pct {featureContext.volatility?.atr_percentile}%
            </div>
            <div style={{ fontSize: "0.78rem", color: "#b7c5dd" }}>
              <div style={{ color: "#64748b", fontSize: "0.7rem" }}>STRUCTURE (ATR mult)</div>
              to 20H {featureContext.structure?.distance_to_20_high} · to 50H {featureContext.structure?.distance_to_50_high}
            </div>
            <div style={{ fontSize: "0.78rem", color: "#b7c5dd" }}>
              <div style={{ color: "#64748b", fontSize: "0.7rem" }}>NORMALISATION</div>
              tick ${featureContext.normalisation?.tick_value}/{featureContext.normalisation?.tick_size} · ~${featureContext.normalisation?.atr_dollars_per_001_lot}/ATR per 0.01 lot
            </div>
          </div>
          {featureContext.normalisation?.position_size_warning && (
            <div style={{ fontSize: "0.78rem", color: "#fbbf24", padding: "0.5rem 0.7rem", borderRadius: 8, border: "1px solid rgba(251,191,36,0.25)", background: "rgba(251,191,36,0.06)" }}>
              ⚠ {featureContext.normalisation?.warning_text}
              {featureContext.normalisation?.position_size_warning_reasons?.length ? (
                <div style={{ color: "#94a3b8", fontSize: "0.72rem", marginTop: "0.2rem" }}>
                  {featureContext.normalisation.position_size_warning_reasons.join(" · ")}
                </div>
              ) : null}
            </div>
          )}

          {/* ── Economic Context (B16.5) ── */}
          <div style={{ marginTop: "0.85rem", paddingTop: "0.75rem", borderTop: "1px solid rgba(255,255,255,0.05)" }}>
            <div style={{ fontSize: "0.72rem", color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.04em", fontWeight: 600, marginBottom: "0.5rem" }}>Economic Context</div>
            {featureContext.news && featureContext.news.impact !== "NONE" ? (
              <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" }}>
                <Badge color={featureContext.news.impact === "HIGH" ? "red" : featureContext.news.impact === "MEDIUM" ? "yellow" : "gray"}>
                  {featureContext.news.impact} impact
                </Badge>
                <span style={{ fontSize: "0.85rem", color: "#e9f4ff", fontWeight: 500 }}>
                  {featureContext.news.event_name || featureContext.news.event_type} ({featureContext.news.currency})
                </span>
                <Badge color={featureContext.news.event_relevance === "HIGH" ? "green" : featureContext.news.event_relevance === "MEDIUM" ? "blue" : "gray"}>
                  relevance: {featureContext.news.event_relevance}
                </Badge>
                <span style={{ fontSize: "0.78rem", color: "#94a3b8" }}>
                  {featureContext.news.is_upcoming
                    ? `in ${featureContext.news.minutes_to_event} min`
                    : `${featureContext.news.minutes_since_event} min ago`}
                </span>
              </div>
            ) : (
              <div style={{ fontSize: "0.8rem", color: "#64748b" }}>No significant economic events nearby.</div>
            )}
          </div>
        </div>
      )}

      {/* ── Regime Analytics ── */}
      {regime && (
        <div style={glass}>
          <div style={secHeader}>Market Regime</div>
          <div style={{ display: "flex", gap: "1.5rem", alignItems: "center", marginBottom: "1rem", flexWrap: "wrap" }}>
            <div>
              <span style={{ fontSize: "0.78rem", color: "#94a3b8" }}>Current: </span>
              <Badge color={regime.current_regime === "BULL" ? "green" : regime.current_regime === "BEAR" ? "red" : "gray"}>
                {regime.current_regime}
              </Badge>
            </div>
            {["BULL", "SIDEWAYS", "BEAR"].map((r) => (
              <div key={r} style={{ fontSize: "0.82rem", color: "#b7c5dd" }}>
                {r}: <span style={{ color: "#e5f4ff", fontWeight: 600 }}>{regime.regime_pct[r]}%</span>
                <span style={{ color: "#64748b", fontSize: "0.72rem" }}> (persist: {pct((regime.persistence[r] || 0) * 100)})</span>
              </div>
            ))}
          </div>
          <div style={secHeader}>Transition Matrix</div>
          <table style={{ width: "auto", borderCollapse: "collapse" }}>
            <thead><tr><th style={th}>From \ To</th>{["BULL", "SIDEWAYS", "BEAR"].map((r) => <th key={r} style={th}>{r}</th>)}</tr></thead>
            <tbody>
              {["BULL", "SIDEWAYS", "BEAR"].map((src) => (
                <tr key={src}>
                  <td style={{ ...td, fontWeight: 600 }}>{src}</td>
                  {["BULL", "SIDEWAYS", "BEAR"].map((dst) => {
                    const p = (regime.transition_matrix.probabilities[src]?.[dst] || 0) * 100;
                    return <td key={dst} style={{ ...numTd, color: p > 50 ? "#86efac" : p > 20 ? "#e5f4ff" : "#64748b" }}>{pct(p)}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Regime Filter Comparison ── */}
      {comparison && (
        <div style={glass}>
          <div style={secHeader}>Regime Filter Comparison</div>
          <div style={{ marginBottom: "0.5rem" }}>
            <Badge color={comparison.filter_verdict === "improved" ? "green" : comparison.filter_verdict === "worsened" ? "red" : "gray"}>
              {comparison.filter_verdict.toUpperCase()}
            </Badge>
            <span style={{ fontSize: "0.82rem", color: "#b7c5dd", marginLeft: "0.5rem" }}>
              {comparison.improvement_pct > 0 ? "+" : ""}{comparison.improvement_pct.toFixed(1)}% improvement
            </span>
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr><th style={th}>Metric</th><th style={th}>Baseline</th><th style={th}>Filtered</th><th style={th}>Diff</th></tr>
            </thead>
            <tbody>
              <tr><td style={td}>Trades</td><td style={numTd}>{comparison.baseline_trades}</td><td style={numTd}>{comparison.filtered_trades}</td><td style={numTd}>-{comparison.skipped_trades}</td></tr>
              <tr><td style={td}>Net Profit</td><td style={numTd}>{dollar(comparison.baseline_net_profit)}</td><td style={{ ...numTd, color: comparison.filtered_net_profit >= 0 ? "#86efac" : "#fca5a5" }}>{dollar(comparison.filtered_net_profit)}</td><td style={{ ...numTd, color: comparison.net_profit_diff >= 0 ? "#86efac" : "#fca5a5" }}>{dollar(comparison.net_profit_diff)}</td></tr>
              <tr><td style={td}>Profit Factor</td><td style={numTd}>{comparison.baseline_profit_factor.toFixed(2)}</td><td style={numTd}>{comparison.filtered_profit_factor.toFixed(2)}</td><td style={{ ...numTd, color: comparison.profit_factor_diff >= 0 ? "#86efac" : "#fca5a5" }}>{comparison.profit_factor_diff >= 0 ? "+" : ""}{comparison.profit_factor_diff.toFixed(2)}</td></tr>
              <tr><td style={td}>Win Rate</td><td style={numTd}>{pct(comparison.baseline_win_rate)}</td><td style={numTd}>{pct(comparison.filtered_win_rate)}</td><td style={numTd}>{comparison.filtered_win_rate >= comparison.baseline_win_rate ? "+" : ""}{(comparison.filtered_win_rate - comparison.baseline_win_rate).toFixed(1)}%</td></tr>
              <tr><td style={td}>Max Drawdown</td><td style={numTd}>{pct(comparison.baseline_max_drawdown)}</td><td style={numTd}>{pct(comparison.filtered_max_drawdown)}</td><td style={{ ...numTd, color: comparison.filtered_max_drawdown <= comparison.baseline_max_drawdown ? "#86efac" : "#fca5a5" }}>{(comparison.filtered_max_drawdown - comparison.baseline_max_drawdown).toFixed(2)}%</td></tr>
            </tbody>
          </table>
        </div>
      )}

      {/* ── Optimisation Results ── */}
      {optResults.length > 0 && (
        <div style={glass}>
          <div style={secHeader}>Parameter Optimisation</div>
          {optWarnings.map((w, i) => (
            <div key={i} style={{ fontSize: "0.78rem", color: "#fbbf24", marginBottom: "0.5rem", padding: "0.4rem 0.6rem", background: "rgba(251,191,36,0.06)", borderRadius: 6, border: "1px solid rgba(251,191,36,0.15)" }}>
              {w}
            </div>
          ))}
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={th}>#</th><th style={th}>Score</th><th style={th}>PF</th>
                <th style={th}>Net</th><th style={th}>Win%</th><th style={th}>DD</th>
                <th style={th}>Trades</th><th style={th}>Parameters</th>
              </tr>
            </thead>
            <tbody>
              {optResults.map((r) => (
                <tr key={r.rank}>
                  <td style={td}>{r.rank}</td>
                  <td style={numTd}>{r.score.toFixed(2)}</td>
                  <td style={numTd}>{r.profit_factor.toFixed(2)}</td>
                  <td style={{ ...numTd, color: r.net_profit >= 0 ? "#86efac" : "#fca5a5" }}>{dollar(r.net_profit)}</td>
                  <td style={numTd}>{pct(r.win_rate)}</td>
                  <td style={numTd}>{pct(r.max_drawdown)}</td>
                  <td style={numTd}>{r.trade_count}</td>
                  <td style={{ ...td, fontSize: "0.72rem", color: "#94a3b8", fontFamily: "monospace" }}>
                    {Object.entries(r.params).map(([k, v]) => `${k}=${v}`).join(", ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Walk-Forward Validation ── */}
      {wfResults.length > 0 && (
        <div style={glass}>
          <div style={secHeader}>Walk-Forward Validation (70% train / 30% test)</div>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={th}>#</th><th style={th}>Train Score</th><th style={th}>Val Score</th>
                <th style={th}>Degradation</th><th style={th}>Robust</th><th style={th}>Parameters</th>
              </tr>
            </thead>
            <tbody>
              {wfResults.map((r) => (
                <tr key={r.rank}>
                  <td style={td}>{r.rank}</td>
                  <td style={numTd}>{r.train_score.toFixed(2)}</td>
                  <td style={numTd}>{r.validation_score.toFixed(2)}</td>
                  <td style={{ ...numTd, color: r.degradation_pct > 50 ? "#fca5a5" : r.degradation_pct < 0 ? "#86efac" : "#e5f4ff" }}>
                    {r.degradation_pct.toFixed(1)}%
                  </td>
                  <td style={td}>
                    <Badge color={r.robust ? "green" : "red"}>{r.robust ? "YES" : "NO"}</Badge>
                  </td>
                  <td style={{ ...td, fontSize: "0.72rem", color: "#94a3b8", fontFamily: "monospace" }}>
                    {Object.entries(r.params).map(([k, v]) => `${k}=${v}`).join(", ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Research Recommendations ── */}
      {recommendations.length > 0 && (
        <div style={glass}>
          <div style={secHeader}>
            Research Recommendations
            <span style={{ fontSize: "0.7rem", color: "#64748b", fontWeight: 400, marginLeft: "0.5rem", textTransform: "none" }}>
              {recSummary.total_recommendations || 0} insights
            </span>
          </div>

          {/* High priority */}
          {recommendations.filter((r) => r.priority === "high").length > 0 && (
            <div style={{ marginBottom: "0.75rem" }}>
              <div style={{ fontSize: "0.72rem", color: "#f87171", fontWeight: 600, marginBottom: "0.4rem", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                High Priority
              </div>
              {recommendations.filter((r) => r.priority === "high").map((r, i) => (
                <div key={`h-${i}`} style={{ padding: "0.6rem 0.75rem", marginBottom: "0.35rem", borderRadius: 8, border: "1px solid rgba(248,113,113,0.15)", background: "rgba(248,113,113,0.04)" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.25rem" }}>
                    <Badge color="red">{r.category}</Badge>
                    <span style={{ fontSize: "0.85rem", color: "#e9f4ff", fontWeight: 500 }}>{r.title}</span>
                  </div>
                  {r.evidence.slice(0, 2).map((e, j) => (
                    <div key={j} style={{ fontSize: "0.75rem", color: "#94a3b8", marginLeft: "0.5rem" }}>{e}</div>
                  ))}
                  {r.suggested_next_action && (
                    <div style={{ fontSize: "0.75rem", color: "#4ab3ff", marginTop: "0.2rem" }}>→ {r.suggested_next_action}</div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Medium priority */}
          {recommendations.filter((r) => r.priority === "medium").length > 0 && (
            <div style={{ marginBottom: "0.75rem" }}>
              <div style={{ fontSize: "0.72rem", color: "#fbbf24", fontWeight: 600, marginBottom: "0.4rem", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                Medium Priority
              </div>
              {recommendations.filter((r) => r.priority === "medium").map((r, i) => (
                <div key={`m-${i}`} style={{ padding: "0.5rem 0.75rem", marginBottom: "0.3rem", borderRadius: 8, border: "1px solid rgba(251,191,36,0.1)", background: "rgba(251,191,36,0.02)" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.15rem" }}>
                    <Badge color="yellow">{r.category}</Badge>
                    <span style={{ fontSize: "0.82rem", color: "#e9f4ff" }}>{r.title}</span>
                  </div>
                  {r.suggested_next_action && (
                    <div style={{ fontSize: "0.72rem", color: "#94a3b8" }}>→ {r.suggested_next_action}</div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Low priority */}
          {recommendations.filter((r) => r.priority === "low").length > 0 && (
            <div>
              <div style={{ fontSize: "0.72rem", color: "#64748b", fontWeight: 600, marginBottom: "0.4rem", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                Low Priority
              </div>
              {recommendations.filter((r) => r.priority === "low").map((r, i) => (
                <div key={`l-${i}`} style={{ padding: "0.4rem 0.75rem", marginBottom: "0.25rem", borderRadius: 6, background: "rgba(255,255,255,0.01)" }}>
                  <span style={{ fontSize: "0.78rem", color: "#8fa0b7" }}>{r.title}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Knowledge Base ── */}
      {(kbStrongest.length > 0 || kbMostTested.length > 0) && (
        <div style={glass}>
          <div style={{ ...secHeader, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span>Research Knowledge Base</span>
            <span style={{ fontSize: "0.68rem", color: "#64748b", fontWeight: 400, textTransform: "none" }}>
              {kbTotalObs} observations · {kbTotalCombos} combinations
            </span>
          </div>

          {/* Strongest Combinations */}
          {kbStrongest.length > 0 && (
            <div style={{ marginBottom: "1rem" }}>
              <div style={{ fontSize: "0.72rem", color: "#86efac", fontWeight: 600, marginBottom: "0.4rem", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                Strongest Combinations
              </div>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    <th style={th}>Template</th><th style={th}>Timeframe</th>
                    <th style={th}>Runs</th><th style={th}>Avg Score</th><th style={th}>Avg PF</th>
                    <th style={th}>Avg DD</th><th style={th}>Confidence</th><th style={th}>Robustness</th>
                  </tr>
                </thead>
                <tbody>
                  {kbStrongest.map((e, i) => (
                    <tr key={`s-${i}`}>
                      <td style={{ ...td, fontSize: "0.78rem" }}>{e.template}</td>
                      <td style={td}>{e.timeframe}</td>
                      <td style={numTd}>{e.run_count}</td>
                      <td style={{ ...numTd, color: e.avg_score >= 60 ? "#86efac" : e.avg_score >= 40 ? "#fbbf24" : "#fca5a5" }}>
                        {e.avg_score}
                      </td>
                      <td style={numTd}>{e.avg_pf.toFixed(2)}</td>
                      <td style={numTd}>{pct(e.avg_drawdown)}</td>
                      <td style={td}>
                        <Badge color={e.confidence === "high" ? "green" : e.confidence === "medium" ? "yellow" : "gray"}>
                          {e.confidence.toUpperCase()} ({e.confidence_score})
                        </Badge>
                      </td>
                      <td style={numTd}>{pct(e.robustness_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Highest Confidence */}
          {kbHighConf.length > 0 && (
            <div style={{ marginBottom: "1rem" }}>
              <div style={{ fontSize: "0.72rem", color: "#4ab3ff", fontWeight: 600, marginBottom: "0.4rem", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                Highest Confidence
              </div>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    <th style={th}>Template</th><th style={th}>TF</th>
                    <th style={th}>Runs</th><th style={th}>Score ±σ</th>
                    <th style={th}>Confidence</th><th style={th}>WF Runs</th><th style={th}>WF Robust</th>
                  </tr>
                </thead>
                <tbody>
                  {kbHighConf.map((e, i) => (
                    <tr key={`c-${i}`}>
                      <td style={{ ...td, fontSize: "0.78rem" }}>{e.template}</td>
                      <td style={td}>{e.timeframe}</td>
                      <td style={numTd}>{e.run_count}</td>
                      <td style={numTd}>
                        {e.avg_score} <span style={{ color: "#64748b", fontSize: "0.7rem" }}>±{e.score_stddev}</span>
                      </td>
                      <td style={td}>
                        <Badge color={e.confidence === "high" ? "green" : e.confidence === "medium" ? "yellow" : "gray"}>
                          {e.confidence_score}
                        </Badge>
                      </td>
                      <td style={numTd}>{e.wf_run_count}</td>
                      <td style={numTd}>{e.wf_run_count > 0 ? pct(e.wf_robust_pct) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Most Tested */}
          {kbMostTested.length > 0 && (
            <div>
              <div style={{ fontSize: "0.72rem", color: "#94a3b8", fontWeight: 600, marginBottom: "0.4rem", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                Most Tested
              </div>
              <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                {kbMostTested.map((e, i) => (
                  <div key={`mt-${i}`} style={{
                    padding: "0.5rem 0.75rem", borderRadius: 8,
                    border: "1px solid rgba(74,179,255,0.08)", background: "rgba(74,179,255,0.02)",
                    fontSize: "0.78rem", color: "#b7c5dd",
                  }}>
                    <div style={{ fontWeight: 600, color: "#e5f4ff", marginBottom: "0.15rem" }}>
                      {e.template} · {e.timeframe}
                    </div>
                    <div style={{ display: "flex", gap: "0.75rem", fontSize: "0.72rem" }}>
                      <span>{e.run_count} runs</span>
                      <span>Score: {e.avg_score}</span>
                      <span>Best: {e.best_score}</span>
                      <span>Worst: {e.worst_score}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div style={{ fontSize: "0.68rem", color: "#475569", marginTop: "0.75rem" }}>
            Knowledge Base accumulates observations from Strategy Lab, Research Matrix, and Optimisation runs.
            Confidence reflects consistency, sample size, and walk-forward robustness — not predicted future performance.
          </div>
        </div>
      )}

      {/* ── Disclaimer ── */}
      <div style={{ fontSize: "0.72rem", color: "#475569", padding: "1rem 0", borderTop: "1px solid rgba(255,255,255,0.04)", marginTop: "1rem" }}>
        Research Mode — All results are simulated using MT5 OHLC data and GuvFX execution assumptions.
        They may differ from MT5 Strategy Tester or live execution. Optimised parameters are fitted to
        historical data and may not perform similarly in live trading. Not financial advice. No automatic deployment.
      </div>
    </div>
  );
}
