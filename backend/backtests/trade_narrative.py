"""
GuvFX Narrative / Explanation Layer (B20)

Deterministically converts a B19 Trade Intelligence Record into multiple
human-readable explanation formats:

  1. trader_summary  — short, simple: interesting? main reason? main risk?
  2. analyst_note    — detailed research explanation
  3. journal_note    — structured before/after review (dict)
  4. education_note  — explains the trading concept

B20 TRANSLATES the structured rationale — it must NOT invent facts or make
claims unsupported by the record. NO LLM, NO external calls, NO prediction,
NO social posts, NO marketing. Public-safe language is enforced (reusing the
B19 governance scrubber).

WIMS: internal machinery is never exposed publicly. Prepares for the future
WIMS-1 Social Content Adapter — but does not generate social content.
"""
from __future__ import annotations

import logging

from backtests.trade_intelligence import _public_language_pass, _tname, _TEMPLATE_SETUP

logger = logging.getLogger(__name__)

# Static, factual educational copy per strategy type (no claims, no prediction)
_EDUCATION = {
    "ema_trend": (
        "This is a trend-continuation setup built on moving-average alignment. "
        "Trend-following approaches are generally studied in markets with a clear "
        "directional bias and tend to struggle in choppy, range-bound conditions. "
        "A learner should notice the trend state and whether volatility supports continuation."
    ),
    "rsi_mean_reversion": (
        "This is a mean-reversion setup that looks for over-extended conditions to revert "
        "toward an average. Such setups are typically examined in range-bound or neutral "
        "conditions; strong directional trends can work against them. A learner should notice "
        "whether the market is ranging versus trending."
    ),
    "atr_breakout": (
        "This is a volatility-breakout setup that engages when price expands beyond a recent range. "
        "Volatility expansion and proximity to structural highs/lows are the conditions of interest. "
        "A learner should notice whether volatility is expanding and whether price is near a meaningful level."
    ),
    "london_breakout": (
        "This is a session-breakout setup tied to the London session's liquidity and range expansion. "
        "Session timing is the central idea. A learner should notice the active session and the breakout level."
    ),
}


def _fmt_quality(q: dict) -> str:
    s, lab = q.get("quality_score"), q.get("quality_label", "")
    return f"{s}/100 ({lab})" if s is not None else "not scored"


def _evidence_phrase(ev: dict) -> str:
    n = ev.get("similar_observation_count", 0)
    if n < 3:
        return f"Historical evidence is limited ({n} similar observation(s)) — interpret cautiously."
    return (
        f"Across {n} similar research observations, the average research score is "
        f"{ev.get('avg_research_score')} and the average setup quality is {ev.get('avg_quality_score')} "
        f"(strong {ev.get('strong_rate')}% / weak {ev.get('weak_rate')}%)."
    )


def generate_narrative(record: dict, fmt: str = "all") -> dict:
    """Translate a B19 record into readable explanation formats. Deterministic."""
    if not record:
        return {"trader_summary": "", "analyst_note": "", "journal_note": {}, "education_note": "",
                "content_safety_mode": "public_safe", "public_language_pass": True,
                "warnings": ["No record provided."]}

    ident = record.get("identity", {})
    ctx = record.get("market_context", {})
    quality = record.get("quality", {})
    ev = record.get("historical_evidence", {})
    thesis = record.get("trade_thesis", "")
    supporting = record.get("supporting_factors", [])
    risks = record.get("risk_factors", [])
    decisions = record.get("decision_notes", [])

    sym = ident.get("symbol", "")
    tf = ident.get("timeframe", "")
    tname = ident.get("template_name") or _tname(ident.get("template", ""))
    setup = ident.get("setup_type", "research setup")
    main_reason = supporting[0] if supporting else "no standout supporting factor in this sample"
    main_risk = risks[0] if risks else "no major risk flag in this sample"

    # 1. Trader Summary
    trader_summary = (
        f"{sym} ({tf}) — {tname} {setup} setup, quality {_fmt_quality(quality)}. "
        f"Main reason: {main_reason} Main risk: {main_risk} "
        + ("Historical evidence is limited, so treat this as early research context."
           if ev.get("similar_observation_count", 0) < 3 else
           f"Based on {ev.get('similar_observation_count')} similar research observations.")
    )

    # 2. Analyst Note
    news = ctx.get("news_context", {})
    news_bit = (
        f" A {news.get('impact')} -impact {news.get('event_type') or 'event'} "
        f"({news.get('event_relevance')} relevance) is noted nearby."
        if news.get("impact") not in (None, "NONE") else " No relevant economic event is noted nearby."
    )
    analyst_note = (
        f"{tname} on {sym} ({tf}). Market context: {ctx.get('trend_state','unknown')} trend, "
        f"{ctx.get('volatility_state','unknown')} volatility, {ctx.get('session_bucket','unknown')} session, "
        f"price {str(ctx.get('breakout_state','unknown')).replace('_',' ')}.{news_bit} "
        f"Setup quality is {_fmt_quality(quality)}"
        + (f" with knowledge-base confidence '{quality.get('confidence_level')}'." if quality.get("confidence_level") else ".")
        + f" {_evidence_phrase(ev)} "
        f"Supporting factors: {('; '.join(supporting))}. "
        f"Open risk factors: {('; '.join(risks))}."
        + (f" Notes: {' '.join(decisions)}" if decisions else "")
    )

    # 3. Journal Note (structured)
    journal_note = {
        "setup": f"{tname} ({setup}) on {sym} {tf} — direction bias: {ident.get('direction','context-dependent')}.",
        "context": (
            f"{ctx.get('trend_state','unknown')} trend, {ctx.get('volatility_state','unknown')} volatility, "
            f"{ctx.get('session_bucket','unknown')} session, structure: {str(ctx.get('breakout_state','unknown')).replace('_',' ')}."
        ),
        "entry_thesis": thesis,
        "risk_factors": risks,
        "review_after_outcome": _review_prompts(ctx, risks, tname),
    }

    # 4. Education Note
    education_note = (
        _EDUCATION.get(ident.get("template", ""), "This is a research setup evaluated against strategy criteria.")
        + f" In this instance, the conditions were: {ctx.get('trend_state','unknown')} trend and "
        f"{ctx.get('volatility_state','unknown')} volatility — which is why the {tname} criteria flagged it."
    )

    out = {
        "trader_summary": trader_summary,
        "analyst_note": analyst_note,
        "journal_note": journal_note,
        "education_note": education_note,
        "content_safety_mode": "public_safe",
    }
    if fmt and fmt != "all":
        keep = {"trader": "trader_summary", "analyst": "analyst_note",
                "journal": "journal_note", "education": "education_note"}.get(fmt)
        if keep:
            out = {k: (v if k == keep else (None if k != "content_safety_mode" else v)) for k, v in out.items()}

    clean, warnings, out = _public_language_pass(out)
    out["public_language_pass"] = clean
    out["warnings"] = warnings
    return out


def _review_prompts(ctx: dict, risks: list[str], tname: str) -> list[str]:
    """Deterministic post-trade review questions (decision-quality focused)."""
    prompts = [
        f"Did the {ctx.get('trend_state','observed')} trend persist through the trade?",
        "Did the setup respect the strategy's criteria, or was discipline compromised?",
        "Compare the realized outcome to the setup quality — was a good-quality setup rewarded, "
        "or did a lower-quality setup get a lucky result? (Decision quality is separate from outcome.)",
    ]
    news = ctx.get("news_context", {})
    if news.get("impact") not in (None, "NONE"):
        prompts.append(f"Did the {news.get('event_type') or 'economic event'} cause the expected volatility, and did it help or hurt?")
    if any("position-size" in r or "high-notional" in r.lower() for r in risks):
        prompts.append("Was the position size appropriate for this instrument's notional / tick value?")
    return prompts
