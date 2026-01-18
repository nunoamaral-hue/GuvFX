"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { AppShell } from "@/components/AppShell";

const FOREX_SYMBOLS = [
  // Majors
  "EURUSD",
  "GBPUSD",
  "USDJPY",
  "USDCHF",
  "USDCAD",
  "AUDUSD",
  "NZDUSD",
  // Crosses
  "EURGBP",
  "EURJPY",
  "EURCHF",
  "EURAUD",
  "EURNZD",
  "EURCAD",
  "GBPJPY",
  "GBPCHF",
  "GBPAUD",
  "GBPNZD",
  "GBPCAD",
  "AUDJPY",
  "AUDCHF",
  "AUDCAD",
  "AUDNZD",
  "NZDJPY",
  "NZDCHF",
  "NZDCAD",
  "CADJPY",
  "CADCHF",
  "CHFJPY",
  // Popular exotics (optional)
  "USDSEK",
  "USDNOK",
  "USDMXN",
  "USDZAR",
];

type ArchetypeId =
  | "TREND_EMA_CROSSOVER"
  | "TREND_PULLBACK_EMA_RSI"
  | "BOLLINGER_MEAN_REVERSION"
  | "DONCHIAN_BREAKOUT"
  | "LONDON_BOX_BREAKOUT"
  | "SWING_BOS_RETEST"
  | "MOMENTUM_RSI_EMA";

type ArchetypeCategory = "Trend" | "Reversion" | "Breakout" | "Structure";

type ArchetypeTemplate = {
  id: ArchetypeId;
  label: string;
  category: ArchetypeCategory;
  accent: "blue" | "green" | "purple" | "yellow";
  description: string;
  recommended?: boolean;
  defaults: {
    style: "SCALPER" | "INTRADAY" | "SWING" | "POSITION";
    timeframe: string;
    marketType: "FOREX";
    edgeType: string;
    indicatorType: "MA" | "RSI" | "NONE";
    maFast?: string;
    maSlow?: string;
    maType?: string;
    slMethod: "SWING_HIGH_LOW" | "FIXED_PIPS" | "ATR_MULTIPLE";
    slAtrPeriod?: string;
    slAtrMultiple?: string;
    slFixedPips?: string;
    slSwingBuffer?: string;
    tpPrimary: "FIXED_RR" | "LEVEL_BASED" | "TRAILING";
    tpRrTarget?: string;
    tpUseTrailing?: boolean;
    tpTrailMethod?: "ATR_TRAIL" | "SWING_TRAIL";
    tpTrailAtrPeriod?: string;
    tpTrailAtrMultiple?: string;
    riskPerTradePct: string;
    maxOpenRiskPct: string;
    dailyMaxLossR: string;
    weeklyMaxLossR: string;
    breakevenEnabled: boolean;
    breakevenAtR: string;
    newsMode: "AVOID_NEWS" | "NEWS_ONLY" | "NEWS_BIASED";
    newsPreMinutes: string;
    newsPostMinutes: string;
    maxTradesPerDay: string;
    preSessionChecklist: string;
    postSessionChecklist: string;
    psychAfterBigWinR: string;
    psychCooldownMinutes: string;
    psychMaxConsecLosses: string;
    psychReducedRiskPct: string;
  };
  indicators: { label: string; detail: string }[];
  patterns: { label: string; detail: string }[];
};

const ARCHETYPES: ArchetypeTemplate[] = [
  {
    id: "TREND_EMA_CROSSOVER",
    label: "Trend EMA Crossover (HTF filter)",
    category: "Trend",
    accent: "blue",
    recommended: true,
    description: "Robust trend follower using EMA crossover with higher-timeframe confirmation.",
    defaults: {
      style: "INTRADAY",
      timeframe: "H1",
      marketType: "FOREX",
      edgeType: "TREND_FOLLOWING",
      indicatorType: "MA",
      maFast: "20",
      maSlow: "50",
      maType: "EMA",
      slMethod: "ATR_MULTIPLE",
      slAtrPeriod: "14",
      slAtrMultiple: "1.5",
      tpPrimary: "FIXED_RR",
      tpRrTarget: "2.0",
      tpUseTrailing: true,
      tpTrailMethod: "ATR_TRAIL",
      tpTrailAtrPeriod: "14",
      tpTrailAtrMultiple: "1.5",
      riskPerTradePct: "0.5",
      maxOpenRiskPct: "3.0",
      dailyMaxLossR: "3.0",
      weeklyMaxLossR: "7.0",
      breakevenEnabled: true,
      breakevenAtR: "1.0",
      newsMode: "AVOID_NEWS",
      newsPreMinutes: "30",
      newsPostMinutes: "30",
      maxTradesPerDay: "5",
      preSessionChecklist: "Market conditions match strategy\nNo high-impact news soon\nRisk within limits\nSpread acceptable",
      postSessionChecklist: "Review trades\nCapture screenshots\nUpdate journal",
      psychAfterBigWinR: "3.0",
      psychCooldownMinutes: "30",
      psychMaxConsecLosses: "3",
      psychReducedRiskPct: "0.5",
    },
    indicators: [
      { label: "EMA 20", detail: "Fast EMA" },
      { label: "EMA 50", detail: "Slow EMA" },
      { label: "ATR 14", detail: "Volatility sizing" },
      { label: "HTF EMA", detail: "H4 slope filter" },
    ],
    patterns: [],
  },
  {
    id: "TREND_PULLBACK_EMA_RSI",
    label: "Trend Pullback (EMA + RSI)",
    category: "Trend",
    accent: "blue",
    description: "Buy dips / sell rallies inside a trend. EMA filter + RSI pullback trigger.",
    defaults: {
      style: "INTRADAY",
      timeframe: "H1",
      marketType: "FOREX",
      edgeType: "TREND_FOLLOWING",
      indicatorType: "RSI",
      maFast: "20",
      maSlow: "200",
      maType: "EMA",
      slMethod: "ATR_MULTIPLE",
      slAtrPeriod: "14",
      slAtrMultiple: "1.5",
      tpPrimary: "FIXED_RR",
      tpRrTarget: "2.0",
      tpUseTrailing: true,
      tpTrailMethod: "ATR_TRAIL",
      tpTrailAtrPeriod: "14",
      tpTrailAtrMultiple: "1.5",
      riskPerTradePct: "0.5",
      maxOpenRiskPct: "3.0",
      dailyMaxLossR: "3.0",
      weeklyMaxLossR: "7.0",
      breakevenEnabled: true,
      breakevenAtR: "1.0",
      newsMode: "AVOID_NEWS",
      newsPreMinutes: "30",
      newsPostMinutes: "30",
      maxTradesPerDay: "5",
      preSessionChecklist: "Trend confirmed (price > EMA 200)\nPullback + RSI trigger\nRisk within limits\nSpread acceptable",
      postSessionChecklist: "Review trades\nUpdate journal",
      psychAfterBigWinR: "3.0",
      psychCooldownMinutes: "30",
      psychMaxConsecLosses: "3",
      psychReducedRiskPct: "0.5",
    },
    indicators: [
      { label: "EMA 200", detail: "Trend filter" },
      { label: "EMA 20/50", detail: "Pullback zone" },
      { label: "RSI 14", detail: "Pullback trigger" },
      { label: "ATR 14", detail: "Sizing" },
    ],
    patterns: [],
  },
  {
    id: "BOLLINGER_MEAN_REVERSION",
    label: "Bollinger Mean Reversion",
    category: "Reversion",
    accent: "green",
    description: "Range/mean reversion using Bollinger Bands + RSI confirmation.",
    defaults: {
      style: "INTRADAY",
      timeframe: "H1",
      marketType: "FOREX",
      edgeType: "MEAN_REVERSION",
      indicatorType: "RSI",
      maFast: "20",
      maSlow: "100",
      maType: "SMA",
      slMethod: "ATR_MULTIPLE",
      slAtrPeriod: "14",
      slAtrMultiple: "1.0",
      tpPrimary: "LEVEL_BASED",
      tpRrTarget: "2.0",
      tpUseTrailing: false,
      tpTrailMethod: "ATR_TRAIL",
      tpTrailAtrPeriod: "14",
      tpTrailAtrMultiple: "1.5",
      riskPerTradePct: "0.5",
      maxOpenRiskPct: "3.0",
      dailyMaxLossR: "3.0",
      weeklyMaxLossR: "7.0",
      breakevenEnabled: false,
      breakevenAtR: "1.0",
      newsMode: "AVOID_NEWS",
      newsPreMinutes: "30",
      newsPostMinutes: "30",
      maxTradesPerDay: "5",
      preSessionChecklist: "Market is ranging\nBands + RSI confirm\nAvoid major news",
      postSessionChecklist: "Review trades\nUpdate journal",
      psychAfterBigWinR: "3.0",
      psychCooldownMinutes: "30",
      psychMaxConsecLosses: "3",
      psychReducedRiskPct: "0.5",
    },
    indicators: [
      { label: "Bollinger (20,2.0)", detail: "Mean reversion bands" },
      { label: "RSI 14", detail: "Oversold/overbought" },
      { label: "EMA 100", detail: "No-trend guard" },
      { label: "ATR 14", detail: "Sizing" },
    ],
    patterns: [],
  },
  {
    id: "DONCHIAN_BREAKOUT",
    label: "Donchian Breakout",
    category: "Breakout",
    accent: "purple",
    description: "Breakout of recent range using Donchian channels with volatility filter.",
    defaults: {
      style: "INTRADAY",
      timeframe: "H1",
      marketType: "FOREX",
      edgeType: "BREAKOUT",
      indicatorType: "NONE",
      slMethod: "SWING_HIGH_LOW",
      slSwingBuffer: "3",
      slAtrPeriod: "14",
      slAtrMultiple: "1.5",
      tpPrimary: "FIXED_RR",
      tpRrTarget: "2.0",
      tpUseTrailing: true,
      tpTrailMethod: "ATR_TRAIL",
      tpTrailAtrPeriod: "14",
      tpTrailAtrMultiple: "1.5",
      riskPerTradePct: "0.5",
      maxOpenRiskPct: "3.0",
      dailyMaxLossR: "3.0",
      weeklyMaxLossR: "7.0",
      breakevenEnabled: true,
      breakevenAtR: "1.0",
      newsMode: "AVOID_NEWS",
      newsPreMinutes: "30",
      newsPostMinutes: "30",
      maxTradesPerDay: "5",
      preSessionChecklist: "Volatility sufficient\nBreakout close confirmed\nRisk within limits",
      postSessionChecklist: "Review trades\nUpdate journal",
      psychAfterBigWinR: "3.0",
      psychCooldownMinutes: "30",
      psychMaxConsecLosses: "3",
      psychReducedRiskPct: "0.5",
    },
    indicators: [
      { label: "Donchian 20", detail: "Range breakout" },
      { label: "ATR 14", detail: "Volatility filter" },
    ],
    patterns: [
      { label: "Inside bar breakout", detail: "Optional consolidation trigger" },
    ],
  },
  {
    id: "LONDON_BOX_BREAKOUT",
    label: "London Session Box Breakout",
    category: "Breakout",
    accent: "purple",
    recommended: true,
    description: "Classic London session range box breakout (M15).",
    defaults: {
      style: "SCALPER",
      timeframe: "M15",
      marketType: "FOREX",
      edgeType: "BREAKOUT",
      indicatorType: "NONE",
      slMethod: "FIXED_PIPS",
      slFixedPips: "20",
      slAtrPeriod: "14",
      slAtrMultiple: "1.5",
      tpPrimary: "FIXED_RR",
      tpRrTarget: "1.8",
      tpUseTrailing: true,
      tpTrailMethod: "ATR_TRAIL",
      tpTrailAtrPeriod: "14",
      tpTrailAtrMultiple: "1.5",
      riskPerTradePct: "0.5",
      maxOpenRiskPct: "3.0",
      dailyMaxLossR: "3.0",
      weeklyMaxLossR: "7.0",
      breakevenEnabled: true,
      breakevenAtR: "1.0",
      newsMode: "AVOID_NEWS",
      newsPreMinutes: "30",
      newsPostMinutes: "30",
      maxTradesPerDay: "2",
      preSessionChecklist: "Session times set\nSpread acceptable\nRisk within limits",
      postSessionChecklist: "Review trades\nUpdate journal",
      psychAfterBigWinR: "3.0",
      psychCooldownMinutes: "30",
      psychMaxConsecLosses: "3",
      psychReducedRiskPct: "0.5",
    },
    indicators: [
      { label: "Session Box", detail: "00:00–07:45 London" },
    ],
    patterns: [
      { label: "Breakout close", detail: "Close above box high" },
    ],
  },
  {
    id: "SWING_BOS_RETEST",
    label: "Swing BOS + Retest",
    category: "Structure",
    accent: "yellow",
    description: "Break of structure + retest confirmation using ATR buffers.",
    defaults: {
      style: "SWING",
      timeframe: "H1",
      marketType: "FOREX",
      edgeType: "TREND_FOLLOWING",
      indicatorType: "NONE",
      slMethod: "SWING_HIGH_LOW",
      slSwingBuffer: "3",
      slAtrPeriod: "14",
      slAtrMultiple: "1.5",
      tpPrimary: "FIXED_RR",
      tpRrTarget: "2.0",
      tpUseTrailing: true,
      tpTrailMethod: "SWING_TRAIL",
      tpTrailAtrPeriod: "14",
      tpTrailAtrMultiple: "1.5",
      riskPerTradePct: "0.5",
      maxOpenRiskPct: "3.0",
      dailyMaxLossR: "3.0",
      weeklyMaxLossR: "7.0",
      breakevenEnabled: true,
      breakevenAtR: "1.0",
      newsMode: "AVOID_NEWS",
      newsPreMinutes: "30",
      newsPostMinutes: "30",
      maxTradesPerDay: "5",
      preSessionChecklist: "BOS confirmed\nRetest tolerance met\nRisk within limits",
      postSessionChecklist: "Review trades\nUpdate journal",
      psychAfterBigWinR: "3.0",
      psychCooldownMinutes: "30",
      psychMaxConsecLosses: "3",
      psychReducedRiskPct: "0.5",
    },
    indicators: [
      { label: "ATR 14", detail: "Buffers + tolerance" },
    ],
    patterns: [
      { label: "BOS", detail: "Close beyond swing high/low" },
      { label: "Retest", detail: "Revisit broken level" },
    ],
  },
  {
    id: "MOMENTUM_RSI_EMA",
    label: "Momentum (RSI + EMA)",
    category: "Trend",
    accent: "blue",
    description: "Catch strong impulses: RSI momentum + EMA alignment.",
    defaults: {
      style: "INTRADAY",
      timeframe: "H1",
      marketType: "FOREX",
      edgeType: "TREND_FOLLOWING",
      indicatorType: "RSI",
      maFast: "50",
      maSlow: "200",
      maType: "EMA",
      slMethod: "ATR_MULTIPLE",
      slAtrPeriod: "14",
      slAtrMultiple: "1.5",
      tpPrimary: "FIXED_RR",
      tpRrTarget: "1.8",
      tpUseTrailing: true,
      tpTrailMethod: "ATR_TRAIL",
      tpTrailAtrPeriod: "14",
      tpTrailAtrMultiple: "1.5",
      riskPerTradePct: "0.5",
      maxOpenRiskPct: "3.0",
      dailyMaxLossR: "3.0",
      weeklyMaxLossR: "7.0",
      breakevenEnabled: true,
      breakevenAtR: "1.0",
      newsMode: "AVOID_NEWS",
      newsPreMinutes: "30",
      newsPostMinutes: "30",
      maxTradesPerDay: "5",
      preSessionChecklist: "EMA alignment confirmed\nRSI momentum trigger\nRisk within limits",
      postSessionChecklist: "Review trades\nUpdate journal",
      psychAfterBigWinR: "3.0",
      psychCooldownMinutes: "30",
      psychMaxConsecLosses: "3",
      psychReducedRiskPct: "0.5",
    },
    indicators: [
      { label: "RSI 14", detail: "Momentum trigger" },
      { label: "EMA 50/200", detail: "Trend alignment" },
      { label: "ATR 14", detail: "Sizing" },
    ],
    patterns: [],
  },
];

const accentPill = (accent: "blue" | "green" | "purple" | "yellow") => {
  const map = {
    blue: { bg: "rgba(59,130,246,0.16)", border: "rgba(59,130,246,0.35)", text: "#93c5fd" },
    green: { bg: "rgba(34,197,94,0.14)", border: "rgba(34,197,94,0.35)", text: "#86efac" },
    purple: { bg: "rgba(168,85,247,0.14)", border: "rgba(168,85,247,0.35)", text: "#d8b4fe" },
    yellow: { bg: "rgba(250,204,21,0.14)", border: "rgba(250,204,21,0.40)", text: "#fde047" },
  } as const;
  return map[accent];
};

const glassCardStyle: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.10)",
  borderRadius: 14,
  background: "linear-gradient(180deg, rgba(10,16,35,0.72), rgba(6,10,25,0.85))",
  boxShadow: "0 10px 30px rgba(0,0,0,0.45)",
};

const pillStyle = (accent: "blue" | "green" | "purple" | "yellow"): React.CSSProperties => {
  const a = accentPill(accent);
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    padding: "0.18rem 0.55rem",
    borderRadius: 999,
    border: `1px solid ${a.border}`,
    background: a.bg,
    color: a.text,
    fontSize: "0.75rem",
    fontWeight: 600,
    whiteSpace: "nowrap",
  };
};

export default function CreateStrategyPage() {
  const router = useRouter();

  const [archetypeId, setArchetypeId] = useState<ArchetypeId>("TREND_EMA_CROSSOVER");
  const selectedArchetype = ARCHETYPES.find((a) => a.id === archetypeId) || ARCHETYPES[0];

  const applyArchetypeDefaults = (tpl: ArchetypeTemplate) => {
    setStyle(tpl.defaults.style);
    setMarketType(tpl.defaults.marketType);
    setTimeframe(tpl.defaults.timeframe);
    setEdgeType(tpl.defaults.edgeType);
    setIndicatorType(tpl.defaults.indicatorType);
    setMaFast(tpl.defaults.maFast || maFast);
    setMaSlow(tpl.defaults.maSlow || maSlow);
    setMaType(tpl.defaults.maType || maType);
    setSlMethod(tpl.defaults.slMethod);
    setSlAtrPeriod(tpl.defaults.slAtrPeriod || slAtrPeriod);
    setSlAtrMultiple(tpl.defaults.slAtrMultiple || slAtrMultiple);
    setTpPrimary(tpl.defaults.tpPrimary);
    setTpRrTarget(tpl.defaults.tpRrTarget || tpRrTarget);
    setTpUseTrailing(Boolean(tpl.defaults.tpUseTrailing));
    setTpTrailMethod(tpl.defaults.tpTrailMethod || tpTrailMethod);
    setTpTrailAtrPeriod(tpl.defaults.tpTrailAtrPeriod || tpTrailAtrPeriod);
    setTpTrailAtrMultiple(tpl.defaults.tpTrailAtrMultiple || tpTrailAtrMultiple);
    setRiskPerTradePct(tpl.defaults.riskPerTradePct);
    setMaxOpenRiskPct(tpl.defaults.maxOpenRiskPct);
    setDailyMaxLossR(tpl.defaults.dailyMaxLossR);
    setWeeklyMaxLossR(tpl.defaults.weeklyMaxLossR);
    setBreakevenEnabled(tpl.defaults.breakevenEnabled);
    setBreakevenAtR(tpl.defaults.breakevenAtR);
    setNewsMode(tpl.defaults.newsMode);
    setNewsPreMinutes(tpl.defaults.newsPreMinutes);
    setNewsPostMinutes(tpl.defaults.newsPostMinutes);
    setMaxTradesPerDay(tpl.defaults.maxTradesPerDay);
    setPreSessionChecklist(tpl.defaults.preSessionChecklist.split('\n').map(text => ({ text, checked: false })));
    setPostSessionChecklist(tpl.defaults.postSessionChecklist.split('\n').map(text => ({ text, checked: false })));
    setPsychAfterBigWinR(tpl.defaults.psychAfterBigWinR);
    setPsychCooldownMinutes(tpl.defaults.psychCooldownMinutes);
    setPsychMaxConsecLosses(tpl.defaults.psychMaxConsecLosses);
    setPsychReducedRiskPct(tpl.defaults.psychReducedRiskPct);
  };

  const [accessToken, setAccessToken] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);

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
  const [preSessionChecklist, setPreSessionChecklist] = useState<{text: string; checked: boolean}[]>([
    { text: "Check economic calendar", checked: false },
    { text: "Mark key levels", checked: false },
    { text: "Define directional bias", checked: false }
  ]);
  const [postSessionChecklist, setPostSessionChecklist] = useState<{text: string; checked: boolean}[]>([
    { text: "Review trades", checked: false },
    { text: "Capture screenshots", checked: false },
    { text: "Update journal", checked: false }
  ]);
  const [psychAfterBigWinR, setPsychAfterBigWinR] = useState("3.0");
  const [psychCooldownMinutes, setPsychCooldownMinutes] = useState("30");
  const [psychMaxConsecLosses, setPsychMaxConsecLosses] = useState("3");
  const [psychReducedRiskPct, setPsychReducedRiskPct] = useState("0.5");

  // 13. Backtesting & Metrics (informational only here)
  // no direct config; just guidance/link later

  // Load token
  useEffect(() => {
    const checkAuth = async () => {
      if (typeof window !== "undefined") {
        const stored = window.localStorage.getItem("guvfx_access_token");
        if (stored) {
          setAccessToken(stored);
        } else {
          // Try cookie-based auth
          try {
            await apiFetch("/api/auth/me/", { method: "GET" });
            setAccessToken("cookie"); // Set any non-empty string to enable UI
          } catch {
            // User not logged in, leave accessToken empty
          }
        }
      }
    };
    checkAuth();
  }, []);

  // Apply archetype defaults when archetype changes (keeps builder fast)
  useEffect(() => {
    applyArchetypeDefaults(selectedArchetype);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [archetypeId]);

  const getTimeframeOptions = () => {
    // Keep simple for v1 demo: allow common TFs, still nudged by style.
    const base = ["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1", "W1"];
    if (style === "SCALPER") return ["M1", "M3", "M5", "M15"];
    if (style === "INTRADAY") return ["M15", "M30", "H1", "H4"];
    if (style === "SWING") return ["H1", "H4", "D1"];
    if (style === "POSITION") return ["D1", "W1"];
    return base;
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
      pre_session_checklist: preSessionChecklist.map(item => item.text).filter(Boolean),
      post_session_checklist: postSessionChecklist.map(item => item.text).filter(Boolean),
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

        <div
          style={{
            display: "flex",
            gap: 10,
            flexWrap: "wrap",
            alignItems: "center",
            marginBottom: "1rem",
          }}
        >
          <Button
            type="button"
            onClick={() => setShowAdvanced((v) => !v)}
            style={{
              border: "1px solid rgba(168,85,247,0.35)",
              background: showAdvanced
                ? "linear-gradient(90deg, rgba(168,85,247,0.22), rgba(59,130,246,0.12))"
                : "rgba(255,255,255,0.06)",
            }}
          >
            {showAdvanced ? "Hide advanced" : "Show advanced"}
          </Button>
          <span style={{ fontSize: "0.85rem", color: "#9ca3af" }}>
            Advanced = indicators, filters, psychology, and extra risk controls.
          </span>
        </div>

        {error && <Alert type="error">{error}</Alert>}
        {info && <Alert type="info">{info}</Alert>}

        {!accessToken && (
          <p style={{ fontStyle: "italic", fontSize: "0.9rem" }}>
            {/* No access token loaded */}
          </p>
        )}

        <form onSubmit={handleSubmit}>
          <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
            {/* 0) Overview */}
            <Card
              title="0) Overview"
              subtitle="Give your strategy a name and optional description."
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit,minmax(260px,1fr))",
                  gap: "0.75rem 1.5rem",
                }}
              >
                <div>
                  <label
                    htmlFor="strategyName"
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
                    id="strategyName"
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. London Breakout v1"
                    style={{
                      width: "100%",
                      padding: "0.6rem 0.8rem",
                      borderRadius: 8,
                      border: "1px solid rgba(168,85,247,0.35)",
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
                    htmlFor="strategyDescription"
                    style={{
                      display: "block",
                      fontSize: "0.85rem",
                      color: "#cbd5f5",
                      marginBottom: "0.25rem",
                    }}
                  >
                    Description (optional)
                  </label>
                  <input
                    id="strategyDescription"
                    type="text"
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="Short notes about this setup"
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
            </Card>

            {/* 1) Archetype */}
            <Card
              title="1) Strategy archetype"
              subtitle="Pick a proven template. Defaults auto-fill below."
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit,minmax(270px,1fr))",
                  gap: 18,
                  marginTop: 8,
                }}
              >
                {ARCHETYPES.map((tpl) => {
                  const selected = tpl.id === archetypeId;
                  const accent = accentPill(tpl.accent);
                  return (
                    <div
                      key={tpl.id}
                      onClick={() => setArchetypeId(tpl.id)}
                      style={{
                        ...glassCardStyle,
                        border: selected
                          ? `2.2px solid ${accent.text}`
                          : `1px solid ${accent.border}`,
                        background: selected
                          ? "linear-gradient(180deg, rgba(40,60,110,0.24), rgba(9,14,35,0.94))"
                          : glassCardStyle.background,
                        cursor: "pointer",
                        padding: "1rem 1.15rem",
                        transition: "border 0.22s, background 0.22s",
                        minHeight: 120,
                        display: "flex",
                        flexDirection: "column",
                        gap: "0.3rem",
                        position: "relative",
                      }}
                    >
                      <span style={pillStyle(tpl.accent)}>{tpl.category}</span>
                      {tpl.recommended && (
                        <span
                          style={{
                            color: "#6ee7b7",
                            fontSize: "0.75rem",
                            fontWeight: 500,
                            marginLeft: 7,
                          }}
                        >
                          Recommended
                        </span>
                      )}
                      <div style={{ fontWeight: 600, fontSize: "1.05rem", color: "#e5f4ff", margin: "0.25rem 0 0.15rem" }}>
                        {tpl.label}
                      </div>
                      <div style={{ color: "#b7c5dd", fontSize: "0.93rem", opacity: 0.93 }}>{tpl.description}</div>
                    </div>
                  );
                })}
              </div>
            </Card>

            {/* 2) Symbols */}
            <Card
              title="2) Symbols"
              subtitle="Select the forex pairs this strategy can trade."
            >
              <div style={{ marginBottom: "0.45rem", display: "flex", gap: 10 }}>
                <Button
                  type="button"
                  onClick={() =>
                    setSelectedSymbols([
                      "EURUSD",
                      "GBPUSD",
                      "USDJPY",
                      "AUDUSD",
                      "USDCAD",
                      "USDCHF",
                      "NZDUSD",
                    ])
                  }
                >
                  Majors
                </Button>
                <Button
                  type="button"
                  onClick={() =>
                    setSelectedSymbols(
                      FOREX_SYMBOLS.slice(0, 27) // All except last 4 exotics
                    )
                  }
                >
                  Majors + Crosses
                </Button>
                <Button
                  type="button"
                  onClick={() => setSelectedSymbols([])}
                >
                  Clear
                </Button>
              </div>
              <div
                style={{
                  marginBottom: "0.35rem",
                  fontSize: "0.85rem",
                  color: "#9ca3af",
                }}
              >
                Selected:{" "}
                {selectedSymbols.length > 0
                  ? selectedSymbols.join(", ")
                  : "None"}
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))",
                  gap: "0.5rem 1.2rem",
                  background: "rgba(3,7,18,0.9)",
                  border: "1px solid rgba(148,163,184,0.65)",
                  borderRadius: 8,
                  padding: "0.7rem 0.7rem",
                  maxHeight: 260,
                  overflowY: "auto",
                }}
              >
                {FOREX_SYMBOLS.map((sym) => (
                  <label
                    key={sym}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      fontSize: "0.92rem",
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
            </Card>

            {/* 3) Market & Timeframe */}
            <Card
              title="3) Market & timeframe"
            >
              <div style={{ fontSize: "0.85rem", color: "#7dd3fc", marginBottom: 8 }}>
                Default: {selectedArchetype.defaults.timeframe}
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))",
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
              </div>
            </Card>

            {false && (
            <>
            {/* 5) Indicators & patterns */}
            <Card
              title="5) Indicators & patterns"
            >
              <div style={{ marginBottom: 10 }}>
                <div style={{ fontWeight: 500, color: "#cbd5f5", fontSize: "0.93rem", marginBottom: 3 }}>
                  Indicators:
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 6 }}>
                  {selectedArchetype.indicators.length === 0 && (
                    <span style={{ color: "#64748b", fontSize: "0.85rem" }}>None</span>
                  )}
                  {selectedArchetype.indicators.map((ind, idx) => (
                    <span key={ind.label + idx} style={pillStyle(selectedArchetype.accent)}>
                      {ind.label}
                      <span style={{ fontWeight: 400, color: "#a3e635", marginLeft: 6, fontSize: "0.8em" }}>
                        {ind.detail}
                      </span>
                    </span>
                  ))}
                </div>
                <div style={{ fontWeight: 500, color: "#cbd5f5", fontSize: "0.93rem", marginBottom: 3 }}>
                  Patterns:
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                  {selectedArchetype.patterns.length === 0 && (
                    <span style={{ color: "#64748b", fontSize: "0.85rem" }}>None</span>
                  )}
                  {selectedArchetype.patterns.map((pat, idx) => (
                    <span key={pat.label + idx} style={pillStyle("purple")}>
                      {pat.label}
                      <span style={{ fontWeight: 400, color: "#fbbf24", marginLeft: 6, fontSize: "0.8em" }}>
                        {pat.detail}
                      </span>
                    </span>
                  ))}
                </div>
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))",
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
            </Card>
            </>
            )}

            {!showAdvanced && (
            <>
            {/* 4) Stops & take profit */}
            <Card
              title="4) Stops & take profit"
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: "1.2rem",
                }}
              >
                {/* Stop Loss Side */}
                <div>
                  <h4 style={{ color: "#cbd5f5", fontSize: "1rem", margin: 0, marginBottom: 4 }}>
                    Stop loss
                  </h4>
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
                {/* Take Profit Side */}
                <div>
                  <h4 style={{ color: "#cbd5f5", fontSize: "1rem", margin: 0, marginBottom: 4 }}>
                    Take profit
                  </h4>
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
              </div>
            </Card>
            </>
            )}

            {false && (
            <>
            {/* 7) Risk limits & trade management */}
            <Card
              title="7) Risk limits & trade management"
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))",
                  gap: "0.75rem 1.5rem",
                }}
              >
                {/* Position sizing */}
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
                {/* Risk & money management */}
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
                {/* Trade management */}
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
                {/* Filters & conditions */}
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
            </Card>
            </>
            )}

            {false && (
            <>
            {/* 8) Trading plan & psychology */}
            <Card
              title="8) Trading plan & psychology"
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
                  gap: "0.75rem 1.5rem",
                }}
              >
                <div>
                  <div
                    style={{
                      display: "block",
                      fontSize: "0.85rem",
                      color: "#cbd5f5",
                      marginBottom: "0.5rem",
                      fontWeight: 500,
                    }}
                  >
                    Pre-session checklist
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                    {preSessionChecklist.map((item, idx) => (
                      <label
                        key={idx}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "0.5rem",
                          cursor: "pointer",
                          fontSize: "0.88rem",
                          color: "#e5f4ff",
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={item.checked}
                          onChange={(e) => {
                            const updated = [...preSessionChecklist];
                            updated[idx].checked = e.target.checked;
                            setPreSessionChecklist(updated);
                          }}
                          style={{
                            width: 16,
                            height: 16,
                            cursor: "pointer",
                            accentColor: "#a855f7",
                          }}
                        />
                        <span>{item.text}</span>
                      </label>
                    ))}
                  </div>
                </div>
                <div>
                  <div
                    style={{
                      display: "block",
                      fontSize: "0.85rem",
                      color: "#cbd5f5",
                      marginBottom: "0.5rem",
                      fontWeight: 500,
                    }}
                  >
                    Post-session checklist
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                    {postSessionChecklist.map((item, idx) => (
                      <label
                        key={idx}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "0.5rem",
                          cursor: "pointer",
                          fontSize: "0.88rem",
                          color: "#e5f4ff",
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={item.checked}
                          onChange={(e) => {
                            const updated = [...postSessionChecklist];
                            updated[idx].checked = e.target.checked;
                            setPostSessionChecklist(updated);
                          }}
                          style={{
                            width: 16,
                            height: 16,
                            cursor: "pointer",
                            accentColor: "#a855f7",
                          }}
                        />
                        <span>{item.text}</span>
                      </label>
                    ))}
                  </div>
                </div>
              </div>
              <div
                style={{
                  marginTop: "0.75rem",
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
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
            </Card>
            </>
            )}

            {showAdvanced && (
            <Card>
              <div>
              {/* 4) Trade idea (edge) */}
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
                    4) Trade idea (edge)
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

              {/* 5) Indicators & patterns */}
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
                    5) Indicators & patterns
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

              {/* 6) Stops & take profit */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  6) Stop loss rules
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

              {/* 6) Stops & take profit (continued) */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  6) Take profit rules
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

              {/* 7) Risk limits & trade management */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  7) Position sizing
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

              {/* 8) Risk limits & trade management (continued) */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  8) Trade management
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

              {/* 9) Filters & conditions */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  9) Filters & conditions
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

              {/* 10) Risk & money management */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  10) Risk & money management (overall)
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

              {/* 11) Trading plan & psychology */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  11) Trading plan & psychology
                </h3>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
                    gap: "0.75rem 1.5rem",
                  }}
                >
                  <div>
                    <div
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.5rem",
                        fontWeight: 500,
                      }}
                    >
                      Pre-session checklist
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                      {preSessionChecklist.map((item, idx) => (
                        <label
                          key={idx}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: "0.5rem",
                            cursor: "pointer",
                            fontSize: "0.88rem",
                            color: "#e5f4ff",
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={item.checked}
                            onChange={(e) => {
                              const updated = [...preSessionChecklist];
                              updated[idx].checked = e.target.checked;
                              setPreSessionChecklist(updated);
                            }}
                            style={{
                              width: 16,
                              height: 16,
                              cursor: "pointer",
                              accentColor: "#a855f7",
                            }}
                          />
                          <span>{item.text}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div
                      style={{
                        display: "block",
                        fontSize: "0.85rem",
                        color: "#cbd5f5",
                        marginBottom: "0.5rem",
                        fontWeight: 500,
                      }}
                    >
                      Post-session checklist
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                      {postSessionChecklist.map((item, idx) => (
                        <label
                          key={idx}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: "0.5rem",
                            cursor: "pointer",
                            fontSize: "0.88rem",
                            color: "#e5f4ff",
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={item.checked}
                            onChange={(e) => {
                              const updated = [...postSessionChecklist];
                              updated[idx].checked = e.target.checked;
                              setPostSessionChecklist(updated);
                            }}
                            style={{
                              width: 16,
                              height: 16,
                              cursor: "pointer",
                              accentColor: "#a855f7",
                            }}
                          />
                          <span>{item.text}</span>
                        </label>
                      ))}
                    </div>
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

              {/* 12) Backtesting & metrics */}
              <section style={{ borderTop: "1px solid #1b2436", paddingTop: 16 }}>
                <h3
                  style={{
                    fontSize: "0.95rem",
                    color: "#e5f4ff",
                    margin: "0 0 0.4rem 0",
                  }}
                >
                  12) Backtesting & metrics
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
            </Card>
            )}
            {/* Review & Create Bar */}
            <Card
              title={showAdvanced ? "13) Review & create" : "5) Review & create"}
              subtitle="Quick preview before saving."
            >
              <div style={{ display: "flex", alignItems: "center", gap: 25, flexWrap: "wrap", marginBottom: 12 }}>
                <div>
                  <span style={{ fontWeight: 700, color: "#e5f4ff", fontSize: "1.1rem" }}>
                    {selectedArchetype.label}
                  </span>
                  <span style={{ marginLeft: 10, fontSize: "0.9rem", color: "#7dd3fc" }}>
                    ({selectedArchetype.category})
                  </span>
                </div>
                <div style={{ color: "#b7c5dd", fontSize: "0.97rem" }}>
                  Timeframe: <b>{timeframe || selectedArchetype.defaults.timeframe}</b>
                </div>
                <div style={{ color: "#b7c5dd", fontSize: "0.97rem" }}>
                  Symbols: <b>{selectedSymbols.length}</b>
                </div>
                <div style={{ color: "#b7c5dd", fontSize: "0.97rem" }}>
                  Risk/trade: <b>{riskPerTradePct}%</b>
                </div>
              </div>
              <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "0.5rem" }}>
                <Button type="submit" disabled={creating || !accessToken || !name.trim()}>
                  {creating ? "Creating…" : "Create strategy"}
                </Button>
                {(!accessToken || !name.trim()) && (
                  <span style={{ fontSize: "0.8rem", color: "#f87171", fontStyle: "italic" }}>
                    {!accessToken
                      ? "Please login to create strategies."
                      : !name.trim()
                      ? "Strategy name is required."
                      : ""}
                  </span>
                )}
              </div>
            </Card>

          </div>
        </form>
      </div>
    </AppShell>
  );
}
