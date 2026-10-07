"""P0-B1 — per-tenant order-bridge config generation (pure, server-derived, ASCII-only).

Renders the env a per-tenant pin-enforcing bridge process is launched with — the multi-tenant generalisation
of the host's single ``node2_bridge.env.bat`` (which was hard-pinned to ``MT5_ACCOUNT_ID=25`` / account 25's
terminal on the fixed port 8789). Every value comes from the authoritative ``HostedExecutionEndpoint`` (itself
server-derived); NOTHING here is client-supplied. Output is ASCII-only so Windows PowerShell/CMD parse it
identically under any encoding (RULE 9 corollary). This module renders text only — it starts no process and
places no order; supervised launch is a separately-gated host concern.
"""
from __future__ import annotations


def render_bridge_env(endpoint, *, allow_live: bool = False) -> str:
    """Return the ``.bat`` env body for ``endpoint``'s dedicated bridge. Parameterised per tenant: its OWN
    account id, terminal path, port, and the full server-derived identity the bridge enforces every order
    against (login/server/windows_username/workspace_uuid).

    D4b — ``MT5_ALLOW_LIVE`` is PER-RUNTIME, never global. It is emitted as ``1`` ONLY when the caller passes
    ``allow_live=True`` (which it derives from ``allow_live_for_account`` — flag + policy-LIVE + a valid §3
    authorization + identity); the default (and every demo runtime) OMITS it entirely, byte-identical to the
    pre-D4 posture. Setting it is NEVER sufficient by itself: the bridge still enforces the mandatory identity
    pin + ``evaluate_binding`` at order time. ``MT5_EXPECTED_IS_DEMO`` continues to reflect the runtime's own
    environment, so a live runtime is 0 and a demo runtime is 1."""
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
        # Fixed safety posture (identical to the certified single-tenant bridge): mandatory pin + guarded attach.
        _kv("MT5_REQUIRE_IDENTITY_PIN", "1"),
        _kv("MT5_GUARDED_ATTACH", "1"),
    ]
    if allow_live:
        # Per-runtime live authorization (D4b). Only reached when the caller's allow_live_for_account() held.
        lines.append(_kv("MT5_ALLOW_LIVE", "1"))
    return "\r\n".join(lines) + "\r\n"


def allow_live_for_account(account) -> bool:
    """D4b — the fail-closed per-runtime decision for whether ``MT5_ALLOW_LIVE`` may be set for ``account``'s
    bridge: the account is authoritatively LIVE (``is_live_environment``) AND holds a valid §3 authorization
    under the armed flag (``live_execution_permitted``). A DEMO account, an un-authorized LIVE account, a
    demo/live mismatch, or any error ⇒ False (MT5_ALLOW_LIVE omitted). Never a global switch — it is evaluated
    per account; the bridge's identity pin + order-time gate remain the final authority."""
    try:
        from trading.account_policy import is_live_environment
        from execution.live_authz import live_execution_permitted
        return bool(is_live_environment(account) and live_execution_permitted(account))
    except Exception:  # noqa: BLE001 — fail closed
        return False


def _kv(key: str, value) -> str:
    v = "" if value is None else str(value)
    # Defence: the env is a batch file; reject any control/newline injection (all values are server-derived,
    # so this should never trigger — it is a belt-and-braces guard, not input validation of customer data).
    if any(ord(ch) < 32 for ch in v):
        raise ValueError(f"illegal control character in bridge env value for {key}")
    return f"set {key}={v}"
