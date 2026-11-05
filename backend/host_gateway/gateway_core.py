"""host_gateway.gateway_core — the pure authenticated monitoring-gateway request handler (ADR-0075).

Stdlib-only, transport-agnostic, deterministic (clock + bridge calls injected), so CI exercises every
security property in-process without sockets or a host. Security properties enforced here:

  * AUTHENTICATION fail-closed + total: constant-time token compare (``hmac.compare_digest``); an UNSET
    expected token denies every request and refuses startup (never a silent allow); a malformed token
    yields UNAUTHENTICATED, never a crash.
  * ALLOW-LISTED read-only ops only (``ALLOWED_OPS``): anything else is OP_NOT_ALLOWED. No write/order op
    exists in the vocabulary at all.
  * ROUTE BY REGISTERED IDENTITY ONLY: the target local bridge port is looked up in the registry by
    account id; the request can never supply a host/port/path, so there is no arbitrary port forwarding
    and no path traversal (ops are a fixed enum, not a path).
  * CROSS-TENANT isolation: the request's ``workspace_uuid`` must equal the registered occupancy's, and an
    optional ``expected_generation`` must match — a request for account A carrying B's binding is refused.
  * STALE / TOMBSTONED / DRAINING registrations are refused (only a SERVING entry routes).
  * REPLAY protection: a per-request nonce must be unseen within a TTL window and the timestamp within a
    bounded skew.
  * CAPABILITY gating: a data op must be advertised by the route.
  * SANITISED errors: stable short codes only; no internal detail, no secret, no login in the clear.
"""
from __future__ import annotations

import hmac
import math
import time

PROTOCOL_VERSION = "mg-1"

# The COMPLETE operation vocabulary — read-only monitoring only. There is deliberately no order/write/
# modify/close op: the gateway structurally cannot place or mutate a trade.
GATEWAY_OPS = frozenset({"health"})               # gateway-level (no per-account route needed)
ROUTE_OPS = frozenset({"attest_identity", "account_snapshot", "positions"})
ALLOWED_OPS = GATEWAY_OPS | ROUTE_OPS
# Which ROUTE_OPS actually dial the local bridge (and therefore require an advertised capability).
_BRIDGE_OPS = frozenset({"account_snapshot", "positions"})

# Stable, sanitised error codes → transport status.
_STATUS = {
    "unauthenticated": 401,
    "op_not_allowed": 403,
    "malformed_request": 400,
    "stale_timestamp": 400,
    "replay_detected": 400,
    "unknown_route": 404,
    "route_not_serving": 409,
    "workspace_mismatch": 409,
    "generation_mismatch": 409,
    "capability_not_advertised": 400,
    "bridge_error": 502,
    "gateway_no_credential": 503,
}


class GatewayError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class _NonceCache:
    """Bounded TTL nonce set for replay protection, using a two-generation bucketed sweep so pruning is
    O(1) (a whole expired generation is discarded at once) and memory is bounded to at most two windows of
    nonces (plus an absolute size cap as a flood backstop) — never the O(n)-per-request scan + unbounded
    growth of a single dict. A nonce is remembered for at least ``ttl`` and at most ``2*ttl``, which is
    sufficient alongside the timestamp-skew bound."""

    def __init__(self, ttl: float, max_per_window: int = 200_000):
        self._ttl = float(ttl)
        self._max = int(max_per_window)
        self._cur: dict[str, bool] = {}
        self._prev: dict[str, bool] = {}
        self._window_start: float | None = None

    def check_and_add(self, nonce: str, now: float) -> bool:
        """True if the nonce is fresh (and records it); False if it is a replay within the window."""
        if self._window_start is None:
            self._window_start = now
        # Rotate generations when the window elapses OR the current bucket hits the flood cap (O(1)).
        if (now - self._window_start) >= self._ttl or len(self._cur) >= self._max:
            self._prev = self._cur
            self._cur = {}
            self._window_start = now
        if nonce in self._cur or nonce in self._prev:
            return False
        self._cur[nonce] = True
        return True


def _mask(login) -> str:
    s = str(login or "")
    return ("••" + s[-4:]) if len(s) >= 4 else "••"


class MonitoringGateway:
    """The per-host gateway. One backend-facing endpoint; routes internally to registered local bridges.

    ``bridge_caller(local_port:int, op:str) -> dict`` is the ONLY way the gateway reaches a bridge; it is
    always called with a port taken from the registry (never from the request). Injecting it keeps the
    core pure and testable and makes it impossible for a request to redirect the call elsewhere."""

    def __init__(self, *, gateway_id: str, host_key: str, expected_token, registry, bridge_caller,
                 clock=time.time, nonce_ttl: float = 120.0, ts_skew: float = 60.0):
        self.gateway_id = str(gateway_id)
        self.host_key = str(host_key)
        self._expected = _as_bytes(expected_token)
        self._registry = registry
        self._bridge_caller = bridge_caller
        self._clock = clock
        self._ts_skew = float(ts_skew)
        self._nonces = _NonceCache(nonce_ttl)

    # ── startup gate (security.md: an unset credential refuses startup) ──
    def assert_startup(self) -> None:
        if not self._expected:
            raise GatewayError("gateway_no_credential", "expected token is empty — refusing startup")

    def _verify_token(self, token) -> bool:
        """Constant-time, total, fail-closed. Empty expected ⇒ deny all. Malformed token ⇒ False (never
        raises)."""
        if not self._expected:
            return False
        try:
            return hmac.compare_digest(_as_bytes(token), self._expected)
        except Exception:  # noqa: BLE001 — any comparison error is a denial, never a 500
            return False

    def handle(self, request) -> dict:
        """Process one request dict; always RETURNS a dict (never raises to the transport). Error dicts are
        ``{"ok": False, "error": <code>, "_status": <int>}``; success dicts carry ``ok: True`` + payload."""
        try:
            return self._handle(request)
        except GatewayError as e:
            return {"ok": False, "error": e.code, "_status": _STATUS.get(e.code, 400)}
        except Exception:  # noqa: BLE001 — never leak an internal error to the caller
            return {"ok": False, "error": "bridge_error", "_status": 502}

    def _handle(self, request) -> dict:
        if not isinstance(request, dict):
            raise GatewayError("malformed_request")
        # 1) AUTH first — an unauthenticated caller learns nothing about routing.
        if not self._verify_token(request.get("token")):
            raise GatewayError("unauthenticated")
        # 2) timestamp skew + 3) nonce replay (bound every request, authenticated or not beyond this point).
        now = self._clock()
        ts = request.get("ts")
        # Fail closed on a non-numeric, boolean, NON-FINITE (NaN/Inf), or un-float-able (huge int) timestamp
        # — NaN would otherwise make the skew comparison False and silently bypass the freshness bound, and
        # a float()-overflowing int must be classified as a 4xx stale_timestamp, not a 5xx bridge_error.
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise GatewayError("stale_timestamp")
        try:
            tsf = float(ts)
        except (OverflowError, ValueError):
            raise GatewayError("stale_timestamp")
        if not math.isfinite(tsf) or abs(now - tsf) > self._ts_skew:
            raise GatewayError("stale_timestamp")
        nonce = request.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise GatewayError("malformed_request")
        if not self._nonces.check_and_add(nonce, now):
            raise GatewayError("replay_detected")
        # 4) op allow-list.
        op = request.get("op")
        if not isinstance(op, str) or op not in ALLOWED_OPS:
            raise GatewayError("op_not_allowed")
        # 5) gateway-level op (no route needed).
        if op in GATEWAY_OPS:
            return {"ok": True, "op": op, "gateway_id": self.gateway_id, "host_key": self.host_key,
                    "protocol": PROTOCOL_VERSION, "routes": len(self._registry), "_status": 200}
        # 6) route resolution — by REGISTERED IDENTITY only.
        account_id = request.get("account_id")
        if not isinstance(account_id, int):
            raise GatewayError("malformed_request")
        entry = self._registry.get(account_id)
        if entry is None:
            raise GatewayError("unknown_route")
        if not entry.is_serving():
            raise GatewayError("route_not_serving")
        if str(request.get("workspace_uuid") or "") != str(entry.workspace_uuid or ""):
            raise GatewayError("workspace_mismatch")
        exp_gen = request.get("expected_generation")
        if exp_gen is not None and int(exp_gen) != int(entry.generation):
            raise GatewayError("generation_mismatch")
        # 7) attestation (no bridge call).
        if op == "attest_identity":
            return {"ok": True, "op": op, "gateway_id": self.gateway_id, "host_key": self.host_key,
                    "protocol": PROTOCOL_VERSION, "account_id": account_id,
                    "workspace_uuid": entry.workspace_uuid, "generation": entry.generation,
                    "verified_login_masked": _mask(entry.verified_login),
                    "verified_server": entry.verified_server, "is_demo": entry.verified_is_demo,
                    "capabilities": list(entry.capabilities), "_status": 200}
        # 8) data op — require advertised capability, then dial the REGISTRY's local port (never the
        #    request's). Any bridge failure is sanitised to bridge_error.
        if op in _BRIDGE_OPS and op not in set(entry.capabilities):
            raise GatewayError("capability_not_advertised")
        try:
            data = self._bridge_caller(int(entry.local_port), op)
        except Exception:  # noqa: BLE001
            raise GatewayError("bridge_error")
        return {"ok": True, "op": op, "account_id": account_id, "data": data, "_status": 200}


def _as_bytes(v) -> bytes:
    if v is None:
        return b""
    if isinstance(v, bytes):
        return v
    return str(v).encode("utf-8")
