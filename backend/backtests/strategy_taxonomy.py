"""
GuvFX Strategy Taxonomy (B20.5)

Registry-based architecture describing strategy families, definitions, trader
profiles, asset classes, setup types, and which families suit which market
states. Pure static research metadata — deterministic, no ML, no execution.

Registries:
  StrategyFamilyRegistry        — the 6 families
  StrategyDefinitionRegistry    — concrete strategy templates → family/setup
  TraderProfileRegistry         — trader archetypes → preferred families
  AssetClassRegistry            — asset classes → characteristics
  SetupTypeRegistry             — setup archetypes
  MarketStateSuitabilityRegistry— market state → family suitability
"""
from __future__ import annotations


class Registry:
    """Lightweight ordered key→value registry."""

    def __init__(self, name: str):
        self.name = name
        self._items: dict[str, dict] = {}

    def register(self, key: str, value: dict) -> None:
        self._items[key] = {"key": key, **value}

    def get(self, key: str) -> dict | None:
        return self._items.get(key)

    def all(self) -> list[dict]:
        return list(self._items.values())

    def keys(self) -> list[str]:
        return list(self._items.keys())


# ── Families ──
FAMILIES = ["TREND_FOLLOWING", "MEAN_REVERSION", "BREAKOUT", "SESSION", "MACRO", "HYBRID"]

StrategyFamilyRegistry = Registry("strategy_family")
StrategyFamilyRegistry.register("TREND_FOLLOWING", {
    "label": "Trend Following",
    "description": "Enters in the direction of an established trend and aims to ride continuation.",
    "thrives_in": ["TREND_EXPANSION", "RISK_ON"],
    "struggles_in": ["RANGE_COMPRESSION", "TREND_EXHAUSTION"],
})
StrategyFamilyRegistry.register("MEAN_REVERSION", {
    "label": "Mean Reversion",
    "description": "Fades over-extended moves expecting reversion toward an average.",
    "thrives_in": ["RANGE_COMPRESSION", "VOLATILITY_CONTRACTION", "TREND_EXHAUSTION"],
    "struggles_in": ["TREND_EXPANSION", "VOLATILITY_EXPANSION"],
})
StrategyFamilyRegistry.register("BREAKOUT", {
    "label": "Breakout",
    "description": "Engages when price expands beyond a established range or level.",
    "thrives_in": ["RANGE_EXPANSION", "VOLATILITY_EXPANSION"],
    "struggles_in": ["RANGE_COMPRESSION", "VOLATILITY_CONTRACTION"],
})
StrategyFamilyRegistry.register("SESSION", {
    "label": "Session",
    "description": "Built around a trading session's liquidity and range behaviour.",
    "thrives_in": ["RANGE_EXPANSION", "VOLATILITY_EXPANSION"],
    "struggles_in": ["NEWS_SHOCK"],
})
StrategyFamilyRegistry.register("MACRO", {
    "label": "Macro",
    "description": "Driven by macro/event regime context rather than micro price structure.",
    "thrives_in": ["RISK_ON", "RISK_OFF"],
    "struggles_in": [],
})
StrategyFamilyRegistry.register("HYBRID", {
    "label": "Hybrid",
    "description": "Combines multiple families; no single dominant edge.",
    "thrives_in": [],
    "struggles_in": [],
})

# ── Strategy definitions (concrete templates) ──
StrategyDefinitionRegistry = Registry("strategy_definition")
StrategyDefinitionRegistry.register("ema_trend", {
    "name": "EMA Trend", "family": "TREND_FOLLOWING", "setup_type": "trend-continuation",
    "description": "Moving-average alignment for trend continuation.",
})
StrategyDefinitionRegistry.register("rsi_mean_reversion", {
    "name": "RSI Mean Reversion", "family": "MEAN_REVERSION", "setup_type": "mean-reversion",
    "description": "RSI-based fade of over-extended conditions.",
})
StrategyDefinitionRegistry.register("atr_breakout", {
    "name": "ATR Breakout", "family": "BREAKOUT", "setup_type": "volatility-breakout",
    "description": "ATR-scaled breakout beyond a recent range.",
})
StrategyDefinitionRegistry.register("london_breakout", {
    "name": "London Breakout", "family": "SESSION", "setup_type": "session-breakout",
    "description": "Breakout of the London session opening range.",
})

# ── Trader profiles ──
TraderProfileRegistry = Registry("trader_profile")
TraderProfileRegistry.register("trend_trader", {
    "label": "Trend Trader", "preferred_families": ["TREND_FOLLOWING", "BREAKOUT"],
    "description": "Seeks directional continuation; patient in trends.",
})
TraderProfileRegistry.register("reversion_trader", {
    "label": "Mean-Reversion Trader", "preferred_families": ["MEAN_REVERSION"],
    "description": "Fades extremes in balanced/ranging markets.",
})
TraderProfileRegistry.register("breakout_trader", {
    "label": "Breakout Trader", "preferred_families": ["BREAKOUT", "SESSION"],
    "description": "Trades range expansion and volatility breakouts.",
})
TraderProfileRegistry.register("session_trader", {
    "label": "Session Trader", "preferred_families": ["SESSION"],
    "description": "Focuses on session-driven liquidity and ranges.",
})
TraderProfileRegistry.register("macro_trader", {
    "label": "Macro Trader", "preferred_families": ["MACRO", "TREND_FOLLOWING"],
    "description": "Positions around macro regime and event context.",
})

# ── Asset classes ──
AssetClassRegistry = Registry("asset_class")
for _k, _v in {
    "FX Major": {"liquidity": "high", "typical_volatility": "moderate", "risk_proxy": "neutral"},
    "FX Minor": {"liquidity": "medium", "typical_volatility": "moderate", "risk_proxy": "neutral"},
    "Metal": {"liquidity": "high", "typical_volatility": "elevated", "risk_proxy": "safe_haven"},
    "Index": {"liquidity": "high", "typical_volatility": "elevated", "risk_proxy": "risk_on"},
    "Crypto": {"liquidity": "medium", "typical_volatility": "high", "risk_proxy": "risk_on"},
    "Energy": {"liquidity": "medium", "typical_volatility": "high", "risk_proxy": "neutral"},
}.items():
    AssetClassRegistry.register(_k, _v)

# ── Setup types ──
SetupTypeRegistry = Registry("setup_type")
SetupTypeRegistry.register("trend-continuation", {"family": "TREND_FOLLOWING", "description": "Continuation in trend direction."})
SetupTypeRegistry.register("mean-reversion", {"family": "MEAN_REVERSION", "description": "Reversion toward an average."})
SetupTypeRegistry.register("volatility-breakout", {"family": "BREAKOUT", "description": "Expansion beyond a range."})
SetupTypeRegistry.register("session-breakout", {"family": "SESSION", "description": "Session opening-range breakout."})

# ── Market-state → family suitability ──
# values: HIGH / MEDIUM / LOW / AVOID
MarketStateSuitabilityRegistry = Registry("market_state_suitability")
MarketStateSuitabilityRegistry.register("TREND_EXPANSION", {"families": {
    "TREND_FOLLOWING": "HIGH", "BREAKOUT": "MEDIUM", "SESSION": "LOW", "MEAN_REVERSION": "AVOID", "MACRO": "LOW", "HYBRID": "LOW"}})
MarketStateSuitabilityRegistry.register("TREND_EXHAUSTION", {"families": {
    "MEAN_REVERSION": "MEDIUM", "TREND_FOLLOWING": "AVOID", "BREAKOUT": "LOW", "SESSION": "LOW", "MACRO": "LOW", "HYBRID": "LOW"}})
MarketStateSuitabilityRegistry.register("RANGE_COMPRESSION", {"families": {
    "MEAN_REVERSION": "MEDIUM", "BREAKOUT": "LOW", "SESSION": "MEDIUM", "TREND_FOLLOWING": "AVOID", "MACRO": "LOW", "HYBRID": "LOW"}})
MarketStateSuitabilityRegistry.register("RANGE_EXPANSION", {"families": {
    "BREAKOUT": "HIGH", "SESSION": "MEDIUM", "TREND_FOLLOWING": "MEDIUM", "MEAN_REVERSION": "LOW", "MACRO": "LOW", "HYBRID": "LOW"}})
MarketStateSuitabilityRegistry.register("VOLATILITY_EXPANSION", {"families": {
    "BREAKOUT": "HIGH", "SESSION": "MEDIUM", "TREND_FOLLOWING": "MEDIUM", "MEAN_REVERSION": "AVOID", "MACRO": "LOW", "HYBRID": "LOW"}})
MarketStateSuitabilityRegistry.register("VOLATILITY_CONTRACTION", {"families": {
    "MEAN_REVERSION": "HIGH", "SESSION": "MEDIUM", "BREAKOUT": "LOW", "TREND_FOLLOWING": "LOW", "MACRO": "LOW", "HYBRID": "LOW"}})
MarketStateSuitabilityRegistry.register("RISK_ON", {"families": {
    "TREND_FOLLOWING": "MEDIUM", "BREAKOUT": "MEDIUM", "MACRO": "MEDIUM", "MEAN_REVERSION": "LOW", "SESSION": "LOW", "HYBRID": "LOW"}})
MarketStateSuitabilityRegistry.register("RISK_OFF", {"families": {
    "MACRO": "MEDIUM", "MEAN_REVERSION": "LOW", "TREND_FOLLOWING": "LOW", "BREAKOUT": "LOW", "SESSION": "LOW", "HYBRID": "LOW"}})
MarketStateSuitabilityRegistry.register("NEWS_SHOCK", {"families": {
    "MACRO": "LOW", "TREND_FOLLOWING": "AVOID", "MEAN_REVERSION": "AVOID", "BREAKOUT": "AVOID", "SESSION": "AVOID", "HYBRID": "AVOID"},
    "note": "Elevated event uncertainty — most setups are lower-quality near a shock."})


def family_of(template: str) -> str | None:
    d = StrategyDefinitionRegistry.get(template)
    return d["family"] if d else None


def strategies_in_family(family: str) -> list[str]:
    return [d["key"] for d in StrategyDefinitionRegistry.all() if d.get("family") == family]
