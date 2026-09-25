"""B1 — MT5 account margin-mode authority: raw int -> label + capability semantics.

The margin mode is observed READ-ONLY from ``MetaTrader5.account_info().margin_mode`` on the
host (an ``ENUM_ACCOUNT_MARGIN_MODE`` int). GuvFX NEVER infers it from broker/server names,
account naming, demo/live, or how many positions happen to exist.

ENUM_ACCOUNT_MARGIN_MODE (MQL5):
    RETAIL_NETTING = 0   -> only ONE position per symbol (same-symbol strategies merge)
    EXCHANGE       = 1    -> exchange netting semantics
    RETAIL_HEDGING = 2    -> multiple independent positions per symbol (per-strategy isolation)

RULE 11: the 0/1/2 mapping is the documented MQL5 enum, but any int NOT in the known set
(or None) maps to UNKNOWN — an unproven value is never asserted as a known mode. A positive
control (observe a known-hedging account and assert ``margin_mode == 2``) should be run on the
host before the conflict policy is ever armed.
"""
from __future__ import annotations

RETAIL_NETTING = 0
EXCHANGE = 1
RETAIL_HEDGING = 2

HEDGING = "HEDGING"
NETTING = "NETTING"
EXCHANGE_LABEL = "EXCHANGE"
UNKNOWN = "UNKNOWN"

_INT_TO_LABEL = {
    RETAIL_HEDGING: HEDGING,
    RETAIL_NETTING: NETTING,
    EXCHANGE: EXCHANGE_LABEL,
}


def label_for(raw) -> str:
    """Map a raw ENUM_ACCOUNT_MARGIN_MODE int to a stable label. Strict: only a genuine int maps;
    None / bool / str / float / unknown int -> UNKNOWN (RULE 11 — never assert an unproven value)."""
    if not isinstance(raw, int) or isinstance(raw, bool):
        return UNKNOWN
    return _INT_TO_LABEL.get(raw, UNKNOWN)


def supports_independent_same_symbol(label: str) -> bool:
    """True only for PROVEN hedging: the one mode where two strategies can independently own
    the same symbol at the same time. NETTING/EXCHANGE/UNKNOWN all fail closed."""
    return label == HEDGING


# Margin mode is a broker-account property that effectively never changes, so this is generous vs the 60s
# execution-readiness window; but a STALE value must never be trusted (it would be treated as UNKNOWN).
FRESHNESS_SECONDS = 24 * 3600


def label_if_fresh(raw, last_decision_at, now) -> str:
    """Map ``raw`` to a label ONLY when its observation (``last_decision_at``) is fresh vs ``now``; else
    UNKNOWN. Missing timestamp / staleness / future-skew all fail closed to UNKNOWN. This is the single
    freshness+mapping gate shared by the conflict policy and the capability read-model."""
    if last_decision_at is None or now is None:
        return UNKNOWN
    try:
        age = (now - last_decision_at).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return UNKNOWN
    if age < 0 or age > FRESHNESS_SECONDS:
        return UNKNOWN
    return label_for(raw)
