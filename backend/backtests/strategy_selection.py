"""
GuvFX Strategy Selection Framework (B20.7)

Combines Market State (B20.6) + Strategy Taxonomy (B20.5) + Knowledge Base
attribution / trade quality to produce RESEARCH GUIDANCE on which strategy
families and strategies suit the current conditions for a symbol.

Deterministic. NO ML, NO execution, NO deployment, NO automation.
Output is research guidance only — never an instruction to trade.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SUIT_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "AVOID": 0}


def select_strategies(bars, symbol: str = "", timeframe: str = "") -> dict:
    """Produce preferred families + strategies for the current market state."""
    from backtests.market_state import classify_market_state
    from backtests.strategy_taxonomy import (
        MarketStateSuitabilityRegistry, StrategyDefinitionRegistry,
        StrategyFamilyRegistry, strategies_in_family,
    )

    ms = classify_market_state(bars, symbol=symbol, timeframe=timeframe)
    state = ms.get("current_state", "UNKNOWN")

    suit = MarketStateSuitabilityRegistry.get(state)
    fam_suit = suit.get("families", {}) if suit else {}

    warnings: list[str] = []
    if state in ("UNKNOWN",):
        warnings.append("market state could not be determined — insufficient data")
    if state == "NEWS_SHOCK":
        warnings.append("NEWS_SHOCK — most setups are lower-quality near a high-impact event; guidance is conservative")

    # KB evidence per (symbol, template, timeframe)
    def kb_evidence(template: str) -> dict:
        try:
            from backtests.models import ResearchObservation
            from backtests.attribution import _is_strong, _is_weak
            qs = ResearchObservation.objects.filter(symbol=symbol, template=template, timeframe=timeframe)
            rows = list(qs.values("research_score", "quality_score"))
            n = len(rows)
            if n == 0:
                return {"n": 0}
            rs = [r["research_score"] for r in rows if r.get("research_score") is not None]
            qsc = [r["quality_score"] for r in rows if r.get("quality_score") is not None]
            return {
                "n": n,
                "avg_research_score": round(sum(rs) / len(rs), 1) if rs else None,
                "avg_quality_score": round(sum(qsc) / len(qsc), 1) if qsc else None,
            }
        except Exception:
            return {"n": 0}

    # Preferred families (suitability-ranked, excluding AVOID)
    preferred_families = []
    for fam, level in sorted(fam_suit.items(), key=lambda x: -_SUIT_RANK.get(x[1], 0)):
        if level == "AVOID":
            continue
        fam_meta = StrategyFamilyRegistry.get(fam) or {}
        preferred_families.append({
            "family": fam, "label": fam_meta.get("label", fam), "suitability": level,
        })

    # Candidate strategies (within non-AVOID families), scored by suitability + KB quality
    candidates = []
    for fam, level in fam_suit.items():
        if level == "AVOID":
            continue
        for template in strategies_in_family(fam):
            d = StrategyDefinitionRegistry.get(template) or {}
            ev = kb_evidence(template)
            # combined score: suitability (0-3 → 0-60) + KB quality (0-100 → 0-40 weighted)
            suit_pts = _SUIT_RANK.get(level, 0) * 20
            q = ev.get("avg_quality_score")
            q_pts = (q * 0.4) if q is not None else 0
            combined = round(suit_pts + q_pts, 1)
            candidates.append({
                "template": template, "name": d.get("name", template), "family": fam,
                "setup_type": d.get("setup_type"), "suitability": level,
                "kb_observations": ev.get("n", 0),
                "kb_avg_quality": ev.get("avg_quality_score"),
                "kb_avg_research_score": ev.get("avg_research_score"),
                "selection_score": combined,
            })
    candidates.sort(key=lambda c: -c["selection_score"])

    # Confidence: blend market-state confidence + whether top candidate has KB evidence
    ms_conf = ms.get("confidence", "low")
    top = candidates[0] if candidates else None
    has_evidence = bool(top and (top.get("kb_observations") or 0) >= 3)
    if ms_conf == "high" and has_evidence:
        confidence = "high"
    elif ms_conf == "low" and not has_evidence:
        confidence = "low"
    else:
        confidence = "medium"

    # Rationale (deterministic, cautious)
    rationale: list[str] = []
    fam_meta = StrategyFamilyRegistry.get(preferred_families[0]["family"]) if preferred_families else None
    rationale.append(
        f"Current market state is {state} ({ms_conf} confidence). "
        + (f"This state favours {fam_meta.get('label')} approaches." if fam_meta else "No family is clearly favoured.")
    )
    if top:
        rationale.append(
            f"Top research candidate: {top['name']} ({top['family']}, suitability {top['suitability']})"
            + (f", historical setup quality {top['kb_avg_quality']} over {top['kb_observations']} observations."
               if top.get("kb_avg_quality") is not None else ", limited historical evidence for this symbol.")
        )
    avoid = [f for f, lvl in fam_suit.items() if lvl == "AVOID"]
    if avoid:
        rationale.append("Families to avoid in this state: " + ", ".join(avoid) + ".")
    rationale.append("Research guidance only — not a recommendation, signal, or instruction to trade.")

    return {
        "symbol": symbol, "timeframe": timeframe,
        "market_state": ms,
        "preferred_families": preferred_families,
        "preferred_strategies": candidates[:5],
        "confidence": confidence,
        "rationale": rationale,
        "warnings": warnings,
        "mode": "research",
        "disclaimer": "Deterministic research guidance derived from market state, taxonomy, and historical "
                      "research observations. No execution, no deployment, no automation.",
    }
