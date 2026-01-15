"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { AppShell } from "@/components/AppShell";

const FOREX_SYMBOLS = [
  "EURUSD",
  "GBPUSD",
  "USDJPY",
  "AUDUSD",
  "USDCAD",
  "USDCHF",
  "NZDUSD",
];

export default function CreateStrategyPage() {
  const router = useRouter();

  const [accessToken, setAccessToken] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // 1. Overview
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  // 2. Market & Timeframe
  const [style, setStyle] = useState("");
  const [marketType, setMarketType] = useState("FOREX");
  const [timeframe, setTimeframe] = useState("");
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>(["EURUSD"]);

  // 3. Edge
  const [edgeType, setEdgeType] = useState("");
  const [edgeRationale, setEdgeRationale] = useState("");

  // 4. Setup & Indicators (simplified: primary indicator + MA fields)
  const [indicatorType, setIndicatorType] = useState<"MA" | "RSI" | "NONE">(
    "MA"
  );
  const [maFast, setMaFast] = useState("20");
  const [maSlow, setMaSlow] = useState("50");
  const [maType, setMaType] = useState("EMA");
  const [autoAi, setAutoAi] = useState(true);

  // 5. Stop Loss
  const [slMethod, setSlMethod] = useState<"SWING_HIGH_LOW" | "FIXED_PIPS" | "ATR_MULTIPLE">(
    "ATR_MULTIPLE"
  );
  const [slFixedPips, setSlFixedPips] = useState("20");
  const [slSwingBuffer, setSlSwingBuffer] = useState("3");
  const [slAtrPeriod, setSlAtrPeriod] = useState("14");
  const [slAtrMultiple, setSlAtrMultiple] = useState("1.5");

  // 6. Take Profit
  const [tpPrimary, setTpPrimary] = useState<
    "FIXED_RR" | "LEVEL_BASED" | "TRAILING"
  >("FIXED_RR");
  const [tpRrTarget, setTpRrTarget] = useState("2.0");
  const [tpUseTrailing, setTpUseTrailing] = useState(true);
  const [tpTrailMethod, setTpTrailMethod] = useState<"ATR_TRAIL" | "SWING_TRAIL">(
    "ATR_TRAIL"
  );
  const [tpTrailAtrPeriod, setTpTrailAtrPeriod] = useState("14");
  const [tpTrailAtrMultiple, setTpTrailAtrMultiple] = useState("1.5");

  // 7. Position sizing
  const [riskPerTradePct, setRiskPerTradePct] = useState("1.0");

  // 8. Trade Management
  const [breakevenEnabled, setBreakevenEnabled] = useState(true);
  const [breakevenAtR, setBreakevenAtR] = useState("1.0");
  const [pyramidingEnabled, setPyramidingEnabled] = useState(false);
  const [pyramidingMaxAdditions, setPyramidingMaxAdditions] = useState("0");

  // 9. Filters & Conditions (News & time filters)
  const [newsMode, setNewsMode] = useState<"AVOID_NEWS" | "NEWS_ONLY" | "NEWS_BIASED">(
    "AVOID_NEWS"
  );
  const [newsPreMinutes, setNewsPreMinutes] = useState("30");
  const [newsPostMinutes, setNewsPostMinutes] = useState("30");
  const [maxTradesPerDay, setMaxTradesPerDay] = useState("5");

  // 10. Risk & Money Management (overall)
  const [dailyMaxLossR, setDailyMaxLossR] = useState("3.0");
  const [weeklyMaxLossR, setWeeklyMaxLossR] = useState("8.0");
  const [maxOpenRiskPct, setMaxOpenRiskPct] = useState("5.0");

  // 11–12. Plan & Psychology
  const [preSessionChecklist, setPreSessionChecklist] = useState(
    "Check economic calendar\nMark key levels\nDefine directional bias"
  );
  const [postSessionChecklist, setPostSessionChecklist] = useState(
    "Review trades\nCapture screenshots\nUpdate journal"
  );
  const [psychAfterBigWinR, setPsychAfterBigWinR] = useState("3.0");
  const [psychCooldownMinutes, setPsychCooldownMinutes] = useState("30");
  const [psychMaxConsecLosses, setPsychMaxConsecLosses] = useState("3");
  const [psychReducedRiskPct, setPsychReducedRiskPct] = useState("0.5");

  // 13. Backtesting & Metrics (informational only here)
  // no direct config; just guidance/link later

  // Load token
  useEffect(() => {
    if (typeof window !== "undefined") {
      const stored = window.localStorage.getItem("guvfx_access_token");
      if (stored) {
        setAccessToken(stored);
      }
    }
  }, []);

  const getTimeframeOptions = () => {
    switch (style) {
      case "SCALPER":
        return ["M1", "M3", "M5"];
      case "INTRADAY":
        return ["M15", "M30", "H1"];
      case "SWING":
        return ["H4", "D1"];
      case "POSITION":
        return ["D1", "W1"];
      default:
        return [];
    }
  };

  const toggleSymbol = (symbol: string) => {
    setSelectedSymbols((prev) =>
      prev.includes(symbol)
        ? prev.filter((s) => s !== symbol)
        : [...prev, symbol]
    );
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setInfo(null);

    if (!accessToken) {
      setError("");
      return;
    }
    if (!name.trim()) {
      setError("Please provide a strategy name.");
      return;
    }

    const symbol_universe = selectedSymbols.join(",");
    const useMA = indicatorType === "MA";

    const sl_rules = {
      method: slMethod,
      atr_period: slMethod === "ATR_MULTIPLE" ? Number(slAtrPeriod) : null,
      atr_multiple: slMethod === "ATR_MULTIPLE" ? Number(slAtrMultiple) : null,
      fixed_pips: slMethod === "FIXED_PIPS" ? Number(slFixedPips) : null,
      swing_buffer_pips:
        slMethod === "SWING_HIGH_LOW" ? Number(slSwingBuffer) : null,
    };

    const tp_rules = {
      primary: tpPrimary,
      rr_target: tpPrimary === "FIXED_RR" ? Number(tpRrTarget) : null,
      use_trailing: tpUseTrailing,
      trailing: tpUseTrailing
        ? {
            method: tpTrailMethod,
            atr_period: tpTrailMethod === "ATR_TRAIL" ? Number(tpTrailAtrPeriod) : null,
            atr_multiple:
              tpTrailMethod === "ATR_TRAIL" ? Number(tpTrailAtrMultiple) : null,
          }
        : null,
    };

    const trade_management = {
      move_to_breakeven: {
        enabled: breakevenEnabled,
        at_r_multiple: Number(breakevenAtR),
      },
      pyramiding: {
        enabled: pyramidingEnabled,
        max_additions: Number(pyramidingMaxAdditions),
      },
    };

    const filters = {
      news_filter: {
        mode: newsMode,
        impact_levels: ["HIGH"], // simplified for now
        event_types: [], // can be extended later
        pre_event_minutes: Number(newsPreMinutes),
        post_event_minutes: Number(newsPostMinutes),
      },
      time_filters: {
        avoid_friday_close: true,
        avoid_rollover: true,
      },
      max_trades_per_day: Number(maxTradesPerDay),
    };

    const risk_limits = {
      daily_max_loss_r: Number(dailyMaxLossR),
      weekly_max_loss_r: Number(weeklyMaxLossR),
      max_open_risk_pct: Number(maxOpenRiskPct),
      correlation_groups: [],
    };

    const plan_meta = {
      pre_session_checklist: preSessionChecklist
        .split("\n")
        .map((l) => l.trim())
        .filter(Boolean),
      post_session_checklist: postSessionChecklist
        .split("\n")
        .map((l) => l.trim())
        .filter(Boolean),
      psychology_rules: {
        after_big_win_r: Number(psychAfterBigWinR),
        cooldown_minutes_after_big_win: Number(psychCooldownMinutes),
        max_consecutive_losses_before_reduce_size: Number(
          psychMaxConsecLosses
        ),
        reduced_risk_per_trade_pct: Number(psychReducedRiskPct),
      },
    };

    const body: Record<string, unknown> = {
      name: name.trim(),
      description: description.trim(),
      style: style || null,
      market_type: marketType,
      symbol_universe,
      timeframe: timeframe || "",
      edge_type: edgeType || null,
      edge_rationale: edgeRationale.trim(),
      ma_fast_period: useMA && maFast ? Number(maFast) : null,
      ma_slow_period: useMA && maSlow ? Number(maSlow) : null,
      ma_type: useMA ? maType : null,
      auto_optimize_by_ai: autoAi,
      risk_per_trade_pct: riskPerTradePct ? Number(riskPerTradePct) : null,
      sl_rules,
      tp_rules,
      trade_management,
      filters,
      risk_limits,
      plan_meta,
    };

    setCreating(true);
    try {
      await apiFetch(
        "/api/strategies/strategies/",
        {
          method: "POST",
          body: JSON.stringify(body),
        }
);

      setInfo("Strategy created successfully.");
      // Redirect to strategies list
      setTimeout(() => router.push("/strategies"), 600);
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error ? err.message : "Failed to create strategy.";
      setError(message);
    } finally {
      setCreating(false);
    }
  };

  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
          Create Strategy
        </h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          Build a complete trading strategy from edge to execution. You can
          refine details later on the strategy page.
        </p>

        {error && <Alert type="error">{error}</Alert>}
        {info && <Alert type="info">{info}</Alert>}

        <Card
          title="Strategy Builder"
          subtitle="Fill out each component of your trading plan."
        >
          {!accessToken && (
            <p style={{ fontStyle: "italic", fontSize: "0.9rem" }}>
              
            </p>
          )}

          <form onSubmit={handleSubmit}>
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "1.5rem",
              }}
            >
              {/* 1. Overview */}
              <section>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "baseline",
                    marginBottom: "0.4rem",
                  }}
                >
                  <h3
                    style={{
                      fontSize: "0.95rem",
                      color: "#e5f4ff",
                      margin: 0,
                    }}
                  >
                    1. Overview
                  </h3>
                  <span
                    style={{ fontSize: "0.75rem", color: "#9ca3af" }}
                  >
                    Name and short description
                  </span>
                </div>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "minmax(0, 1.2fr) minmax(0, 1.8fr)",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <label
                      htmlFor="name"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Strategy name
                    </label>
                    <input
                      id="name"
                      type="text"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="e.g. H4 Trend Follower"
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="description"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Description (optional)
                    </label>
                    <textarea
                      id="description"
                      rows={2}
                      value={description}
                      onChange={(e) => setDescription(e.target.value)}
                      placeholder="Short description of your strategy logic."
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                        resize: "vertical",
                      }}
                    />
                  </div>
                </div>
              </section>

              {/* 2. Market & Timeframe */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "baseline",
                    marginBottom: "0.4rem",
                  }}
                >
                  <h3
                    style={{
                      fontSize: "0.95rem",
                      color: "#e5f4ff",
                      margin: 0,
                    }}
                  >
                    2. Market & Timeframe
                  </h3>
                  <span
                    style={{ fontSize: "0.75rem", color: "#9ca3af" }}
                  >
                    What you trade and on which horizon
                  </span>
                </div>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns:
                      "repeat(auto-fit, minmax(220px, 1fr))",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <label
                      htmlFor="style"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Style
                    </label>
                    <select
                      id="style"
                      value={style}
                      onChange={(e) => {
                        setStyle(e.target.value);
                        setTimeframe(""); // reset to force user to choose valid TF
                      }}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    >
                      <option value="">Select style</option>
                      <option value="SCALPER">Scalper</option>
                      <option value="INTRADAY">Intraday</option>
                      <option value="SWING">Swing</option>
                      <option value="POSITION">Position</option>
                    </select>
                  </div>
                  <div>
                    <label
                      htmlFor="marketType"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Market type
                    </label>
                    <select
                      id="marketType"
                      value={marketType}
                      onChange={(e) => setMarketType(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    >
                      <option value="FOREX">Forex</option>
                    </select>
                  </div>
                  <div>
                    <label
                      htmlFor="timeframe"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Timeframe
                    </label>
                    <select
                      id="timeframe"
                      value={timeframe}
                      onChange={(e) => setTimeframe(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    >
                      <option value="">Select timeframe</option>
                      {getTimeframeOptions().map((tf) => (
                        <option key={tf} value={tf}>
                          {tf}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div style={{ gridColumn: "1 / -1" }}>
                    <label
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Symbols (Forex pairs)
                    </label>
                    <div
                      style={{
                        marginBottom: "0.35rem",
                        fontSize: "0.8rem",
                        color: "#9ca3af",
                      }}
                    >
                      Selected: {selectedSymbols.join(", ") || "None"}
                    </div>
                    <div
                      style={{
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        padding: "0.5rem 0.7rem",
                        display: "flex",
                        flexWrap: "wrap",
                        gap: "0.5rem 1.5rem",
                      }}
                    >
                      {FOREX_SYMBOLS.map((sym) => (
                        <label
                          key={sym}
                          style={{
                            display: "inline-flex",
                            alignItems: "center",
                            gap: 6,
                            fontSize: "0.85rem",
                            color: "#e5f4ff",
                            cursor: "pointer",
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={selectedSymbols.includes(sym)}
                            onChange={() => toggleSymbol(sym)}
                            style={{ cursor: "pointer" }}
                          />
                          {sym}
                        </label>
                      ))}
                    </div>
                  </div>
                </div>
              </section>

              {/* 3. Trade Idea (Edge) */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "baseline",
                    marginBottom: "0.4rem",
                  }}
                >
                  <h3
                    style={{
                      fontSize: "0.95rem",
                      color: "#e5f4ff",
                      margin: 0,
                    }}
                  >
                    3. Trade idea (edge)
                  </h3>
                  <span
                    style={{ fontSize: "0.75rem", color: "#9ca3af" }}
                  >
                    Why this should make money
                  </span>
                </div>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "minmax(0, 1.2fr) minmax(0, 1.8fr)",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <label
                      htmlFor="edgeType"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Edge type
                    </label>
                    <select
                      id="edgeType"
                      value={edgeType}
                      onChange={(e) => setEdgeType(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    >
                      <option value="">Select edge type</option>
                      <option value="TREND_FOLLOWING">Trend following</option>
                      <option value="MEAN_REVERSION">Mean reversion</option>
                      <option value="BREAKOUT">Breakout</option>
                      <option value="NEWS_FUNDAMENTAL">
                        News / fundamental
                      </option>
                    </select>
                  </div>
                  <div>
                    <label
                      htmlFor="edgeRationale"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Edge rationale
                    </label>
                    <textarea
                      id="edgeRationale"
                      rows={3}
                      value={edgeRationale}
                      onChange={(e) => setEdgeRationale(e.target.value)}
                      placeholder="In one or two sentences, why should this strategy work?"
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                        resize: "vertical",
                      }}
                    />
                  </div>
                </div>
              </section>

              {/* 4. Setup & Indicators */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "baseline",
                    marginBottom: "0.4rem",
                  }}
                >
                  <h3
                    style={{
                      fontSize: "0.95rem",
                      color: "#e5f4ff",
                      margin: 0,
                    }}
                  >
                    4. Setup & indicators
                  </h3>
                  <span
                    style={{ fontSize: "0.75rem", color: "#9ca3af" }}
                  >
                    What the chart should look like and which indicator you use
                  </span>
                </div>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns:
                      "repeat(auto-fit, minmax(220px, 1fr))",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <label
                      htmlFor="indicatorType"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Primary indicator
                    </label>
                    <select
                      id="indicatorType"
                      value={indicatorType}
                      onChange={(e) =>
                        setIndicatorType(
                          e.target.value as "MA" | "RSI" | "NONE"
                        )
                      }
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    >
                      <option value="MA">Moving Average (MA)</option>
                      <option value="RSI">RSI-based</option>
                      <option value="NONE">None / other</option>
                    </select>
                  </div>

                  {indicatorType === "MA" && (
                    <>
                      <div>
                        <label
                          htmlFor="maFast"
                          style={{
                            display: "block",
                            fontSize: "0.85rem",
                            color: "#cbd5f5",
                            marginBottom: "0.25rem",
                          }}
                        >
                          Fast MA period
                        </label>
                        <input
                          id="maFast"
                          type="number"
                          min={1}
                          value={maFast}
                          onChange={(e) => setMaFast(e.target.value)}
                          style={{
                            width: "100%",
                            padding: "0.6rem 0.8rem",
                            borderRadius: 8,
                            border: "1px solid rgba(148,163,184,0.65)",
                            background: "rgba(3,7,18,0.9)",
                            color: "#e5f4ff",
                            fontSize: "0.9rem",
                            outline: "none",
                            boxSizing: "border-box",
                          }}
                        />
                      </div>
                      <div>
                        <label
                          htmlFor="maSlow"
                          style={{
                            display: "block",
                            fontSize: "0.85rem",
                            color: "#cbd5f5",
                            marginBottom: "0.25rem",
                          }}
                        >
                          Slow MA period
                        </label>
                        <input
                          id="maSlow"
                          type="number"
                          min={1}
                          value={maSlow}
                          onChange={(e) => setMaSlow(e.target.value)}
                          style={{
                            width: "100%",
                            padding: "0.6rem 0.8rem",
                            borderRadius: 8,
                            border: "1px solid rgba(148,163,184,0.65)",
                            background: "rgba(3,7,18,0.9)",
                            color: "#e5f4ff",
                            fontSize: "0.9rem",
                            outline: "none",
                            boxSizing: "border-box",
                          }}
                        />
                      </div>
                      <div>
                        <label
                          htmlFor="maType"
                          style={{
                            display: "block",
                            fontSize: "0.85rem",
                            color: "#cbd5f5",
                            marginBottom: "0.25rem",
                          }}
                        >
                          MA type
                        </label>
                        <select
                          id="maType"
                          value={maType}
                          onChange={(e) => setMaType(e.target.value)}
                          style={{
                            width: "100%",
                            padding: "0.6rem 0.8rem",
                            borderRadius: 8,
                            border: "1px solid rgba(148,163,184,0.65)",
                            background: "rgba(3,7,18,0.9)",
                            color: "#e5f4ff",
                            fontSize: "0.9rem",
                            outline: "none",
                            boxSizing: "border-box",
                          }}
                        >
                          <option value="SMA">Simple MA (SMA)</option>
                          <option value="EMA">Exponential MA (EMA)</option>
                          <option value="WMA">Weighted MA (WMA)</option>
                        </select>
                      </div>
                    </>
                  )}

                  <div>
                    <label
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      AI optimization
                    </label>
                    <label
                      style={{
                        fontSize: "0.85rem",
                        color: "#e5f4ff",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 8,
                        cursor: "pointer",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={autoAi}
                        onChange={(e) => setAutoAi(e.target.checked)}
                        style={{ cursor: "pointer" }}
                      />
                      Let AI manage parameters automatically
                    </label>
                  </div>
                </div>
              </section>

              {/* 5. Stop Loss Rules */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  5. Stop loss rules
                </h3>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns:
                      "repeat(auto-fit, minmax(220px, 1fr))",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <label
                      htmlFor="slMethod"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Method
                    </label>
                    <select
                      id="slMethod"
                      value={slMethod}
                      onChange={(e) =>
                        setSlMethod(
                          e.target.value as
                            | "SWING_HIGH_LOW"
                            | "FIXED_PIPS"
                            | "ATR_MULTIPLE"
                        )
                      }
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    >
                      <option value="ATR_MULTIPLE">ATR multiple</option>
                      <option value="SWING_HIGH_LOW">Below/above swing point</option>
                      <option value="FIXED_PIPS">Fixed pips</option>
                    </select>
                  </div>
                  {slMethod === "FIXED_PIPS" && (
                    <div>
                      <label
                        htmlFor="slFixedPips"
                        style={{
                          display: "block",
                          fontSize: "0.85rem",
                          color: "#cbd5f5",
                          marginBottom: "0.25rem",
                        }}
                      >
                        Distance (pips)
                      </label>
                      <input
                        id="slFixedPips"
                        type="number"
                        min={1}
                        value={slFixedPips}
                        onChange={(e) => setSlFixedPips(e.target.value)}
                        style={{
                          width: "100%",
                          padding: "0.6rem 0.8rem",
                          borderRadius: 8,
                          border: "1px solid rgba(148,163,184,0.65)",
                          background: "rgba(3,7,18,0.9)",
                          color: "#e5f4ff",
                          fontSize: "0.9rem",
                          outline: "none",
                          boxSizing: "border-box",
                        }}
                      />
                    </div>
                  )}
                  {slMethod === "SWING_HIGH_LOW" && (
                    <div>
                      <label
                        htmlFor="slSwingBuffer"
                        style={{
                          display: "block",
                          fontSize: "0.85rem",
                          color: "#cbd5f5",
                          marginBottom: "0.25rem",
                        }}
                      >
                        Buffer (pips)
                      </label>
                      <input
                        id="slSwingBuffer"
                        type="number"
                        min={0}
                        value={slSwingBuffer}
                        onChange={(e) => setSlSwingBuffer(e.target.value)}
                        style={{
                          width: "100%",
                          padding: "0.6rem 0.8rem",
                          borderRadius: 8,
                          border: "1px solid rgba(148,163,184,0.65)",
                          background: "rgba(3,7,18,0.9)",
                          color: "#e5f4ff",
                          fontSize: "0.9rem",
                          outline: "none",
                          boxSizing: "border-box",
                        }}
                      />
                    </div>
                  )}
                  {slMethod === "ATR_MULTIPLE" && (
                    <>
                      <div>
                        <label
                          htmlFor="slAtrPeriod"
                          style={{
                            display: "block",
                            fontSize: "0.85rem",
                            color: "#cbd5f5",
                            marginBottom: "0.25rem",
                          }}
                        >
                          ATR period
                        </label>
                        <input
                          id="slAtrPeriod"
                          type="number"
                          min={1}
                          value={slAtrPeriod}
                          onChange={(e) => setSlAtrPeriod(e.target.value)}
                          style={{
                            width: "100%",
                            padding: "0.6rem 0.8rem",
                            borderRadius: 8,
                            border: "1px solid rgba(148,163,184,0.65)",
                            background: "rgba(3,7,18,0.9)",
                            color: "#e5f4ff",
                            fontSize: "0.9rem",
                            outline: "none",
                            boxSizing: "border-box",
                          }}
                        />
                      </div>
                      <div>
                        <label
                          htmlFor="slAtrMultiple"
                          style={{
                            display: "block",
                            fontSize: "0.85rem",
                            color: "#cbd5f5",
                            marginBottom: "0.25rem",
                          }}
                        >
                          ATR multiple
                        </label>
                        <input
                          id="slAtrMultiple"
                          type="number"
                          step={0.1}
                          min={0.1}
                          value={slAtrMultiple}
                          onChange={(e) => setSlAtrMultiple(e.target.value)}
                          style={{
                            width: "100%",
                            padding: "0.6rem 0.8rem",
                            borderRadius: 8,
                            border: "1px solid rgba(148,163,184,0.65)",
                            background: "rgba(3,7,18,0.9)",
                            color: "#e5f4ff",
                            fontSize: "0.9rem",
                            outline: "none",
                            boxSizing: "border-box",
                          }}
                        />
                      </div>
                    </>
                  )}
                </div>
              </section>

              {/* 6. Take Profit Rules */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  6. Take profit rules
                </h3>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns:
                      "repeat(auto-fit, minmax(220px, 1fr))",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <label
                      htmlFor="tpPrimary"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Primary method
                    </label>
                    <select
                      id="tpPrimary"
                      value={tpPrimary}
                      onChange={(e) =>
                        setTpPrimary(
                          e.target.value as "FIXED_RR" | "LEVEL_BASED" | "TRAILING"
                        )
                      }
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    >
                      <option value="FIXED_RR">Fixed R-multiple</option>
                      <option value="LEVEL_BASED">Level-based</option>
                      <option value="TRAILING">Trailing only</option>
                    </select>
                  </div>
                  {tpPrimary === "FIXED_RR" && (
                    <div>
                      <label
                        htmlFor="tpRrTarget"
                        style={{
                          display: "block",
                          fontSize: "0.85rem",
                          color: "#cbd5f5",
                          marginBottom: "0.25rem",
                        }}
                      >
                        Target R-multiple
                      </label>
                      <input
                        id="tpRrTarget"
                        type="number"
                        step={0.1}
                        min={0.1}
                        value={tpRrTarget}
                        onChange={(e) => setTpRrTarget(e.target.value)}
                        style={{
                          width: "100%",
                          padding: "0.6rem 0.8rem",
                          borderRadius: 8,
                          border: "1px solid rgba(148,163,184,0.65)",
                          background: "rgba(3,7,18,0.9)",
                          color: "#e5f4ff",
                          fontSize: "0.9rem",
                          outline: "none",
                          boxSizing: "border-box",
                        }}
                      />
                    </div>
                  )}
                  <div>
                    <label
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Trailing stop
                    </label>
                    <label
                      style={{
                        fontSize: "0.85rem",
                        color: "#e5f4ff",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 8,
                        cursor: "pointer",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={tpUseTrailing}
                        onChange={(e) => setTpUseTrailing(e.target.checked)}
                        style={{ cursor: "pointer" }}
                      />
                      Enable trailing logic
                    </label>
                  </div>
                  {tpUseTrailing && (
                    <>
                      <div>
                        <label
                          htmlFor="tpTrailMethod"
                          style={{
                            display: "block",
                            fontSize: "0.85rem",
                            color: "#cbd5f5",
                            marginBottom: "0.25rem",
                          }}
                        >
                          Trailing method
                        </label>
                        <select
                          id="tpTrailMethod"
                          value={tpTrailMethod}
                          onChange={(e) =>
                            setTpTrailMethod(
                              e.target.value as "ATR_TRAIL" | "SWING_TRAIL"
                            )
                          }
                          style={{
                            width: "100%",
                            padding: "0.6rem 0.8rem",
                            borderRadius: 8,
                            border: "1px solid rgba(148,163,184,0.65)",
                            background: "rgba(3,7,18,0.9)",
                            color: "#e5f4ff",
                            fontSize: "0.9rem",
                            outline: "none",
                            boxSizing: "border-box",
                          }}
                        >
                          <option value="ATR_TRAIL">ATR-based trailing</option>
                          <option value="SWING_TRAIL">
                            Swing high/low trailing
                          </option>
                        </select>
                      </div>
                      {tpTrailMethod === "ATR_TRAIL" && (
                        <>
                          <div>
                            <label
                              htmlFor="tpTrailAtrPeriod"
                              style={{
                                display: "block",
                                fontSize: "0.85rem",
                                color: "#cbd5f5",
                                marginBottom: "0.25rem",
                              }}
                            >
                              ATR period
                            </label>
                            <input
                              id="tpTrailAtrPeriod"
                              type="number"
                              min={1}
                              value={tpTrailAtrPeriod}
                              onChange={(e) =>
                                setTpTrailAtrPeriod(e.target.value)
                              }
                              style={{
                                width: "100%",
                                padding: "0.6rem 0.8rem",
                                borderRadius: 8,
                                border: "1px solid rgba(148,163,184,0.65)",
                                background: "rgba(3,7,18,0.9)",
                                color: "#e5f4ff",
                                fontSize: "0.9rem",
                                outline: "none",
                                boxSizing: "border-box",
                              }}
                            />
                          </div>
                          <div>
                            <label
                              htmlFor="tpTrailAtrMultiple"
                              style={{
                                display: "block",
                                fontSize: "0.85rem",
                                color: "#cbd5f5",
                                marginBottom: "0.25rem",
                              }}
                            >
                              ATR multiple
                            </label>
                            <input
                              id="tpTrailAtrMultiple"
                              type="number"
                              step={0.1}
                              min={0.1}
                              value={tpTrailAtrMultiple}
                              onChange={(e) =>
                                setTpTrailAtrMultiple(e.target.value)
                              }
                              style={{
                                width: "100%",
                                padding: "0.6rem 0.8rem",
                                borderRadius: 8,
                                border: "1px solid rgba(148,163,184,0.65)",
                                background: "rgba(3,7,18,0.9)",
                                color: "#e5f4ff",
                                fontSize: "0.9rem",
                                outline: "none",
                                boxSizing: "border-box",
                              }}
                            />
                          </div>
                        </>
                      )}
                    </>
                  )}
                </div>
              </section>

              {/* 7. Position Sizing */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  7. Position sizing
                </h3>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "minmax(0, 1fr)",
                    gap: "0.75rem",
                  }}
                >
                  <div>
                    <label
                      htmlFor="riskPerTradePct"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Risk per trade (% of account)
                    </label>
                    <input
                      id="riskPerTradePct"
                      type="number"
                      step={0.1}
                      min={0.1}
                      value={riskPerTradePct}
                      onChange={(e) => setRiskPerTradePct(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                </div>
              </section>

              {/* 8. Trade Management */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  8. Trade management
                </h3>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns:
                      "repeat(auto-fit, minmax(220px, 1fr))",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <label
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Move stop to breakeven
                    </label>
                    <label
                      style={{
                        fontSize: "0.85rem",
                        color: "#e5f4ff",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 8,
                        cursor: "pointer",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={breakevenEnabled}
                        onChange={(e) =>
                          setBreakevenEnabled(e.target.checked)
                        }
                        style={{ cursor: "pointer" }}
                      />
                      Enable breakeven logic
                    </label>
                    {breakevenEnabled && (
                      <div style={{ marginTop: "0.4rem" }}>
                        <label
                          htmlFor="breakevenAtR"
                          style={{
                            display: "block",
                            fontSize: "0.8rem",
                            color: "#9ca3af",
                            marginBottom: "0.25rem",
                          }}
                        >
                          At R-multiple
                        </label>
                        <input
                          id="breakevenAtR"
                          type="number"
                          step={0.1}
                          min={0.1}
                          value={breakevenAtR}
                          onChange={(e) => setBreakevenAtR(e.target.value)}
                          style={{
                            width: "100%",
                            padding: "0.5rem 0.7rem",
                            borderRadius: 8,
                            border: "1px solid rgba(148,163,184,0.65)",
                            background: "rgba(3,7,18,0.9)",
                            color: "#e5f4ff",
                            fontSize: "0.85rem",
                            outline: "none",
                            boxSizing: "border-box",
                          }}
                        />
                      </div>
                    )}
                  </div>
                  <div>
                    <label
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Pyramiding
                    </label>
                    <label
                      style={{
                        fontSize: "0.85rem",
                        color: "#e5f4ff",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 8,
                        cursor: "pointer",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={pyramidingEnabled}
                        onChange={(e) =>
                          setPyramidingEnabled(e.target.checked)
                        }
                        style={{ cursor: "pointer" }}
                      />
                      Allow adding to winners
                    </label>
                    {pyramidingEnabled && (
                      <div style={{ marginTop: "0.4rem" }}>
                        <label
                          htmlFor="pyramidingMaxAdditions"
                          style={{
                            display: "block",
                            fontSize: "0.8rem",
                            color: "#9ca3af",
                            marginBottom: "0.25rem",
                          }}
                        >
                          Maximum additional entries
                        </label>
                        <input
                          id="pyramidingMaxAdditions"
                          type="number"
                          min={0}
                          value={pyramidingMaxAdditions}
                          onChange={(e) =>
                            setPyramidingMaxAdditions(e.target.value)
                          }
                          style={{
                            width: "100%",
                            padding: "0.5rem 0.7rem",
                            borderRadius: 8,
                            border: "1px solid rgba(148,163,184,0.65)",
                            background: "rgba(3,7,18,0.9)",
                            color: "#e5f4ff",
                            fontSize: "0.85rem",
                            outline: "none",
                            boxSizing: "border-box",
                          }}
                        />
                      </div>
                    )}
                  </div>
                </div>
              </section>

              {/* 9. Filters & Conditions */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  9. Filters & conditions
                </h3>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns:
                      "repeat(auto-fit, minmax(220px, 1fr))",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <label
                      htmlFor="newsMode"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      News filter mode
                    </label>
                    <select
                      id="newsMode"
                      value={newsMode}
                      onChange={(e) =>
                        setNewsMode(
                          e.target.value as
                            | "AVOID_NEWS"
                            | "NEWS_ONLY"
                            | "NEWS_BIASED"
                        )
                      }
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    >
                      <option value="AVOID_NEWS">Avoid major news</option>
                      <option value="NEWS_ONLY">Trade only around news</option>
                      <option value="NEWS_BIASED">
                        Use news as directional bias
                      </option>
                    </select>
                  </div>
                  <div>
                    <label
                      htmlFor="newsPreMinutes"
                      style={{
                        display: "block",
                        fontSize: "0.8rem",
                        color: "#9ca3af",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Minutes before news to pause trading
                    </label>
                    <input
                      id="newsPreMinutes"
                      type="number"
                      min={0}
                      value={newsPreMinutes}
                      onChange={(e) => setNewsPreMinutes(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.5rem 0.7rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.85rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="newsPostMinutes"
                      style={{
                        display: "block",
                        fontSize: "0.8rem",
                        color: "#9ca3af",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Minutes after news to resume trading
                    </label>
                    <input
                      id="newsPostMinutes"
                      type="number"
                      min={0}
                      value={newsPostMinutes}
                      onChange={(e) => setNewsPostMinutes(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.5rem 0.7rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.85rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="maxTradesPerDay"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Max trades per day
                    </label>
                    <input
                      id="maxTradesPerDay"
                      type="number"
                      min={0}
                      value={maxTradesPerDay}
                      onChange={(e) => setMaxTradesPerDay(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                </div>
              </section>

              {/* 10. Risk & Money Management */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  10. Risk & money management (overall)
                </h3>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns:
                      "repeat(auto-fit, minmax(220px, 1fr))",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <label
                      htmlFor="dailyMaxLossR"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Daily max loss (R)
                    </label>
                    <input
                      id="dailyMaxLossR"
                      type="number"
                      step={0.1}
                      min={0}
                      value={dailyMaxLossR}
                      onChange={(e) => setDailyMaxLossR(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="weeklyMaxLossR"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Weekly max loss (R)
                    </label>
                    <input
                      id="weeklyMaxLossR"
                      type="number"
                      step={0.1}
                      min={0}
                      value={weeklyMaxLossR}
                      onChange={(e) => setWeeklyMaxLossR(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="maxOpenRiskPct"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Max open risk (% of equity)
                    </label>
                    <input
                      id="maxOpenRiskPct"
                      type="number"
                      step={0.1}
                      min={0}
                      value={maxOpenRiskPct}
                      onChange={(e) => setMaxOpenRiskPct(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                </div>
              </section>

              {/* 11–12. Trading Plan & Psychology */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  11–12. Trading plan & psychology
                </h3>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <label
                      htmlFor="preSession"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Pre-session checklist (one item per line)
                    </label>
                    <textarea
                      id="preSession"
                      rows={4}
                      value={preSessionChecklist}
                      onChange={(e) => setPreSessionChecklist(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                        resize: "vertical",
                      }}
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="postSession"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Post-session checklist (one item per line)
                    </label>
                    <textarea
                      id="postSession"
                      rows={4}
                      value={postSessionChecklist}
                      onChange={(e) => setPostSessionChecklist(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                        resize: "vertical",
                      }}
                    />
                  </div>
                </div>
                <div
                  style={{
                    marginTop: "0.75rem",
                    display: "grid",
                    gridTemplateColumns:
                      "repeat(auto-fit, minmax(220px, 1fr))",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <label
                      htmlFor="psychAfterBigWinR"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Big win threshold (R)
                    </label>
                    <input
                      id="psychAfterBigWinR"
                      type="number"
                      step={0.1}
                      min={0}
                      value={psychAfterBigWinR}
                      onChange={(e) => setPsychAfterBigWinR(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="psychCooldownMinutes"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Cooldown after big win (minutes)
                    </label>
                    <input
                      id="psychCooldownMinutes"
                      type="number"
                      min={0}
                      value={psychCooldownMinutes}
                      onChange={(e) =>
                        setPsychCooldownMinutes(e.target.value)
                      }
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="psychMaxConsecLosses"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Max consecutive losses before reducing size
                    </label>
                    <input
                      id="psychMaxConsecLosses"
                      type="number"
                      min={0}
                      value={psychMaxConsecLosses}
                      onChange={(e) =>
                        setPsychMaxConsecLosses(e.target.value)
                      }
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="psychReducedRiskPct"
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.25rem",
                      }}
                    >
                      Reduced risk per trade (%)
                    </label>
                    <input
                      id="psychReducedRiskPct"
                      type="number"
                      step={0.1}
                      min={0}
                      value={psychReducedRiskPct}
                      onChange={(e) =>
                        setPsychReducedRiskPct(e.target.value)
                      }
                      style={{
                        width: "100%",
                        padding: "0.6rem 0.8rem",
                        borderRadius: 8,
                        border: "1px solid rgba(148,163,184,0.65)",
                        background: "rgba(3,7,18,0.9)",
                        color: "#e5f4ff",
                        fontSize: "0.9rem",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                </div>
              </section>

              {/* 13. Backtesting & Metrics (informational) */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  13. Backtesting & metrics
                </h3>
                <p
                  style={{
                    fontSize: "0.8rem",
                    color: "#9ca3af",
                    margin: 0,
                  }}
                >
                  After saving this strategy, run backtests from the Backtests
                  section to measure win rate, average R, drawdown, and other
                  performance metrics. Use those numbers before scaling risk.
                </p>
              </section>
            </div>

            <div
              style={{
                marginTop: "1.25rem",
                display: "flex",
                justifyContent: "flex-end",
              }}
            >
              <Button type="submit" disabled={creating || !accessToken}>
                {creating ? "Creating…" : "Create strategy"}
              </Button>
            </div>
          </form>
        </Card>
      </div>
    </AppShell>
  );
}
