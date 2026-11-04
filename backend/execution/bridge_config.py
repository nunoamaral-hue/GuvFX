"""P0-B1 — per-tenant order-bridge config generation (pure, server-derived, ASCII-only).

Renders the env a per-tenant pin-enforcing bridge process is launched with — the multi-tenant generalisation
of the host's single ``node2_bridge.env.bat`` (which was hard-pinned to ``MT5_ACCOUNT_ID=25`` / account 25's
terminal on the fixed port 8789). Every value comes from the authoritative ``HostedExecutionEndpoint`` (itself
server-derived); NOTHING here is client-supplied. Output is ASCII-only so Windows PowerShell/CMD parse it
identically under any encoding (RULE 9 corollary). This module renders text only — it starts no process and
places no order; supervised launch is a separately-gated host concern.
"""
from __future__ import annotations


def render_bridge_env(endpoint, *, allow_live: bool = False, live_expiry_epoch: int | None = None,
                      readonly_positions: bool = False) -> str:
    """Return the ``.bat`` env body for ``endpoint``'s dedicated bridge. Parameterised per tenant: its OWN
    account id, terminal path, port, and the full server-derived identity the bridge enforces every order
    against (login/server/windows_username/workspace_uuid).

    D4b — ``MT5_ALLOW_LIVE`` is PER-RUNTIME, never global. It is emitted as ``1`` ONLY when the caller passes
    ``allow_live=True`` (which it derives from ``allow_live_for_account`` — flag + policy-LIVE + a valid §3
    authorization + identity); the default (and every demo runtime) OMITS it entirely, byte-identical to the
    pre-D4 posture. Setting it is NEVER sufficient by itself: the bridge still enforces the mandatory identity
    pin + ``evaluate_binding`` at order time. ``MT5_EXPECTED_IS_DEMO`` continues to reflect the runtime's own
    environment, so a live runtime is 0 and a demo runtime is 1.

    ADR-0049.1 (host-side time-bound) — a LIVE env is ALWAYS time-bounded on the host. When ``allow_live`` is
    emitted, ``MT5_LIVE_EXPIRY`` is emitted ALONGSIDE it: ``live_expiry_epoch`` is a positive integer **UTC Unix
    epoch in SECONDS** after which the runtime bridge refuses every LIVE order (even if the OFF restore never
    runs). ``allow_live=True`` therefore REQUIRES a positive-int ``live_expiry_epoch`` — emitting
    ``MT5_ALLOW_LIVE`` without an expiry is a caller error and is refused here; the bridge independently
    fail-closes on an absent/malformed/past expiry. The OFF render omits BOTH keys.

    GFX-LIVE-POSITIONS-READONLY-001 (ADR-0072) — ``readonly_positions`` emits ``MT5_LIVE_READONLY_POSITIONS_ENABLED``
    (a READ-ONLY monitoring flag) ONLY when the caller passes ``readonly_positions=True`` (derived from the
    fail-closed, execution-independent ``readonly_positions_for_account``). It is INDEPENDENT of ``allow_live``:
    a monitoring-only LIVE runtime may carry it with NO execution authority, and a live-execution runtime gains
    NO read authority from ``MT5_ALLOW_LIVE``. Default OMITTED => the read capability is OFF (and cleared on
    restart, as it lives in the env file, not process state). The bridge consults it in exactly one read path
    (``fetch_positions``) and never in any order/close/modify path."""
    if allow_live and not (isinstance(live_expiry_epoch, int) and not isinstance(live_expiry_epoch, bool)
                           and live_expiry_epoch > 0):
        raise ValueError("render_bridge_env(allow_live=True) requires a positive integer live_expiry_epoch "
                         "(MT5_LIVE_EXPIRY, UTC seconds): a LIVE boundary must be time-bounded on the host")
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
        # ADR-0049.1 — the host-side time bound, emitted in lockstep with MT5_ALLOW_LIVE (validated above to be a
        # positive int). The bridge admits a LIVE order ONLY while now < MT5_LIVE_EXPIRY, so the LIVE boundary
        # self-expires on the host independently of the backend authorization and the OFF restore.
        lines.append(_kv("MT5_LIVE_EXPIRY", int(live_expiry_epoch)))
    if readonly_positions:
        # GFX-LIVE-POSITIONS-READONLY-001 (ADR-0072) — READ-ONLY monitoring flag, emitted ONLY when the caller's
        # readonly_positions_for_account() held. INDEPENDENT of MT5_ALLOW_LIVE (emitted with or without it); the
        # bridge consults it in exactly one read path (fetch_positions) and in NO order/close/modify path.
        lines.append(_kv("MT5_LIVE_READONLY_POSITIONS_ENABLED", "1"))
    return "\r\n".join(lines) + "\r\n"


def allow_live_for_account(account) -> bool:
    """D4b — the fail-closed per-runtime decision for whether ``MT5_ALLOW_LIVE`` may be set for ``account``'s
    bridge: the account is authoritatively LIVE (``is_live_environment``) AND holds a valid §3 authorization
    under the armed flag (``live_execution_permitted``). A DEMO account, an un-authorized LIVE account, a
    demo/live mismatch, or any error ⇒ False (MT5_ALLOW_LIVE omitted). Never a global switch — it is evaluated
    per account; the bridge's identity pin + order-time gate remain the final authority."""
    try:
        from trading.account_policy import is_live_environment
        from execution.live_authz import copier_live_execution_authorized, live_execution_permitted
        if not is_live_environment(account):
            return False
        # Either the strategy §3 authorization OR the ADR-0057 per-pair copier authorization independently
        # authorizes this LIVE runtime. The strategy path is unchanged; the copier path is additive and equally
        # fail-closed. Whichever set the flag, the order-time identity pin + evaluate_binding remain the final
        # authority, and the copier itself only ever creates a LIVE job after its full per-pair admission.
        return bool(live_execution_permitted(account) or copier_live_execution_authorized(account))
    except Exception:  # noqa: BLE001 — fail closed
        return False


def readonly_positions_for_account(account) -> bool:
    """GFX-LIVE-POSITIONS-READONLY-001 (ADR-0072) — fail-closed per-tenant decision for whether a bridge may be
    rendered with ``MT5_LIVE_READONLY_POSITIONS_ENABLED``. TRUE only when BOTH hold:

      * the account is authoritatively LIVE (``is_live_environment`` — a DEMO account never needs this flag; it
        reads positions unconditionally), AND
      * the dedicated, default-OFF monitoring authority is armed
        (``LIVE_READONLY_POSITIONS_MONITORING_ENABLED``, settings- or env-sourced).

    This authority is SEPARATE from every execution gate (``MT5_ALLOW_LIVE`` / ``live_execution_permitted`` /
    ``copier_live_execution_authorized`` / ``LiveExecutionAuthorization`` / ``HOSTED_LIVE_EXECUTION_ENABLED``):
    arming it can never place, size or approve an order, and it is never consulted by the order path. Any error
    => False. The emitted flag is a READ gate only; the bridge's own ``fetch_positions`` enforcement is the
    backend-independent final authority."""
    try:
        from trading.account_policy import is_live_environment
        if not is_live_environment(account):
            return False
        return _live_readonly_positions_monitoring_armed()
    except Exception:  # noqa: BLE001 — fail closed
        return False


def _live_readonly_positions_monitoring_armed() -> bool:
    """Default-OFF monitoring authority flag, independent of every execution flag. Settings override, else env
    (mirrors ``hosted_workspace.flags._flag``): absent/blank/falsey => OFF."""
    import os
    val = None
    try:
        from django.conf import settings
        val = getattr(settings, "LIVE_READONLY_POSITIONS_MONITORING_ENABLED", None)
    except Exception:  # noqa: BLE001 — settings unavailable => fall back to env
        val = None
    if val is None:
        val = os.getenv("LIVE_READONLY_POSITIONS_MONITORING_ENABLED", "")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _kv(key: str, value) -> str:
    v = "" if value is None else str(value)
    # Defence: the env is a batch file; reject any control/newline injection (all values are server-derived,
    # so this should never trigger — it is a belt-and-braces guard, not input validation of customer data).
    if any(ord(ch) < 32 for ch in v):
        raise ValueError(f"illegal control character in bridge env value for {key}")
    return f"set {key}={v}"
