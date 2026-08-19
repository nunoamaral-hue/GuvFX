"""P0-B1 — per-tenant order-bridge config generation (pure, server-derived, ASCII-only).

Renders the env a per-tenant pin-enforcing bridge process is launched with — the multi-tenant generalisation
of the host's single ``node2_bridge.env.bat`` (which was hard-pinned to ``MT5_ACCOUNT_ID=25`` / account 25's
terminal on the fixed port 8789). Every value comes from the authoritative ``HostedExecutionEndpoint`` (itself
server-derived); NOTHING here is client-supplied. Output is ASCII-only so Windows PowerShell/CMD parse it
identically under any encoding (RULE 9 corollary). This module renders text only — it starts no process and
places no order; supervised launch is a separately-gated host concern.
"""
from __future__ import annotations


def render_bridge_env(endpoint) -> str:
    """Return the ``.bat`` env body for ``endpoint``'s dedicated bridge. Mirrors the certified single-tenant
    posture (mandatory identity pin, guarded attach, DEMO-only — ``MT5_ALLOW_LIVE`` is never set) but is
    parameterised per tenant: its OWN account id, terminal path, port, and the full server-derived identity
    the bridge enforces every order against (login/server/windows_username/workspace_uuid)."""
    lines = [
        "REM GuvFX per-tenant order bridge env (P0-B1) - GENERATED, do not edit by hand.",
        "REM Server-derived from HostedExecutionEndpoint; ASCII-only (RULE 9).",
        _kv("MT5_ACCOUNT_ID", endpoint.trading_account_id),
        _kv("MT5_TERMINAL_PATH", endpoint.runtime_path),
        _kv("HTTP_SERVER_PORT", endpoint.port),
        _kv("MT5_EXPECTED_LOGIN", endpoint.expected_login),
        _kv("MT5_EXPECTED_SERVER", endpoint.expected_server),
        _kv("MT5_EXPECTED_WINDOWS_USERNAME", endpoint.windows_username),
        _kv("MT5_EXPECTED_WORKSPACE_UUID", str(endpoint.workspace_uuid)),
        _kv("MT5_EXPECTED_IS_DEMO", "1" if endpoint.is_demo else "0"),
        # Fixed safety posture (identical to the certified single-tenant bridge). MT5_ALLOW_LIVE stays UNSET.
        _kv("MT5_REQUIRE_IDENTITY_PIN", "1"),
        _kv("MT5_GUARDED_ATTACH", "1"),
    ]
    return "\r\n".join(lines) + "\r\n"


def _kv(key: str, value) -> str:
    v = "" if value is None else str(value)
    # Defence: the env is a batch file; reject any control/newline injection (all values are server-derived,
    # so this should never trigger — it is a belt-and-braces guard, not input validation of customer data).
    if any(ord(ch) < 32 for ch in v):
        raise ValueError(f"illegal control character in bridge env value for {key}")
    return f"set {key}={v}"
