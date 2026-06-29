"""
Portfolio layer hooks (no-op stubs v1).

Each function receives a meta dict (engine audit payload) and returns it
unchanged.  Future versions will implement real correlation dampening,
vol targeting, and macro overlay adjustments.

These hooks are called by composite engines (e.g. TC1, hybrid sleeve)
after signal generation but before lot sizing / job creation.
"""

from __future__ import annotations

from typing import Any, Dict

# Version constants — embedded in every audit payload so we can trace
# which version of each layer was active when a trade was generated.
CORR_DAMPENER_VERSION = "CORR_DAMP_V1_NOOP"
VOL_TARGET_VERSION = "VOL_TARGET_V1_NOOP"
MACRO_OVERLAY_VERSION = "MACRO_OVERLAY_V1_NOOP"


def apply_correlation_dampening(meta: Dict[str, Any]) -> Dict[str, Any]:
    """No-op: return meta unchanged.  Future: reduce risk_pct for correlated pairs."""
    meta["correlation_dampener_version"] = CORR_DAMPENER_VERSION
    return meta


def apply_vol_targeting(meta: Dict[str, Any]) -> Dict[str, Any]:
    """No-op: return meta unchanged.  Future: scale risk_pct to target portfolio vol."""
    meta["vol_target_version"] = VOL_TARGET_VERSION
    return meta


def apply_macro_overlay(meta: Dict[str, Any]) -> Dict[str, Any]:
    """No-op: return meta unchanged.  Future: adjust risk_pct based on macro regime."""
    meta["macro_overlay_version"] = MACRO_OVERLAY_VERSION
    return meta
