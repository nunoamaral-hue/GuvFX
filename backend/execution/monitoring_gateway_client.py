"""execution.monitoring_gateway_client — backend → monitoring boundary transport (ADR-0075, DARK).

GFX-MT5-BRIDGE-SCALE-001, Phase 5 (transport half). Read-only. Resolves a ``MonitoringRoute`` to the
monitoring boundary and performs a structured identity + positions probe, returning a result the readiness
contract maps to a durable reason code.

Two modes, chosen by ``monitoring_gateway_routing_enabled()`` (ANDed with the master gate):
  * GATEWAY (the scale target): POST allow-listed read-only ops to the physical host's ONE gateway port,
    authenticated with ``X-GuvFX-Agent-Token`` + a per-request nonce/timestamp, carrying the account id +
    workspace_uuid so the gateway routes internally. No per-account Tailnet port is used.
  * LEGACY/TRANSITION (flag off): GET the account's own per-account endpoint exactly as the existing
    dashboard fetcher does — byte-identical reachability, so the foundation can ship DARK with zero change.

Token handling: read from the established env chain and attached as a header ONLY; its value is NEVER
logged, returned, or placed in a result. The HTTP seam is injectable (``transport``) so CI never touches
the network. Fail-closed: any error yields a not-reachable result with a sanitised reason code.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from execution import monitoring_models as mm

logger = logging.getLogger("guvfx.execution.monitoring.client")

_DEFAULT_TIMEOUT = 5.0


@dataclass
class ProbeResult:
    reachable: bool = False
    mode: str = "none"                 # "gateway" | "legacy" | "none"
    identity_ok: bool | None = None
    verified_login: str = ""           # masked
    verified_server: str = ""
    trade_mode: int | None = None
    is_demo: bool | None = None
    capabilities: list = field(default_factory=list)
    positions_ok: bool | None = None
    positions_error: str = ""
    n_positions: int | None = None
    reason_code: str = mm.RC_UNKNOWN


def _agent_token() -> str:
    """The agent token from the established env chain. NEVER logged or returned to a caller."""
    return (os.environ.get("WINDOWS_AGENT_TOKEN")
            or os.environ.get("GUVFX_WINDOWS_AGENT_TOKEN")
            or os.environ.get("GUVFX_AGENT_TOKEN") or "")


def _mask(login) -> str:
    s = str(login or "")
    return ("••" + s[-4:]) if len(s) >= 4 else "••"


def _default_transport(method: str, url: str, *, headers: dict, body: bytes | None,
                       timeout: float) -> tuple[int | None, dict]:
    """Stdlib HTTP seam → (http_status, json_dict). Never raises; a transport failure returns (None, {})."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, (json.loads(raw) if raw else {})
            except Exception:  # noqa: BLE001
                return r.status, {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception:  # noqa: BLE001 — network/timeout/DNS: fail-closed, no detail leak
        return None, {}


def _routing_enabled() -> bool:
    try:
        from execution.monitoring_flags import monitoring_gateway_routing_enabled
        return monitoring_gateway_routing_enabled()
    except Exception:  # noqa: BLE001
        return False


def probe_route(route, *, transport=None, timeout: float = _DEFAULT_TIMEOUT) -> ProbeResult:
    """Probe a route's monitoring boundary read-only and classify the outcome. Chooses gateway vs legacy
    transport by the routing flag + gateway health. Pure except for the injected ``transport`` (default
    stdlib HTTP). Fail-closed."""
    transport = transport or _default_transport
    token = _agent_token()
    if _routing_enabled() and route.gateway_id and route.gateway.is_routable:
        return _probe_gateway(route, transport=transport, timeout=timeout, token=token)
    return _probe_legacy(route, transport=transport, timeout=timeout, token=token)


def _probe_gateway(route, *, transport, timeout, token) -> ProbeResult:
    base = (route.gateway.gateway_base_url or "").rstrip("/")
    if not base:
        return ProbeResult(reachable=False, mode="gateway", reason_code=mm.RC_GATEWAY_UNHEALTHY)
    res = ProbeResult(mode="gateway")

    def _call(op):
        payload = {"op": op, "account_id": int(route.trading_account_id),
                   "workspace_uuid": str(route.workspace_uuid or ""),
                   "expected_generation": int(route.allocation_generation),
                   "nonce": secrets.token_hex(16), "ts": time.time()}
        headers = {"Content-Type": "application/json", "X-GuvFX-Agent-Token": token}
        return transport("POST", base, headers=headers, body=json.dumps(payload).encode("utf-8"),
                         timeout=timeout)

    st, ident = _call("attest_identity")
    if st is None:
        res.reachable = False
        res.reason_code = mm.RC_UNREACHABLE
        return res
    res.reachable = True
    if not (isinstance(ident, dict) and ident.get("ok")):
        # Fail-closed on a non-dict/null body; classify occupancy-integrity refusals (cross-tenant or
        # stale generation) as an identity mismatch, everything else as an unhealthy gateway.
        res.identity_ok = False
        err = ident.get("error") if isinstance(ident, dict) else None
        res.reason_code = (mm.RC_IDENTITY_MISMATCH
                           if err in ("workspace_mismatch", "generation_mismatch")
                           else mm.RC_GATEWAY_UNHEALTHY)
        return res
    res.identity_ok = True
    res.verified_login = str(ident.get("verified_login_masked") or "")
    res.verified_server = str(ident.get("verified_server") or "")
    res.is_demo = ident.get("is_demo")
    res.capabilities = list(ident.get("capabilities") or [])
    st2, env = _call("positions")
    # The gateway wraps a successful bridge read under "data"; an error envelope keeps {ok:False,error}.
    if isinstance(env, dict) and env.get("ok"):
        return _classify_positions(res, st2, env.get("data") or {})
    return _classify_positions(res, st2, env)


def _probe_legacy(route, *, transport, timeout, token) -> ProbeResult:
    ep = route.execution_endpoint
    base = (getattr(ep, "base_url", "") or "").rstrip("/")
    res = ProbeResult(mode="legacy")
    if not base:
        res.reachable = False
        res.reason_code = mm.RC_NO_ENDPOINT
        return res
    headers = {"X-GuvFX-Agent-Token": token}
    st, ident = transport("GET", base + "/mt5/snapshots/account", headers=headers, body=None,
                          timeout=timeout)
    if st is None:
        res.reachable = False
        res.reason_code = mm.RC_UNREACHABLE
        return res
    res.reachable = True
    if isinstance(ident, dict) and ident.get("ok"):
        res.identity_ok = True
        res.verified_login = _mask(ident.get("login") or ident.get("account_login"))
        res.verified_server = str(ident.get("server") or ident.get("account_server") or "")
        res.trade_mode = ident.get("trade_mode")
    else:
        res.identity_ok = False
    st2, pos = transport("GET", base + "/mt5/positions", headers=headers, body=None, timeout=timeout)
    return _classify_positions(res, st2, pos)


def _classify_positions(res: ProbeResult, status, pos) -> ProbeResult:
    """Map the positions read to the structured reason code (the Phase-1 taxonomy, deterministic)."""
    if status is None:
        res.positions_ok = None
        res.reason_code = mm.RC_UNREACHABLE
        return res
    if isinstance(pos, dict) and pos.get("ok"):
        positions = pos.get("positions")
        n = len(positions) if isinstance(positions, list) else 0
        res.positions_ok = True
        res.n_positions = n
        if pos.get("trade_mode") is not None:
            res.trade_mode = pos.get("trade_mode")
        res.reason_code = mm.RC_OK if n > 0 else mm.RC_NO_POSITION
        return res
    # ok:false / non-dict / data:null — classify the bridge error deterministically, fail-closed. Coerce
    # non-dict payloads to {} before any .get so a stray list / null never raises.
    res.positions_ok = False
    d = pos if isinstance(pos, dict) else {}
    inner = d.get("data") if isinstance(d.get("data"), dict) else {}
    err = str(d.get("error") or inner.get("error") or "").strip()
    res.positions_error = err
    res.reason_code = {
        "account_not_demo": mm.RC_FEATURE_DISABLED,
        "capability_not_advertised": mm.RC_FEATURE_DISABLED,
        "mt5_init_failed": mm.RC_TERMINAL_INIT_FAILED,
        "mt5_not_installed": mm.RC_TERMINAL_INIT_FAILED,
        "account_info_failed": mm.RC_TERMINAL_DISCONNECTED,
        "positions_get_failed": mm.RC_TERMINAL_DISCONNECTED,
        "unknown_route": mm.RC_LISTENER_ABSENT,
        "route_not_serving": mm.RC_LISTENER_ABSENT,
        "workspace_mismatch": mm.RC_IDENTITY_MISMATCH,
        "generation_mismatch": mm.RC_IDENTITY_MISMATCH,
    }.get(err, mm.RC_UNKNOWN)
    return res
