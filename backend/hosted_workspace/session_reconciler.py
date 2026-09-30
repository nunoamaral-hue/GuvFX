"""hosted_workspace.session_reconciler — P4 cold-boot SESSION recovery reconciler (DARK, 2026-09-30).

THE PROBLEM (project_host_reboot_recovery_gap): after a host cold boot, a per-tenant /portable MT5 TERMINAL does
not auto-recover, because its interactive ``guvfx_u_<id>`` RDS SESSION is gone (a boot destroys all sessions; only
Administrator autologons). ``liveness_recovery`` relaunches a TERMINAL inside an EXISTING session, so it
structurally cannot help when the SESSION itself is absent (its observe needs a live session). This reconciler is
the missing step that runs BEFORE observation can work: it selects the same armed candidates, reads each one's
Windows SESSION state via the read-only PROBE_SESSION host-op, and drives the session back to the desired state.

THE PINNED DESIRED-STATE TABLE (Sponsor 2026-09-30) — the PROBE is authoritative; the classification decides the
action; the atomic claim is only PERMISSION TO ATTEMPT (cooldown/cap), never proof recovery is warranted:
  * ACTIVE        -> converge only; NEVER create a session.
  * DISCONNECTED  -> reconnect the EXISTING session; NEVER create a second Windows session.
  * ABSENT        -> establish a fresh session, but ONLY after all server-derived desired-state gates pass.
  * UNKNOWN       -> fail closed / no action. ``ok:false``, a missing/malformed status, an out-of-enum value, or a
                     transport failure are ALL normalised to UNKNOWN here (never ABSENT).

TWO INDEPENDENT DARKNESS GATES:
  * ``hosted_session_reconciler_enabled()`` (+ master ``hosted_persistent_mt5_enabled()``) turns the reconciler on.
    With ONLY these, it SELECTS / PROBES / CLASSIFIES / AUDITS and performs ZERO credential decryption and ZERO
    self-connect (probe-only). No attempt is claimed and no session is touched.
  * ``hosted_session_reconciler_arm_selfconnect_enabled()`` (P4-c) is SEPARATELY required for any actual
    establish/reconnect. Establishment additionally stays impossible unless the host executor is armed+configured
    (``resolve_host_executor`` non-None), the desired-state gate (``workspace_delivery_ready``) passes, and a real
    ``establish_fn`` is wired (P4-c). In P4-b the default ``establish_fn`` is None, so it is probe-only by
    construction even if the arm flag were set — the establish path exists only for tests (injected) until P4-c.

SAFETY:
  * Customer Zero (1) and account 18 are reserved and excluded (reuse capability_recovery._RESERVED_ACCOUNT_IDS);
    the executor + host + .ps1 refuse CZ again (defence in depth).
  * ARMS NOTHING, LOGS IN NOTHING, PLACES NO ORDER. Restoring a runtime is NOT execution readiness / TRADING: the
    independent observe -> capability_recovery -> auto_arm chain + the readiness freshness gate remain the sole
    re-arm authority. A DISCONNECTED session is NOT proof its MT5 terminal exists — the TERMINAL dimension is
    liveness_recovery's job once the session is back (P5 orchestrates both dimensions).
  * BOUNDED / LOOP-SAFE: at most MAX_RECOVERY_ATTEMPTS per workspace, each behind RECOVERY_COOLDOWN_S, claimed
    atomically BEFORE the self-connect; a persistently-failing establish backs off and raises ONE operator alert.
  * Per-attempt audit is SECRET-FREE (internal account id + enum-ish phase/status/decision/action/reason only) —
    never a Windows password, Guacamole token, or broker credential.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from execution.readiness import _observation_fresh
from hosted_workspace.capability_recovery import _RESERVED_ACCOUNT_IDS
from hosted_workspace.delivery import workspace_delivery_ready
from hosted_workspace.flags import (
    hosted_persistent_mt5_enabled,
    hosted_session_reconciler_arm_selfconnect_enabled,
    hosted_session_reconciler_enabled,
)
from hosted_workspace.liveness_recovery import _armed_and_matched   # canonical "should be trading" predicate
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.slot_preparation import resolve_host_executor

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.session_reconciler"

# Bounds (conservative; mirror liveness_recovery). At most 3 establish/reconnect cycles per workspace, each behind a
# 5-minute cooldown; a persistently-unrecoverable session is attempted at most 3 times over ~15 min, then the runner
# stops and the OPEN operator alert carries it to a human (never a self-connect loop).
MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_COOLDOWN_S = 300

# P5 cold-boot STAGGERING: cap the number of actual self-connect ESTABLISH/RECONNECT attempts per pass. A cold boot
# can make the whole estate ABSENT at once; without a cap the reconciler would fire N simultaneous guacd self-
# connects (RDS/CPU surge on the 8 vCPU host). With the cap, each pass establishes a bounded few and the cron
# cadence staggers the remainder across subsequent passes (idempotent: the rest stay ABSENT and are retried).
# Deliberately conservative for the current host (reference_windows_host_capacity: safe=12). Probe/classify/audit
# are NOT capped — only the credential-decrypting self-connect is.
MAX_ESTABLISH_PER_CYCLE = 4

_SESSION_STATES = ("ACTIVE", "DISCONNECTED", "ABSENT", "UNKNOWN")
_ALERT_PREFIX = "hosted_session_recovery"


def _ok(res) -> bool:
    return bool(res) and bool(res.get("ok"))


def _normalize_status(probe) -> str:
    """Fail-closed classification: return one of _SESSION_STATES. An ok:false / missing / malformed / out-of-enum
    probe is UNKNOWN, NEVER ABSENT. (probe_session already guarantees this; re-normalise defensively so an injected
    probe_fn or a future caller cannot bypass the invariant.)"""
    if not isinstance(probe, dict) or not probe.get("ok"):
        return "UNKNOWN"
    status = probe.get("session_status")
    return status if status in _SESSION_STATES else "UNKNOWN"


def _attempt_allowed(ws, now) -> bool:
    if (ws.session_recovery_count or 0) >= MAX_RECOVERY_ATTEMPTS:
        return False
    last = ws.session_recovery_at
    if last is not None and (now - last).total_seconds() < RECOVERY_COOLDOWN_S:
        return False
    return True


def _claim_attempt(pk, now) -> bool:
    """Atomically CLAIM one self-connect attempt: re-check (under a row lock) the workspace is still armed and still
    within the bound/cooldown, then stamp the attempt BEFORE any self-connect. False if concurrently consumed.

    NOTE (Sponsor 2026-09-30): a successful claim is PERMISSION TO ATTEMPT, not proof recovery is warranted — the
    PROBE + classification (done before this) are authoritative; the claim only enforces the cooldown/cap."""
    with transaction.atomic():
        ws = HostedMt5Workspace.objects.select_for_update().select_related("trading_account").get(pk=pk)
        account = getattr(ws, "trading_account", None)
        if account is None or not _armed_and_matched(ws, account) or not _attempt_allowed(ws, now):
            return False
        ws.session_recovery_at = now
        ws.session_recovery_count = (ws.session_recovery_count or 0) + 1
        ws.save(update_fields=["session_recovery_at", "session_recovery_count", "updated_at"])
        return True


def _reset_recovery_counter(ws) -> None:
    """On a confirmed-ACTIVE session, clear the attempt counter/timestamp so the bound is PER-INCIDENT, not
    per-lifetime — a cold boot recurs, so a healthy session must restore a fresh attempt budget (differs
    intentionally from the certified liveness_recovery; Amber). No-op when already clear. Best-effort/fail-open."""
    if (ws.session_recovery_count or 0) == 0 and ws.session_recovery_at is None:
        return
    try:
        with transaction.atomic():
            row = HostedMt5Workspace.objects.select_for_update().get(pk=ws.pk)
            row.session_recovery_count = 0
            row.session_recovery_at = None
            row.save(update_fields=["session_recovery_count", "session_recovery_at", "updated_at"])
    except Exception:  # pragma: no cover — best-effort
        logger.warning("session_reconciler: reset-counter failed acct %s",
                       getattr(getattr(ws, "trading_account", None), "id", "?"))


def _safe_call(ex, method_name, *args, **kwargs) -> dict:
    fn = getattr(ex, method_name, None)
    if fn is None:
        return {"ok": False, "reason": "executor_incomplete"}
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — host errors are sanitised, never propagated or logged verbatim
        logger.warning("session_reconciler: host step %s errored", method_name)
        return {"ok": False, "reason": "host_error"}


def _audit(account, *, correlation_id, phase, status="", decision="", action="", reason="") -> None:
    """One SECRET-FREE per-attempt audit record (durable OperationalEvent) + a structured log line, distinguishing
    selection / probe result / decision / attempted action-result / fail-closed reason. Records ONLY the internal
    account id + enum-ish fields — never a Windows password, Guacamole token, or broker credential. Fail-open."""
    try:
        from operational_events.constants import CATEGORY_RUNTIME
        from operational_events.events import record_event
        record_event(
            category=CATEGORY_RUNTIME, event_type="workspace.session_recovery", severity="INFO",
            account=account, source=SOURCE, correlation_id=str(correlation_id or ""),
            summary=f"session reconcile acct {getattr(account, 'id', '?')}: {phase}",
            reason_code=str(reason or ""), status=str(status or ""), customer_visible=False,
            metadata={"account_id": getattr(account, "id", None), "phase": phase, "session_status": status,
                      "decision": decision, "action": action, "reason": reason},
            dedup_key=f"{_ALERT_PREFIX}:{correlation_id}:{getattr(account, 'id', '?')}:{phase}")
    except Exception:  # pragma: no cover — audit is best-effort, never breaks the reconciler
        pass
    logger.info("session_reconciler: acct=%s phase=%s status=%s decision=%s action=%s reason=%s",
                getattr(account, "id", "?"), phase, status, decision, action, reason)


def _alert_dedup_key(account_id) -> str:
    return f"{_ALERT_PREFIX}:account:{account_id}"


def _open_session_down_alert(account, *, exhausted: bool) -> None:
    """Raise (idempotently) ONE operator alert that this armed workspace's Windows SESSION is unavailable and could
    not be auto-recovered. Best-effort; never raises into the runner. No secret — the internal account id only."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = _alert_dedup_key(account.id)
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.CRITICAL, component=Component.MT5_TERMINAL,
            trading_account_id=account.id,
            title=f"[GuvFX execution] Workspace acct {account.id}: Windows session unavailable after reboot",
            body=("[GuvFX execution] Workspace acct " + str(account.id) + ": the Windows/MT5 runtime session is "
                  "absent or unreachable and automated recovery is required. Execution stays paused safely."
                  + (" Automatic session-recovery attempts exhausted - operator intervention needed." if exhausted else "")),
            dedup_key=dedup_key, status=AlertEvent.Status.OPEN,
            detail={"account_id": account.id, "reason": "session_unavailable", "exhausted": bool(exhausted)})
    except Exception:  # pragma: no cover — alerting is best-effort
        logger.exception("session_reconciler: open-alert failed acct %s", getattr(account, "id", "?"))


def _resolve_session_down_alert(account) -> None:
    """Resolve any OPEN session-down alert for this account (called when the session is now ACTIVE). Best-effort."""
    try:
        from reliability.models import AlertEvent
        AlertEvent.objects.filter(dedup_key=_alert_dedup_key(account.id), status=AlertEvent.Status.OPEN).update(
            status=AlertEvent.Status.RESOLVED, resolved_at=timezone.now())
    except Exception:  # pragma: no cover — best-effort
        logger.exception("session_reconciler: resolve-alert failed acct %s", getattr(account, "id", "?"))


def _new_correlation_id() -> str:
    try:
        from core.observability import new_correlation_id
        return new_correlation_id()
    except Exception:  # pragma: no cover
        import secrets
        return secrets.token_hex(8)


def _empty_summary(enabled: bool) -> dict:
    return {"enabled": enabled, "self_connect_armed": False, "candidates": 0, "active": 0, "disconnected": 0,
            "absent": 0, "unknown": 0, "established": 0, "reconnected": 0, "skipped_healthy": 0,
            "skipped_no_executor": 0, "skipped_not_armed": 0, "skipped_gate": 0, "skipped_cooldown": 0,
            "skipped_rate_limited": 0, "errors": 0}


def run_hosted_session_reconciler(*, actor: str = SOURCE, executor_resolver=None, probe_fn=None,
                                  establish_fn=None) -> dict:
    """One session-reconciliation pass. DARK unless master + reconciler flags on. Probe-first (authoritative),
    then classify per the pinned table; self-connect ONLY when separately armed AND all server-derived gates pass.
    Idempotent, loop-safe, fail-closed per workspace. Arms nothing; places no order. Returns a secret-free summary.

    Seams (all injectable for host-free tests): ``executor_resolver(account_id, rdp_host) -> SignedHostExecutor|None``;
    ``probe_fn(ws) -> probe-dict`` (defaults to the executor's read-only ``probe_session``); ``establish_fn(ws,
    account, node, status) -> {"ok": bool, ...}`` (the P4-c guacd self-connect driver; None here => probe-only)."""
    if not (hosted_persistent_mt5_enabled() and hosted_session_reconciler_enabled()):
        return _empty_summary(False)

    now = timezone.now()
    corr = _new_correlation_id()
    resolve_ex = executor_resolver or (lambda account_id, rdp_host: resolve_host_executor(
        account_id=account_id, rdp_host=rdp_host))
    # Self-connect is possible ONLY when the ARM sub-gate is on AND a real establish_fn is wired (P4-c). In P4-b the
    # default establish_fn is None => probe-only by construction, regardless of the arm flag. Tests inject it.
    self_connect_armed = hosted_session_reconciler_arm_selfconnect_enabled() and establish_fn is not None

    s = _empty_summary(True)
    s["self_connect_armed"] = bool(self_connect_armed)
    established_attempts = 0                 # P5 staggering: self-connect attempts made THIS pass (capped)

    qs = (HostedMt5Workspace.objects
          .filter(execution_enabled=True, execution_authorized_at__isnull=False, proj_account_match=True,
                  trading_account__is_demo=True, trading_account__workspace_confirmed_at__isnull=False)
          .filter(trading_account__disconnected_at__isnull=True)          # never recover a tombstoned account
          .exclude(trading_account_id__in=_RESERVED_ACCOUNT_IDS)          # CZ (1) + account 18 reserved
          .select_related("trading_account", "execution_node")
          .iterator())

    for ws in qs:
        account = getattr(ws, "trading_account", None)
        if account is None:
            s["errors"] += 1
            continue
        # A FRESH workspace has a live, observed session — not a recovery candidate. Only STALE (needs-attention)
        # workspaces are probed (a cold boot makes the projection go stale within the freshness window).
        if _observation_fresh(ws):
            s["skipped_healthy"] += 1
            continue
        s["candidates"] += 1
        # ONE authoritative node for probe + gate + establish (F1). The delivery gate (workspace_delivery_ready)
        # AND the P4-c self-connect BOTH mint from ``workspace_node``, so the read-only PROBE must target the SAME
        # host — otherwise we could probe host A, pass the gate on host B, and establish over a session that is live
        # on B. Use workspace_node throughout and FAIL CLOSED on any divergence from execution_node.
        node = getattr(ws, "workspace_node", None)
        if node is None:
            s["skipped_no_executor"] += 1
            _audit(account, correlation_id=corr, phase="select", action="skipped", reason="no_workspace_node")
            continue
        exec_node = getattr(ws, "execution_node", None)
        if exec_node is not None and getattr(exec_node, "id", None) != getattr(node, "id", None):
            s["skipped_gate"] += 1               # data-integrity fail-closed: never probe/establish a split host
            _audit(account, correlation_id=corr, phase="select", action="skipped", reason="node_divergence")
            continue
        rdp_host = str(getattr(node, "rdp_host", "") or "").strip()
        if not rdp_host:
            s["skipped_no_executor"] += 1
            _audit(account, correlation_id=corr, phase="select", action="skipped", reason="no_rdp_host")
            continue
        ex = resolve_ex(account.id, rdp_host)
        if ex is None:                        # DARK / unarmed host executor — cannot even probe
            s["skipped_no_executor"] += 1
            _audit(account, correlation_id=corr, phase="select", action="skipped", reason="no_executor")
            continue

        # PROBE is authoritative (read-only PROBE_SESSION). Its classification decides the action.
        probe = probe_fn(ws) if probe_fn is not None else _safe_call(ex, "probe_session", rdp_host=rdp_host)
        status = _normalize_status(probe)

        if status == "ACTIVE":
            s["active"] += 1
            _resolve_session_down_alert(account)      # the session is back / up
            _reset_recovery_counter(ws)               # per-INCIDENT cap: a confirmed-healthy session gets a fresh
                                                      # attempt budget, so a later reboot is not starved (F2, Amber)
            _audit(account, correlation_id=corr, phase="classify", status=status,
                   decision="converge", action="none", reason="session_active")
            continue
        if status == "UNKNOWN":
            s["unknown"] += 1
            _audit(account, correlation_id=corr, phase="classify", status=status,
                   decision="none", action="none", reason="fail_closed_unknown")
            continue

        # status in (ABSENT, DISCONNECTED): action WARRANTED by classification.
        decision = "establish" if status == "ABSENT" else "reconnect"
        if status == "ABSENT":
            s["absent"] += 1
        else:
            s["disconnected"] += 1

        # Probe-only mode (arm sub-gate off OR no establish_fn): decide + audit, but perform ZERO self-connect and
        # ZERO credential decryption. Nothing is claimed (a claim is permission to ATTEMPT a self-connect).
        if not self_connect_armed:
            s["skipped_not_armed"] += 1
            _audit(account, correlation_id=corr, phase="decide", status=status, decision=decision,
                   action="skipped", reason="self_connect_not_armed")
            continue

        # Server-derived desired-state gate: the slot must be deliverable (flags + node.rdp_host + PROVISIONED
        # non-admin identity + credential + GUAC configured). Fail-closed on any missing precondition.
        if not workspace_delivery_ready(ws):
            s["skipped_gate"] += 1
            _audit(account, correlation_id=corr, phase="gate", status=status, decision=decision,
                   action="skipped", reason="delivery_not_ready")
            continue

        # P5 STAGGERING: cap self-connect attempts per pass. A rate-limited candidate is DEFERRED (no cooldown burn,
        # no claim) and retried next cycle - it stays ABSENT/DISCONNECTED, so convergence is idempotent.
        if established_attempts >= MAX_ESTABLISH_PER_CYCLE:
            s["skipped_rate_limited"] += 1
            _audit(account, correlation_id=corr, phase="gate", status=status, decision=decision,
                   action="skipped", reason="rate_limited")
            continue

        if not _attempt_allowed(ws, now):
            exhausted = (ws.session_recovery_count or 0) >= MAX_RECOVERY_ATTEMPTS   # F3: distinguish cap vs cooldown
            _open_session_down_alert(account, exhausted=exhausted)
            s["skipped_cooldown"] += 1
            _audit(account, correlation_id=corr, phase="gate", status=status, decision=decision, action="skipped",
                   reason=("exhausted" if exhausted else "cooldown"))
            continue
        _open_session_down_alert(account, exhausted=False)
        if not _claim_attempt(ws.pk, now):
            s["skipped_cooldown"] += 1
            _audit(account, correlation_id=corr, phase="gate", status=status, decision=decision,
                   action="skipped", reason="claim_lost")
            continue

        # ACT via the seam. ABSENT -> establish a fresh session; DISCONNECTED -> reconnect the EXISTING session
        # (the driver reuses the stable per-workspace connection id so Windows rejoins rather than duplicating).
        established_attempts += 1            # this self-connect counts toward the per-pass stagger cap
        r = _safe_call_establish(establish_fn, ws, account, node, status)
        if _ok(r):
            if status == "ABSENT":
                s["established"] += 1
            else:
                s["reconnected"] += 1
            _audit(account, correlation_id=corr, phase="act", status=status, decision=decision,
                   action="ok", reason=str((r or {}).get("reason", "")))
        else:
            s["errors"] += 1
            _audit(account, correlation_id=corr, phase="act", status=status, decision=decision,
                   action="failed", reason=str((r or {}).get("reason", "self_connect_failed")))

    return s


def _safe_call_establish(establish_fn, ws, account, node, status) -> dict:
    if establish_fn is None:                  # defence in depth: never reached when self_connect_armed is True
        return {"ok": False, "reason": "establish_unavailable"}
    try:
        return establish_fn(ws, account, node, status)
    except Exception:  # noqa: BLE001 — self-connect errors are sanitised, never propagated
        logger.warning("session_reconciler: establish errored acct %s", getattr(account, "id", "?"))
        return {"ok": False, "reason": "establish_error"}
