"""monitoring_reconcile (GFX-DASHBOARD-LIVE-POSITIONS-PROVISIONING-WIRING-137, ADR-0111) — the dedicated,
MONITORING-ONLY read-only-positions bridge reconciler.

It turns the merged ADR-0110 account-level allow-list DECISION (``execution.bridge_config.readonly_positions_for_account``)
into a safe, account-scoped PROVISIONING operation. For an explicitly-named, owner-authorized, identity-pinned,
allow-listed, LIVE, persistent-workspace account it:

  1. selects ONLY that owner's persistent-workspace account(s) (never a global sweep);
  2. calls ``readonly_positions_for_account(account)`` per account (the sole arming authority);
  3. renders the execution-OFF bridge env WITH ``MT5_LIVE_READONLY_POSITIONS_ENABLED=1`` — and ONLY when the account
     is explicitly allow-listed AND identity-verified;
  4. proves the rendered env differs from the captured baseline by EXACTLY the one read-only line (no other delta);
  5. (only with ``commit``) applies it via the SIGNED ``RESTORE_TENANT_BRIDGE_EXECUTION_OFF`` transport — which
     narrowly restarts that ONE tenant's bridge and attests ``MT5_ALLOW_LIVE`` is OFF — with byte-exact rollback
     capture, post-apply read-back, idempotency and per-account truthful reporting.

SAFETY INVARIANTS (enforced here AND re-enforced host-side by the RESTORE validator):
  * NEVER renders, sets, infers or carries ``MT5_ALLOW_LIVE`` / ``MT5_LIVE_EXPIRY`` — the render is always
    ``allow_live=False`` and is asserted free of both keys.
  * NEVER invokes an execution/admission/arm operation; it only ever dispatches RESTORE (execution-OFF transport).
  * The ONLY env delta it may introduce is the single ``MT5_LIVE_READONLY_POSITIONS_ENABLED=1`` line (teardown
    removes exactly that line). Any other delta fails closed.
  * Default DRY-RUN mutates nothing. Fail-closed on an empty/malformed allow-list, an unapproved / cross-owner /
    identity-mismatched account, an unsupported workspace, a missing endpoint, or a demo account.
  * Copy-Trading / follower execution is untouched: enabling an account's read (incl. #48) can neither arm nor
    admit execution — the reconciler has no code path that does, and it refuses to act on an account that currently
    holds an active live-execution authorization or arm.

This module creates NO order and arms NO execution flag. The real host transport is an injected, operator-run
adapter (``_RealMonitoringOps``) that is never exercised in CI; tests drive a fake ``MonitoringReconcileOps``.
"""
from __future__ import annotations

import abc
import base64
import hashlib

_READONLY_KEY = "MT5_LIVE_READONLY_POSITIONS_ENABLED"
_FORBIDDEN_EXECUTION_KEYS = ("MT5_ALLOW_LIVE", "MT5_LIVE_EXPIRY")

# Authoritative established, monitoring-capable lifecycle states (GFX-141): HostedMt5Workspace.canonical_state
# (WorkspaceLifecycleState, ADR-0034 §3 — the nine canonical states every subsystem consumes). A reconcile is
# meaningful only when the per-tenant terminal is broker-CONNECTED (optionally EXECUTION_READY — still broker
# connected; the reconciler always renders allow_live=False, so execution stays OFF). EVERY other canonical state
# FAILS CLOSED: PROVISIONING / WAITING_FOR_LOGIN (transitional / login-required), EXECUTING (active live execution —
# refused HERE directly, so a canonical-EXECUTING runtime is rejected even with no authorization row; the separate
# execution_off gate additionally refuses any account holding an active/consumed authorization or engaged arm),
# DISCONNECTED / RECOVERING / SUSPENDED / RETIRED, and any missing / empty / unknown value. The legacy ``state`` (WorkspaceState) field is DELIBERATELY IGNORED for eligibility — persistent-
# workspace accounts leave it at a stale NOT_PROVISIONED — and ``proj_connected`` is NEVER the authority either (a
# True projection cannot override an unsafe canonical state).
_SUPPORTED_CANONICAL_STATES = frozenset({"CONNECTED", "EXECUTION_READY"})


def _mask(v) -> str:
    s = str(v or "")
    return ("\u2026" + s[-4:]) if len(s) >= 4 else "****"


def resolve_ready_endpoint(account):
    """The single READY ``HostedExecutionEndpoint`` for this account, else ``None`` (fail-closed). Local to this
    module so the hosted-workspace reconciler takes no dependency on the copier app."""
    try:
        from execution.models import HostedExecutionEndpoint
        return (HostedExecutionEndpoint.objects
                .filter(trading_account_id=account.id, state="READY")
                .order_by("-id").first())
    except Exception:  # noqa: BLE001 — fail closed
        return None


def _runtime_root(account) -> str:
    rr = getattr(account, "runtime_root", None)
    return str(rr) if rr else rf"C:\GuvFX\accounts\{account.id}"


def render_readonly_env(account, *, readonly: bool):
    """Render the execution-OFF bridge env for ``account`` with the read-only flag present (``readonly=True``) or
    absent (``readonly=False`` — the byte-exact baseline). Returns ``(b64, sha256, text)``.

    Always ``allow_live=False``: the render can therefore NEVER carry ``MT5_ALLOW_LIVE`` / ``MT5_LIVE_EXPIRY`` — a
    belt-and-braces assertion refuses if either appears. Fail-closed: no READY endpoint ⇒ ValueError."""
    from execution.bridge_config import render_bridge_env
    ep = resolve_ready_endpoint(account)
    if ep is None:
        raise ValueError("no_ready_endpoint")
    text = render_bridge_env(ep, allow_live=False, readonly_positions=bool(readonly))
    for k in _FORBIDDEN_EXECUTION_KEYS:
        if k in text:                                  # an OFF render must NEVER carry an execution key
            raise ValueError("off_render_unexpected_live")
    if readonly and f"set {_READONLY_KEY}=1" not in text:
        raise ValueError("readonly_flag_not_rendered")
    if (not readonly) and _READONLY_KEY in text:
        raise ValueError("baseline_carries_readonly")
    raw = text.encode("ascii")
    return (base64.b64encode(raw).decode("ascii"), hashlib.sha256(raw).hexdigest(), text)


def _env_delta_is_only_readonly(baseline_text: str, monitoring_text: str) -> bool:
    """True iff ``monitoring_text`` is EXACTLY ``baseline_text`` plus the single ``set <READONLY_KEY>=1`` line and
    nothing else (order-independent line-set comparison). This is the authoritative "no delta beyond the read-only
    flag" guard (contract point 8): if the monitoring render differs from the baseline in ANY other line — e.g. a
    smuggled ``MT5_ALLOW_LIVE`` — the reconcile fails closed."""
    def _lines(t):
        return [ln for ln in t.replace("\r\n", "\n").split("\n") if ln.strip() != ""]
    base = _lines(baseline_text)
    mon = _lines(monitoring_text)
    added = [ln for ln in mon if ln not in base]
    removed = [ln for ln in base if ln not in mon]
    return removed == [] and added == [f"set {_READONLY_KEY}=1"]


def _account_has_active_execution(account_id) -> bool:
    """Defence: True iff the account currently holds an ACTIVE live-execution authorization or an engaged arm, so the
    reconciler can REFUSE to touch a bridge mid-execution (a monitoring reconcile must never disturb, nor be confused
    with, an execution runtime). Any error ⇒ True (fail closed: refuse rather than risk acting)."""
    try:
        from execution.models import LiveExecutionAuthorization
        # Canonical active-authorization predicate (mirrors execution.live_authz): is_active + not revoked. There is
        # no expires_at on this model — a partial-unique constraint keeps at most one active row per account instance.
        if LiveExecutionAuthorization.objects.filter(
                trading_account_id=account_id, is_active=True, revoked_at__isnull=True).exists():
            return True
    except Exception:  # noqa: BLE001 — the primary execution check fails CLOSED (refuse rather than risk acting)
        return True
    try:
        from django.utils import timezone
        from trade_copier.models import (CopierLiveAuthorization, CopierLiveAuthorizationStatus,
                                         CopierLiveArm)
        now = timezone.now()
        # ACTIVE **or CONSUMED**: a CONSUMED authorization still manages an already-bound live position through
        # MODIFY/CLOSE (mirrors execution.bridge_config._account_is_authorized_live_follower), so the follower is
        # still mid-execution and must not be monitoring-reconciled.
        if CopierLiveAuthorization.objects.filter(
                follower_account_id=account_id,
                status__in=(CopierLiveAuthorizationStatus.ACTIVE, CopierLiveAuthorizationStatus.CONSUMED),
                revoked_at__isnull=True, expires_at__gt=now).exists():
            return True
        if CopierLiveArm.objects.filter(
                follower_account_id=account_id, disengaged_at__isnull=True, expires_at__gt=now).exists():
            return True
    except Exception:  # noqa: BLE001 — fail CLOSED (refuse), symmetric with the primary check above
        return True
    return False


def evaluate_monitoring_gates(account, *, expected_owner_id, expected_login=None, expected_workspace_uuid=None):
    """Fail-closed validation for a MONITORING read-only reconcile. Returns ``(ok, gates)`` where ``gates`` is a list
    of ``(name, ok, detail)``. ANY gate False ⇒ not ok (the reconcile refuses). Never raises on bad data — a lookup
    error becomes a failed gate."""
    from trading.account_policy import is_live_environment
    from execution.bridge_config import readonly_positions_for_account
    from execution.readiness import PERSISTENT_WORKSPACE

    gates = []

    def g(name, ok, detail=""):
        gates.append((name, bool(ok), str(detail)))
        return bool(ok)

    # 1) owner isolation — the account must belong to the expected owner (never cross-owner).
    if not g("owner_matches", getattr(account, "user_id", None) == int(expected_owner_id),
             f"acct_owner={getattr(account, 'user_id', None)} expected={expected_owner_id}"):
        return (False, gates)
    # 2) not tombstoned
    g("not_disconnected", getattr(account, "disconnected_at", None) is None)
    # 3) persistent-workspace readiness provider only
    g("persistent_workspace",
      str(getattr(account, "readiness_provider", "")) == PERSISTENT_WORKSPACE,
      str(getattr(account, "readiness_provider", "")))
    # 4) a supported, established HostedMt5Workspace exists
    try:
        from hosted_workspace.models import HostedMt5Workspace
        ws = HostedMt5Workspace.objects.filter(trading_account_id=account.id).first()
    except Exception as e:  # noqa: BLE001
        ws = None
        g("workspace_lookup", False, type(e).__name__)
    # GFX-141: decide eligibility from the AUTHORITATIVE canonical_state (WorkspaceLifecycleState), never the stale
    # legacy ``state`` field and never ``proj_connected``. Fail closed on a missing/empty/unknown/transitional/
    # login-required/disconnected/recovering/suspended/retired/executing canonical state.
    canonical = str(getattr(ws, "canonical_state", "")) if ws is not None else ""
    # EXACT match against the canonical enum values (always upper-case) — a case/format variation is malformed and
    # fails closed, never normalised into eligibility.
    g("workspace_supported", ws is not None and canonical in _SUPPORTED_CANONICAL_STATES, canonical)
    # 5) LIVE only — a DEMO account reads positions unconditionally and must never be touched by this op.
    g("is_live", is_live_environment(account))
    # 6) explicitly allow-listed (the sole arming authority; empty/malformed allow-list ⇒ False ⇒ refuse)
    g("allowlisted", readonly_positions_for_account(account))
    # 7) a READY endpoint, port in the tenant band, bound to THIS account
    ep = resolve_ready_endpoint(account)
    g("endpoint_ready",
      ep is not None and ep.trading_account_id == account.id and 8800 <= int(getattr(ep, "port", 0) or 0) <= 8899,
      f"port={getattr(ep, 'port', None)}")
    # 8) identity pin: the endpoint's expected identity must agree with the account AND any operator-supplied pin
    if ep is not None:
        try:
            acct_login = str(getattr(account, "account_number", "") or "").strip()
            acct_server = str(getattr(getattr(account, "broker_server", None), "server_name", "") or "").strip()
        except Exception:  # noqa: BLE001
            acct_login, acct_server = "", ""
        ep_login = str(getattr(ep, "expected_login", "") or "").strip()
        ep_server = str(getattr(ep, "expected_server", "") or "").strip()
        g("endpoint_is_live", not bool(getattr(ep, "is_demo", True)))
        g("identity_login_matches", ep_login != "" and ep_login == acct_login, f"{_mask(ep_login)}=={_mask(acct_login)}")
        g("identity_server_matches", ep_server != "" and ep_server == acct_server)
        if expected_login is not None:
            g("operator_login_pin", str(expected_login).strip() == acct_login and str(expected_login).strip() == ep_login)
        if expected_workspace_uuid is not None:
            g("operator_workspace_pin", str(getattr(ep, "workspace_uuid", "")) == str(expected_workspace_uuid))
    # 9) execution must be OFF for this account — never reconcile monitoring on a mid-execution runtime
    g("execution_off", not _account_has_active_execution(account.id))

    ok = all(x[1] for x in gates)
    return (ok, gates)


class MonitoringReconcileOps(abc.ABC):
    """Injected host transport. The real adapter signs + dispatches RESTORE and reads positions back from the bridge;
    the test fake records calls and returns synthetic attestations/read-backs. No host is contacted in CI."""

    @abc.abstractmethod
    def apply_off_env(self, account, *, env_b64, env_sha, port, runtime_root, expect_readonly):
        """Dispatch the signed execution-OFF env to THIS tenant's bridge and return ``(ok, attestation, reason)``.
        ``expect_readonly`` records whether the applied env carries the read flag (the baseline apply is False)."""

    @abc.abstractmethod
    def read_back_positions(self, account):
        """Return ``{"http_status": int|None, "ok": bool, "reason": str}`` proving the bridge now serves (or still
        refuses) ``/mt5/positions`` — a READ only, never an order. Synthetic in tests."""


def _verify_off_attestation(result, *, account_id, port, expect_env_sha):
    """Prove the host's SIGNED attestation matches what we applied, for the execution-OFF posture (expect_live=False
    always): ok, account id, port, env checksum, ``allow_live`` FALSE, identity pin + guarded attach, listening. A
    LIVE posture in the attestation is a hard failure (the monitoring op must never produce a live bridge)."""
    if not isinstance(result, dict):
        return (False, "no_result")
    if not result.get("ok"):
        return (False, str(result.get("reason", "host_not_ok")))
    try:
        if int(result.get("account_id", -1)) != int(account_id):
            return (False, "account_mismatch")
        if port is not None and int(result.get("port", -1)) != int(port):
            return (False, "port_mismatch")
    except (TypeError, ValueError):
        return (False, "malformed_attestation")
    if str(result.get("env_sha256", "")).lower() != str(expect_env_sha).lower():
        return (False, "env_sha_mismatch")
    if bool(result.get("allow_live")):
        return (False, "unexpected_live_posture")        # the OFF/monitoring apply must attest allow_live FALSE
    if not (result.get("require_pin") and result.get("guarded_attach")):
        return (False, "pin_or_guard_off")
    if not result.get("listening"):
        return (False, "not_listening")
    return (True, "ok")


def reconcile_account(account_id, *, commit=False, expected_owner_id, expected_login=None,
                      expected_workspace_uuid=None, ops=None, teardown=False):
    """Reconcile ONE account's read-only monitoring flag. Default dry-run. Returns a per-account result dict with
    ``ok`` / ``applied`` / ``rolled_back`` / ``dry_run`` / ``reason`` / ``preview`` / ``read_back``.

    teardown=False: ensure the read-only flag is PRESENT (render monitoring env, baseline-diff guard, apply).
    teardown=True:  ensure the read-only flag is ABSENT (render the baseline env, apply) — removes ONLY the flag.
    On a post-apply attestation/read-back failure the account is rolled back to its captured byte-exact baseline."""
    from trading.models import TradingAccount
    account = (TradingAccount.objects.filter(id=account_id)
               .select_related("broker_server").first())
    if account is None:
        return {"ok": False, "account_id": account_id, "reason": "account_not_found"}

    ok, gates = evaluate_monitoring_gates(account, expected_owner_id=expected_owner_id,
                                          expected_login=expected_login,
                                          expected_workspace_uuid=expected_workspace_uuid)
    failed = [n for (n, g, _d) in gates if not g]
    op = "TEARDOWN_READONLY" if teardown else "ENABLE_READONLY"
    if not ok:
        return {"ok": False, "account_id": account.id, "reason": "gate_failed", "failed_gates": failed,
                "operation": op, "dry_run": not commit}

    # Render the byte-exact BASELINE (read-only ABSENT) and the DESIRED env for this operation.
    try:
        base_b64, base_sha, base_text = render_readonly_env(account, readonly=False)
        if teardown:
            desired_b64, desired_sha, desired_text = base_b64, base_sha, base_text
        else:
            desired_b64, desired_sha, desired_text = render_readonly_env(account, readonly=True)
            # Authoritative "no delta beyond the read-only flag" guard.
            if not _env_delta_is_only_readonly(base_text, desired_text):
                return {"ok": False, "account_id": account.id, "reason": "env_delta_unexpected",
                        "operation": op, "dry_run": not commit}
    except ValueError as e:
        return {"ok": False, "account_id": account.id, "reason": str(e), "operation": op, "dry_run": not commit}

    port = None
    ep = resolve_ready_endpoint(account)
    if ep is not None:
        port = int(ep.port)
    preview = {
        "account_id": account.id, "login": _mask(account.account_number), "port": port,
        "operation": op, "transport": "RESTORE_TENANT_BRIDGE_EXECUTION_OFF",
        "baseline_env_sha256": base_sha, "desired_env_sha256": desired_sha,
        "readonly_flag_target": (not teardown),
        "env_carries_allow_live": False,                 # invariant (asserted in render_readonly_env)
    }
    if not commit:
        return {"ok": True, "account_id": account.id, "applied": False, "dry_run": True,
                "reason": "dry_run_ok", "preview": preview}

    if ops is None:
        return {"ok": False, "account_id": account.id, "reason": "ops_unavailable", "preview": preview}

    # TOCTOU: re-evaluate the gates immediately before the signed dispatch — refuse if anything changed.
    ok2, gates2 = evaluate_monitoring_gates(account, expected_owner_id=expected_owner_id,
                                            expected_login=expected_login,
                                            expected_workspace_uuid=expected_workspace_uuid)
    if not ok2:
        return {"ok": False, "account_id": account.id, "reason": "gate_failed_recheck",
                "failed_gates": [n for (n, g, _d) in gates2 if not g], "preview": preview}

    runtime_root = _runtime_root(account)
    apply_ok, attestation, apply_reason = ops.apply_off_env(
        account, env_b64=desired_b64, env_sha=desired_sha, port=port, runtime_root=runtime_root,
        expect_readonly=(not teardown))
    att_ok, att_reason = (_verify_off_attestation(attestation, account_id=account.id, port=port,
                                                  expect_env_sha=desired_sha) if apply_ok else (False, apply_reason))
    if not att_ok:
        # Fail closed: roll THIS account back to the captured byte-exact baseline (read-only absent).
        rb_ok, rb_att, rb_reason = ops.apply_off_env(
            account, env_b64=base_b64, env_sha=base_sha, port=port, runtime_root=runtime_root, expect_readonly=False)
        rb_att_ok, _ = (_verify_off_attestation(rb_att, account_id=account.id, port=port,
                                                expect_env_sha=base_sha) if rb_ok else (False, rb_reason))
        return {"ok": False, "account_id": account.id, "applied": False, "rolled_back": bool(rb_att_ok),
                "reason": f"attestation_failed:{att_reason}", "rollback_reason": (None if rb_att_ok else "rollback_failed"),
                "preview": preview, "attestation": attestation}

    # Post-apply READ-BACK (read only): prove the bridge now serves positions (enable) — informational for teardown.
    read_back = None
    try:
        read_back = ops.read_back_positions(account)
    except Exception as e:  # noqa: BLE001 — a read-back error never un-applies a verified env; report it
        read_back = {"http_status": None, "ok": False, "reason": type(e).__name__}
    if (not teardown) and not (isinstance(read_back, dict) and read_back.get("http_status") == 200):
        # The env verified but the bridge did not serve positions — roll back to baseline and report truthfully.
        rb_ok, rb_att, rb_reason = ops.apply_off_env(
            account, env_b64=base_b64, env_sha=base_sha, port=port, runtime_root=runtime_root, expect_readonly=False)
        rb_att_ok, _ = (_verify_off_attestation(rb_att, account_id=account.id, port=port,
                                                expect_env_sha=base_sha) if rb_ok else (False, rb_reason))
        return {"ok": False, "account_id": account.id, "applied": False, "rolled_back": bool(rb_att_ok),
                "reason": "read_back_failed", "rollback_reason": (None if rb_att_ok else "rollback_failed"),
                "preview": preview, "attestation": attestation, "read_back": read_back}

    return {"ok": True, "account_id": account.id, "applied": True, "rolled_back": False,
            "reason": ("readonly_removed" if teardown else "readonly_enabled"),
            "preview": preview, "attestation": attestation, "read_back": read_back}


def reconcile(account_ids, *, commit=False, expected_owner_id, expected_login=None, expected_workspace_uuid=None,
              ops=None, teardown=False, max_concurrency=1):
    """Reconcile a set of EXACT account ids (never a sweep), each one isolated: one account's failure rolls back and
    reports only that account and never aborts the others' truthful reporting. Sequential by default
    (``max_concurrency<=1``); a bounded thread pool otherwise. Returns ``{results:[...], summary:{...}}``."""
    ids = [int(a) for a in account_ids]
    if not ids:
        return {"results": [], "summary": {"total": 0, "ok": 0, "refused": 0, "applied": 0, "rolled_back": 0,
                                           "dry_run": not commit, "reason": "no_accounts"}}

    def _one(aid):
        try:
            return reconcile_account(aid, commit=commit, expected_owner_id=expected_owner_id,
                                     expected_login=expected_login, expected_workspace_uuid=expected_workspace_uuid,
                                     ops=ops, teardown=teardown)
        except Exception as e:  # noqa: BLE001 — isolate: one account's crash never aborts the batch
            return {"ok": False, "account_id": aid, "reason": f"exception:{type(e).__name__}"}

    results = []
    if max_concurrency is None or int(max_concurrency) <= 1:
        for aid in ids:
            results.append(_one(aid))
    else:
        import concurrent.futures as _f
        with _f.ThreadPoolExecutor(max_workers=min(len(ids), int(max_concurrency))) as ex:
            fut = {ex.submit(_one, aid): aid for aid in ids}
            by_id = {}
            for f in _f.as_completed(fut):
                r = f.result()
                by_id[fut[f]] = r
            results = [by_id[aid] for aid in ids]

    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r.get("ok")),
        "refused": sum(1 for r in results if not r.get("ok")),
        "applied": sum(1 for r in results if r.get("applied")),
        "rolled_back": sum(1 for r in results if r.get("rolled_back")),
        "dry_run": not commit,
        "operation": ("TEARDOWN_READONLY" if teardown else "ENABLE_READONLY"),
    }
    return {"results": results, "summary": summary}


# ── Real host transport (operator-run; NOT exercised in CI) ──────────────────────────────────────────────────────
class _RealMonitoringOps(MonitoringReconcileOps):
    """The real adapter: signs + dispatches RESTORE_TENANT_BRIDGE_EXECUTION_OFF via the per-tenant signed host
    executor and reads positions back from the bridge. Never run in tests (which inject a fake)."""

    def __init__(self, executor_resolver=None):
        self._executor_resolver = executor_resolver

    def _executor(self, account):
        if self._executor_resolver is not None:
            return self._executor_resolver(account)
        from hosted_workspace.host_executor import resolve_signed_host_executor
        return resolve_signed_host_executor(account_id=int(account.id))

    def apply_off_env(self, account, *, env_b64, env_sha, port, runtime_root, expect_readonly):
        executor = self._executor(account)
        if executor is None:
            return (False, None, "host_executor_unavailable")
        result = executor.restore_tenant_bridge_execution_off(runtime_root, port, env_b64)
        return (True, result, "dispatched")

    def read_back_positions(self, account):
        # Operator-run read-back: a plain positions READ over the per-tenant transport (never an order). Implemented
        # by the trade-history read path on the host; left to the operator wiring so CI contacts no bridge.
        return {"http_status": None, "ok": False, "reason": "read_back_requires_operator_wiring"}
