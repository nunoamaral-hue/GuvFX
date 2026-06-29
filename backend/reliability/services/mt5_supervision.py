"""RX-2B — MT5 Supervision Service.

Probes the Windows bridge additive endpoint GET /mt5/supervision (read-only)
and evaluates MT5 terminal / broker / snapshot health, scoped per terminal +
account. Detection only — never re-logs-in or restarts anything (Phase 1).
"""
import json
import os
import urllib.request

from ..constants import Component, HealthStatus, MT5_TICK_STALE_SECONDS
from . import health_store


def _agent():
    base = (os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL") or "").rstrip("/")
    # Token MUST match the 8788 bridge URL (RX-1 lesson): GUVFX_WINDOWS_AGENT_TOKEN.
    token = (os.getenv("GUVFX_WINDOWS_AGENT_TOKEN") or "").strip()
    return base, token


def probe_supervision(timeout=12):
    """Return the bridge supervision dict, or {'ok': False, 'error': ...}."""
    base, token = _agent()
    if not base:
        return {"ok": False, "error": "agent_base_not_configured"}
    url = f"{base}/mt5/supervision"
    req = urllib.request.Request(url, method="GET", headers={"X-GuvFX-Agent-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
            return json.loads(raw) if raw else {"ok": False, "error": "empty_response"}
    except Exception as e:  # noqa: BLE001 — fail-closed to UNKNOWN/FAILED
        return {"ok": False, "error": f"unreachable:{type(e).__name__}"}


def evaluate(terminal_node, trading_account=None, mt5_instance=None):
    """Evaluate MT5_TERMINAL, MT5_BROKER, SNAPSHOT_FEED for a terminal/account."""
    data = probe_supervision()
    detail = {"probe": data}

    if not data.get("ok"):
        # Bridge unreachable or endpoint missing → terminal/broker UNKNOWN, not FAILED,
        # to avoid false DOWN if the endpoint is not yet deployed.
        for comp in (Component.MT5_TERMINAL, Component.MT5_BROKER):
            health_store.upsert(comp, HealthStatus.UNKNOWN, detail=detail,
                                terminal_node=terminal_node, mt5_instance=mt5_instance, trading_account=trading_account)
        return data

    initialized = bool(data.get("mt5_initialized"))
    connected = bool(data.get("broker_connected"))
    tick_age = data.get("last_tick_age_s")

    # MT5_TERMINAL: is the terminal initialised/responsive?
    term_status = HealthStatus.OK if initialized else HealthStatus.FAILED
    health_store.upsert(Component.MT5_TERMINAL, term_status, detail={"mt5_initialized": initialized, "login": data.get("account_login")},
                        terminal_node=terminal_node, mt5_instance=mt5_instance, trading_account=trading_account)

    # MT5_BROKER: logged in + broker connected? (THE proven 2026-06-10 gap)
    broker_status = HealthStatus.OK if (initialized and connected) else HealthStatus.FAILED
    health_store.upsert(Component.MT5_BROKER, broker_status,
                        detail={"broker_connected": connected, "trade_allowed": data.get("trade_allowed"), "login": data.get("account_login"), "equity": data.get("equity")},
                        terminal_node=terminal_node, mt5_instance=mt5_instance, trading_account=trading_account)

    # SNAPSHOT_FEED freshness via last tick age.
    if tick_age is None:
        snap_status = HealthStatus.UNKNOWN
    elif tick_age <= MT5_TICK_STALE_SECONDS:
        snap_status = HealthStatus.OK
    else:
        snap_status = HealthStatus.STALE
    health_store.upsert(Component.SNAPSHOT_FEED, snap_status, detail={"last_tick_age_s": tick_age},
                        terminal_node=terminal_node, mt5_instance=mt5_instance, trading_account=trading_account)
    return data
