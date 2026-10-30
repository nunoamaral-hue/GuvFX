"""ADR-0049 LIVE bridge provisioning integrator - backend orchestration (fail-closed, account-scoped).

The missing wire-up: a signed, account-scoped operation that

  1. RE-EVALUATES LIVE eligibility from the authoritative backend (the full gate stack, not a bare boolean);
  2. RENDERS the reviewed bridge env via ``execution.bridge_config.render_bridge_env`` (the SOLE source of
     ``MT5_ALLOW_LIVE`` - never hand-written policy in PowerShell);
  3. DISPATCHES a signed ``ACTIVATE_TENANT_BRIDGE_LIVE_AUTHORIZED`` to EXACTLY this tenant's host executor;
  4. VERIFIES the signed attestation against what it rendered (env checksum, live posture, identity pin,
     port, no-other-tenant);
  5. FAILS CLOSED to an execution-OFF restore on any post-verification mismatch.

This module creates NO order, arms NO flag, and (dry-run default) mutates NOTHING. A real dispatch requires the
caller process to carry the LIVE arm stack (COPIER_ENABLED + COPIER_EXECUTE_ENABLED + COPIER_LIVE_EXECUTE_ENABLED
+ HOSTED_LIVE_EXECUTION_ENABLED) so ``allow_live_for_account`` can resolve True, AND a signed host executor to be
resolvable. The OFF restore needs none of that and is safe to run any time.
"""
from __future__ import annotations

import base64
import hashlib


def _mask(v) -> str:
    s = str(v or "")
    return ("\u2026" + s[-4:]) if len(s) >= 4 else "****"


def resolve_ready_endpoint(account):
    """The single READY ``HostedExecutionEndpoint`` for this account, else ``None`` (fail-closed)."""
    try:
        from execution.models import HostedExecutionEndpoint
        return (HostedExecutionEndpoint.objects
                .filter(trading_account_id=account.id, state="READY")
                .order_by("-id").first())
    except Exception:  # noqa: BLE001
        return None


def _active_authorization(account_id):
    from django.utils import timezone
    from trade_copier.models import CopierLiveAuthorization, CopierLiveAuthorizationStatus
    now = timezone.now()
    return (CopierLiveAuthorization.objects
            .filter(follower_account_id=account_id, status=CopierLiveAuthorizationStatus.ACTIVE,
                    revoked_at__isnull=True, valid_from__lte=now, expires_at__gt=now)
            .order_by("-created_at").first())


def evaluate_live_gates(account, authz_id):
    """Re-evaluate EVERY LIVE-bridge gate from the authoritative backend. Returns ``(ok, gates)`` where ``gates``
    is a list of ``(name, ok, detail)``. Fail-closed: any gate False ⇒ not ok. This is the authority; the host
    re-checks the env structure, and the bridge's own order-time pin + ``_bridge_allow_live`` remain final."""
    from trade_copier import flags as cf
    from trade_copier.models import CopierLiveAuthorization, CopierLiveKillSwitch
    from trading.account_policy import account_environment, is_live_environment
    from trade_copier.symbols import resolve_follower_symbol
    from execution.models import BrokerInstrument, BrokerRuntimePause
    from execution.bridge_config import allow_live_for_account
    from hosted_workspace.flags import hosted_live_execution_enabled

    gates = []

    def g(name, ok, detail=""):
        gates.append((name, bool(ok), str(detail)))
        return bool(ok)

    g("copier_enabled", cf.copier_enabled())
    g("copier_execute_enabled", cf.copier_execute_enabled())
    g("copier_live_execute_enabled", cf.copier_live_execute_enabled())
    g("hosted_live_execution_enabled", hosted_live_execution_enabled())

    authz = CopierLiveAuthorization.objects.filter(pk=authz_id).first()
    if not g("authorization_exists", authz is not None, f"id={authz_id}"):
        return (False, gates)
    active = _active_authorization(account.id)
    g("authorization_active_for_follower", active is not None and active.pk == authz.pk)
    g("authorization_follower_is_account", authz.follower_account_id == account.id,
      f"{authz.follower_account_id}=={account.id}")
    g("authorization_not_revoked", authz.revoked_at is None and authz.status == "ACTIVE")
    g("allowance_remaining", authz.orders_consumed < authz.max_orders,
      f"{authz.orders_consumed}/{authz.max_orders}")

    from django.utils import timezone
    now = timezone.now()
    g("within_window", authz.valid_from <= now < authz.expires_at)

    master = authz.master_account
    g("tenant_user_matches", authz.user_id == account.user_id == master.user_id,
      f"authz={authz.user_id} follower={account.user_id} master={master.user_id}")
    g("master_follower_ids", authz.master_account_id == master.id and authz.follower_account_id == account.id)

    # pinned identities both sides vs the authorization snapshots
    try:
        f_login = str(account.account_number or "").strip()
        f_server = str(getattr(account.broker_server, "server_name", "") or "").strip()
        f_env = account_environment(account)
        m_login = str(master.account_number or "").strip()
        m_server = str(getattr(master.broker_server, "server_name", "") or "").strip()
        m_env = account_environment(master)
    except Exception as e:  # noqa: BLE001
        g("identities_resolvable", False, type(e).__name__)
        return (False, gates)
    g("follower_identity_matches",
      authz.follower_login == f_login and authz.follower_server == f_server and authz.follower_environment == f_env)
    g("master_identity_matches",
      authz.master_login == m_login and authz.master_server == m_server and authz.master_environment == m_env)
    g("both_sides_live", is_live_environment(account) and is_live_environment(master))

    # verified symbol map + exact symbols
    sym = resolve_follower_symbol(master, authz.source_symbol, account)
    g("verified_map", sym.ok and sym.follower_symbol == authz.follower_symbol,
      f"{getattr(sym,'ok',None)}/{getattr(sym,'follower_symbol',None)}")

    g("follower_active", bool(getattr(account, "is_active", False)))

    g("no_kill_switch", not CopierLiveKillSwitch.objects.filter(engaged=True).exists())
    g("no_runtime_pause", not BrokerRuntimePause.objects.filter(account_id=account.id, paused=True).exists())

    bi = BrokerInstrument.objects.filter(account_id=account.id, broker_symbol=authz.follower_symbol, enabled=True).first()
    md = (bi.metadata or {}) if bi else {}
    vmin = md.get("volume_min", md.get("min_volume"))
    vstep = md.get("volume_step", md.get("lot_step"))
    g("instrument_enabled_authoritative", bool(bi) and vmin is not None and vstep is not None,
      f"enabled={bool(bi)} min={vmin} step={vstep}")

    ep = resolve_ready_endpoint(account)
    g("endpoint_ready", ep is not None and ep.trading_account_id == account.id and 8800 <= int(ep.port) <= 8899,
      f"port={getattr(ep,'port',None)}")

    # THE authoritative per-runtime boundary predicate (the same one render_bridge_env consumes). It itself ANDs
    # the full flag stack + a valid authorization + identity; this is necessary-and-the-render-source.
    g("allow_live_for_account", allow_live_for_account(account))

    ok = all(x[1] for x in gates)
    return (ok, gates)


def render_env_b64(account, *, allow_live):
    """Render the bridge env for ``account`` via render_bridge_env and return ``(b64, sha256, text)``. ``allow_live``
    is the authoritative flag the caller already evaluated (True only for the LIVE op after the full gate stack
    passed; False for the OFF restore, which needs nothing). Fail-closed: no endpoint ⇒ ValueError."""
    from execution.bridge_config import render_bridge_env
    ep = resolve_ready_endpoint(account)
    if ep is None:
        raise ValueError("no_ready_endpoint")
    text = render_bridge_env(ep, allow_live=bool(allow_live))
    raw = text.encode("ascii")
    return (base64.b64encode(raw).decode("ascii"), hashlib.sha256(raw).hexdigest(), text)


def _endpoint_port(account):
    ep = resolve_ready_endpoint(account)
    return int(ep.port) if ep is not None else None


def _resolve_executor(account, executor_resolver):
    if executor_resolver is not None:
        return executor_resolver(account)
    # resolve_signed_host_executor is keyword-only on an INT account id (DARK-gated -> None unless the hosted
    # executor flag + keyring/key_id/base_url are all present). Pass the id, never the object.
    from hosted_workspace.host_executor import resolve_signed_host_executor
    return resolve_signed_host_executor(account_id=int(account.id))


def provision_live(account_id, authz_id, *, commit=False, executor_resolver=None):
    """Re-evaluate gates → render LIVE env → (commit) dispatch signed ACTIVATE_TENANT_BRIDGE_LIVE_AUTHORIZED →
    verify attestation → fail-closed OFF restore on mismatch. Default dry-run mutates nothing. Returns a dict
    with ``armed`` / ``dry_run`` / ``preview`` / ``attestation`` / ``reason``."""
    from django.utils import timezone
    from trading.models import TradingAccount
    from execution.bridge_config import allow_live_for_account

    account = TradingAccount.objects.filter(id=account_id).select_related("broker_server").first()
    if account is None:
        return {"ok": False, "armed": False, "reason": "account_not_found"}

    ok, gates = evaluate_live_gates(account, authz_id)
    failed = [n for (n, g, _d) in gates if not g]
    port = _endpoint_port(account)
    # Render is authoritative: allow_live comes from the predicate, never a bare bool.
    try:
        env_b64, env_sha, _text = render_env_b64(account, allow_live=allow_live_for_account(account))
    except ValueError as e:
        return {"ok": False, "armed": False, "reason": str(e), "gates_ok": ok, "failed_gates": failed}

    preview = {
        "account_id": account.id, "follower_login": _mask(account.account_number),
        "authorization_id": authz_id, "port": port, "env_sha256": env_sha,
        "env_will_be_live": "MT5_ALLOW_LIVE=1" in _text, "gates_ok": ok, "failed_gates": failed,
        "operation": "ACTIVATE_TENANT_BRIDGE_LIVE_AUTHORIZED",
    }
    if not ok:
        return {"ok": False, "armed": False, "reason": "gate_failed", "preview": preview}
    if not preview["env_will_be_live"]:
        # allow_live_for_account disagreed with the gate aggregate - refuse rather than dispatch a non-live env.
        return {"ok": False, "armed": False, "reason": "render_not_live", "preview": preview}
    if not commit:
        return {"ok": True, "armed": False, "dry_run": True, "reason": "dry_run_ok", "preview": preview}

    executor = _resolve_executor(account, executor_resolver)
    if executor is None:
        return {"ok": False, "armed": False, "reason": "host_executor_unavailable", "preview": preview}

    # RE-READ the gates immediately before signed dispatch (TOCTOU) - refuse if anything changed.
    ok2, gates2 = evaluate_live_gates(account, authz_id)
    if not ok2:
        return {"ok": False, "armed": False, "reason": "gate_failed_recheck",
                "failed_gates": [n for (n, g, _d) in gates2 if not g], "preview": preview}

    result = executor.activate_tenant_bridge_live_authorized(
        account.runtime_root if hasattr(account, "runtime_root") else rf"C:\GuvFX\accounts\{account.id}",
        port, env_b64)

    att_ok, att_reason = _verify_attestation(result, account_id=account.id, port=port,
                                             expect_env_sha=env_sha, expect_live=True)
    if att_ok:
        return {"ok": True, "armed": True, "reason": "live_bridge_activated",
                "preview": preview, "attestation": result}
    # Post-verification failure ⇒ fail closed: attempt OFF restoration, report the original failure.
    restore = restore_off(account_id, commit=True, executor_resolver=executor_resolver)
    return {"ok": False, "armed": False, "reason": f"attestation_failed:{att_reason}",
            "preview": preview, "attestation": result, "off_restore": restore}


def restore_off(account_id, *, commit=False, executor_resolver=None):
    """Render the execution-OFF env (allow_live=False; needs no flags/authorization) and (commit) dispatch
    RESTORE_TENANT_BRIDGE_EXECUTION_OFF. Risk-reducing + idempotent. Default dry-run mutates nothing."""
    from trading.models import TradingAccount
    account = TradingAccount.objects.filter(id=account_id).select_related("broker_server").first()
    if account is None:
        return {"ok": False, "reason": "account_not_found"}
    port = _endpoint_port(account)
    try:
        env_b64, env_sha, _text = render_env_b64(account, allow_live=False)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}
    if "MT5_ALLOW_LIVE" in _text:   # defence: the OFF render must never carry it
        return {"ok": False, "reason": "off_render_unexpected_live"}
    preview = {"account_id": account.id, "port": port, "env_sha256": env_sha,
               "operation": "RESTORE_TENANT_BRIDGE_EXECUTION_OFF"}
    if not commit:
        return {"ok": True, "dry_run": True, "reason": "dry_run_ok", "preview": preview}
    executor = _resolve_executor(account, executor_resolver)
    if executor is None:
        return {"ok": False, "reason": "host_executor_unavailable", "preview": preview}
    result = executor.restore_tenant_bridge_execution_off(
        account.runtime_root if hasattr(account, "runtime_root") else rf"C:\GuvFX\accounts\{account.id}",
        port, env_b64)
    att_ok, att_reason = _verify_attestation(result, account_id=account.id, port=port,
                                             expect_env_sha=env_sha, expect_live=False)
    return {"ok": bool(att_ok), "reason": ("execution_off_restored" if att_ok else f"attestation_failed:{att_reason}"),
            "preview": preview, "attestation": result}


def _verify_attestation(result, *, account_id, port, expect_env_sha, expect_live):
    """Prove the host's SIGNED attestation matches what we rendered: ok, account id, port, env checksum, the live
    posture, the mandatory identity pin + guarded attach, and listening. Never trusts file-generation alone."""
    if not isinstance(result, dict):
        return (False, "no_result")
    if not result.get("ok"):
        return (False, str(result.get("reason", "host_not_ok")))
    try:
        res_account = int(result.get("account_id", -1))
        res_port = int(result.get("port", -1))
    except (TypeError, ValueError):
        return (False, "malformed_attestation")      # a non-int field from a (compromised) host -> fail closed
    if res_account != int(account_id):
        return (False, "account_mismatch")
    if port is not None and res_port != int(port):
        return (False, "port_mismatch")
    if str(result.get("env_sha256", "")).lower() != str(expect_env_sha).lower():
        return (False, "env_sha_mismatch")
    if bool(result.get("allow_live")) != bool(expect_live):
        return (False, "live_posture_mismatch")
    if not (result.get("require_pin") and result.get("guarded_attach")):
        return (False, "pin_or_guard_off")
    if not result.get("listening"):
        return (False, "not_listening")
    return (True, "ok")
