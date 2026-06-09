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
  const [optResults, setOptResults] = useState<OptResult[]>([]);
  const [wfResults, setWfResults] = useState<WFResult[]>([]);
  const [optWarnings, setOptWarnings] = useState<string[]>([]);

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
    setRegime(null); setRegimePerf(null); setComparison(null);
    setOptResults([]); setWfResults([]); setOptWarnings([]);

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
        regime_skip_analysis: Record<string, unknown>;
      }>("/api/backtests/regime-filter/", {
        method: "POST",
        body: JSON.stringify({
          template_name: selectedTemplate, symbol, timeframe, bar_count: 1000,
          regime_filter: { allowed_entry_regimes: allowed },
        }),
      });
      if (filterRes.ok) setComparison(filterRes.comparison);

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

      {/* ── Disclaimer ── */}
      <div style={{ fontSize: "0.72rem", color: "#475569", padding: "1rem 0", borderTop: "1px solid rgba(255,255,255,0.04)", marginTop: "1rem" }}>
        Research Mode — All results are simulated using MT5 OHLC data and GuvFX execution assumptions.
        They may differ from MT5 Strategy Tester or live execution. Optimised parameters are fitted to
        historical data and may not perform similarly in live trading. Not financial advice. No automatic deployment.
      </div>
    </div>
  );
}
