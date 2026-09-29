"""hosted_workspace.decommission - Remove Broker Account STAGE-2 physical host teardown (async, retryable).

Logical removal (tombstone: credential destroy + is_active=False + disconnected_at, endpoint retire, entitlement
release) is authoritative and commits FIRST in ``trading.account_removal.remove_account``, which then flips this
workspace's ``cleanup_state`` to PENDING inside that same transaction. This module drives the SEPARATE physical
reclamation of the removed account's Windows footprint via the governed signed-executor primitives, in dependency
order, idempotently, with bounded retry/backoff.

INVARIANT (Stage-1 vs Stage-2): a failure here NEVER un-tombstones, restores credentials, restores entitlement
consumption, or re-enables execution. This module writes ONLY the ``cleanup_*`` columns (via queryset .update, so
the model's immutable-binding guard never refetches) - never ``is_active``/``disconnected_at``. It fails CLOSED:
it refuses to physically tear down anything that is not still tombstoned, and re-checks open positions before
teardown (never orphan/destroy a terminal that still has a live position).

Driven by the hosted observation cron (``run_hosted_observations.run_cycle`` -> ``run_workspace_cleanup``);
single-flight via that cron's Postgres advisory lock, so per-account teardown never overlaps itself.
"""
from __future__ import annotations

import logging

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger("guvfx.hosted_workspace")

# --- cleanup lifecycle states (stored on HostedMt5Workspace.cleanup_state) --------------------------------------
CLEANUP_NOT_REQUIRED = "NOT_REQUIRED"     # no hosted footprint to reclaim (default; also BETA accounts)
CLEANUP_PENDING = "PENDING"               # queued by remove_account, awaiting first teardown attempt
CLEANUP_RUNNING = "RUNNING"               # a teardown attempt is in flight (leased via cleanup_next_retry_at)
CLEANUP_SUCCEEDED = "SUCCEEDED"           # host footprint fully reclaimed + verified
CLEANUP_FAILED_RETRYABLE = "FAILED_RETRYABLE"  # a step left residual / host unreachable; retries with backoff

_ACTIVE_STATES = (CLEANUP_PENDING, CLEANUP_RUNNING, CLEANUP_FAILED_RETRYABLE)
_MAX_ATTEMPTS = 8            # after this the account still retries, but on the long backoff + a raised alert
_LEASE_SECONDS = 300        # a RUNNING claim older than this is treated as a crashed attempt and re-tried
_BACKOFF_BASE_SECONDS = 120
_BACKOFF_MAX_SECONDS = 3600
_HARD_MAX_PER_CYCLE = 12    # never fan out beyond node capacity in one cron tick

SOURCE = "hosted_workspace.decommission"


def _empty(enabled: bool) -> dict:
    return {"enabled": enabled, "polled": 0, "succeeded": 0, "retry": 0, "skipped": 0, "errors": 0, "reasons": {}}


def _backoff_seconds(attempts: int) -> int:
    return min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))


def _write(ws, *, state, now, reason="", next_retry_at=..., bump_attempts=False):
    """Write ONLY the cleanup_* columns (never is_active/disconnected_at). queryset .update avoids the model's
    immutable-binding refetch. Returns the rowcount (0 => lost a race / row gone)."""
    from hosted_workspace.models import HostedMt5Workspace
    fields = {"cleanup_state": state, "cleanup_last_reason": (reason or "")[:120]}
    if next_retry_at is not ...:
        fields["cleanup_next_retry_at"] = next_retry_at
    qs = HostedMt5Workspace.objects.filter(pk=ws.pk)
    if bump_attempts:
        from django.db.models import F
        fields["cleanup_attempts"] = F("cleanup_attempts") + 1
    return qs.update(**fields)


def _endpoint_port(ws) -> int:
    """The account's OWN bridge port from its (now retired) HostedExecutionEndpoint row - the only reliable way to
    identify the shared bridge python process on the host. 0 if none (host then skips the port-targeted kill)."""
    try:
        from execution.models import HostedExecutionEndpoint
        ep = HostedExecutionEndpoint.objects.filter(workspace=ws).order_by("-id").first()
        return int(getattr(ep, "port", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def run_workspace_cleanup(*, executor_resolver=None, now=None) -> dict:
    """One bounded pass of STAGE-2 physical cleanup. Selects tombstoned workspaces whose cleanup is due, and drives
    each through the ordered governed teardown. Never raises into the scheduler; never touches a non-tombstoned
    account. DARK unless the master ``hosted_persistent_mt5_enabled()`` is on (same gate as observation)."""
    from hosted_workspace.flags import hosted_persistent_mt5_enabled
    if not hosted_persistent_mt5_enabled():
        return _empty(False)
    from hosted_workspace.models import HostedMt5Workspace
    if now is None:
        now = timezone.now()

    qs = (HostedMt5Workspace.objects
          .select_related("trading_account", "execution_node")
          # MUST be tombstoned - a physical teardown may only ever follow a committed logical removal.
          .filter(trading_account__disconnected_at__isnull=False)
          .filter(cleanup_state__in=_ACTIVE_STATES)
          .filter(Q(cleanup_next_retry_at__isnull=True) | Q(cleanup_next_retry_at__lte=now))
          .order_by("cleanup_next_retry_at")[:_HARD_MAX_PER_CYCLE])

    out = _empty(True)
    for ws in list(qs):
        out["polled"] += 1
        try:
            outcome, reason = _cleanup_one(ws, executor_resolver=executor_resolver, now=now)
        except Exception:  # noqa: BLE001 - one account's teardown failure must not stop the pass
            out["errors"] += 1
            logger.exception("hosted cleanup failed workspace=%s", getattr(ws, "pk", None))
            continue
        out[outcome] = out.get(outcome, 0) + 1
        if reason:
            out["reasons"][reason] = out["reasons"].get(reason, 0) + 1
    return out


def _cleanup_one(ws, *, executor_resolver, now) -> tuple:
    """Drive ONE workspace's physical teardown. Returns (outcome, reason) where outcome in
    {'succeeded','retry','skipped'}. Fail-closed on every ambiguity; writes only cleanup_* columns."""
    account = ws.trading_account

    # Customer-Zero / reserved floor (defence in depth over the daemon + dispatch refusals): a reserved account is
    # never physically torn down. De-queue it so it is not re-selected every cycle.
    if int(getattr(account, "id", 0) or 0) < 2:
        _write(ws, state=CLEANUP_NOT_REQUIRED, now=now, reason="reserved_account", next_retry_at=None)
        return "skipped", "reserved_account"

    # FAIL-CLOSED re-check 1: must still be tombstoned. If not (should be impossible - enqueue only on tombstone),
    # NEVER physically tear down a live account; hold as an integrity error for the operator.
    if getattr(account, "disconnected_at", None) is None:
        _write(ws, state=CLEANUP_FAILED_RETRYABLE, now=now, reason="not_tombstoned",
               next_retry_at=now + timezone.timedelta(seconds=_BACKOFF_MAX_SECONDS))
        logger.error("hosted_cleanup_integrity account=%s not tombstoned but cleanup queued - refusing teardown",
                     getattr(account, "id", None))
        return "skipped", "not_tombstoned"

    # FAIL-CLOSED re-check 2 (Phase 4): never physically decommission a terminal that still has an open broker
    # position. This re-queries the SAME authoritative source the logical-removal gate used (Trade.close_time IS
    # NULL). It is sound as the sole gate here because a tombstoned account can open NO new positions (execution is
    # killed at removal: readiness/arm/claim all fail closed on disconnected_at), so the count cannot GROW after
    # removal - if removal passed with 0 open, Stage-2 still sees 0. A non-zero count (a late-ingested position that
    # existed at removal time) holds the teardown for retry rather than tearing down a position-bearing terminal.
    from trading.account_removal import open_position_count
    if open_position_count(account) > 0:
        _write(ws, state=CLEANUP_FAILED_RETRYABLE, now=now, reason="open_positions",
               next_retry_at=now + timezone.timedelta(seconds=_backoff_seconds(int(ws.cleanup_attempts or 0) + 1)))
        return "skipped", "open_positions"

    # Resolve the signed host executor (DARK/unconfigured -> hold + retry; never a partial teardown).
    if executor_resolver is None:
        from hosted_workspace.host_executor import resolve_signed_host_executor
        from hosted_workspace.live_observe import _node_rdp_host
        executor = resolve_signed_host_executor(account_id=int(account.id), rdp_host=_node_rdp_host(ws))
    else:
        executor = executor_resolver(int(account.id), ws)
    if executor is None:
        _write(ws, state=CLEANUP_FAILED_RETRYABLE, now=now, reason="no_executor",
               next_retry_at=now + timezone.timedelta(seconds=_backoff_seconds(int(ws.cleanup_attempts or 0) + 1)))
        return "retry", "no_executor"

    # CLAIM: a real compare-and-swap - claim the row ONLY if it is still in an active state and due (the state/lease
    # we selected on has not changed). rowcount 0 => a concurrent tick already claimed it => skip. This is the
    # double-run belt over the cron's advisory lock, not merely an unconditional write.
    from django.db.models import F, Q
    from hosted_workspace.models import HostedMt5Workspace
    claimed = (HostedMt5Workspace.objects
               .filter(pk=ws.pk, cleanup_state__in=_ACTIVE_STATES)
               .filter(Q(cleanup_next_retry_at__isnull=True) | Q(cleanup_next_retry_at__lte=now))
               .update(cleanup_state=CLEANUP_RUNNING, cleanup_attempts=F("cleanup_attempts") + 1,
                       cleanup_last_reason="running",
                       cleanup_next_retry_at=now + timezone.timedelta(seconds=_LEASE_SECONDS)))
    if not claimed:
        return "skipped", "lost_claim"
    attempts = int(ws.cleanup_attempts or 0) + 1

    from hosted_workspace.host_agent_dispatch import derive_slot
    slot = derive_slot(int(account.id))
    user, root = slot["username"], slot["runtime_root"]
    port = _endpoint_port(ws)

    # ORDERED governed teardown (each op idempotent; already-absent => ok). Order: stop the observer first (no new
    # attach), unpublish RemoteApp (member cannot relaunch), strip AppLocker, then the physical runtime teardown
    # (bridge+watchdog+port, terminal by owner+path, session, tenant+runtime dirs, disable identity) LAST.
    steps = [
        ("remove_observer", lambda: executor.remove_observer(username=user, runtime_root=root)),
        ("remove_remoteapp", lambda: executor.remove_remoteapp(username=user, runtime_root=root)),
        ("applocker_remove", lambda: executor.applocker_remove(username=user)),
        ("decommission_runtime", lambda: executor.decommission_runtime(username=user, runtime_root=root, port=port)),
    ]
    failed = []
    for name, call in steps:
        try:
            res = call() or {}
        except Exception as exc:  # noqa: BLE001 - a transport/host error is a retryable hold, never a partial claim of success
            failed.append(f"{name}:exc")
            logger.warning("hosted cleanup step raised account=%s step=%s err=%r", account.id, name, exc)
            continue
        if not bool(res.get("ok")):
            failed.append(f"{name}:{str(res.get('reason') or 'not_ok')[:40]}")

    if not failed:
        _write(ws, state=CLEANUP_SUCCEEDED, now=now, reason="cleaned", next_retry_at=None)
        logger.info("hosted_cleanup_succeeded account=%s attempts=%s", account.id, attempts)
        return "succeeded", "cleaned"

    reason = ";".join(failed)[:120]
    if attempts >= _MAX_ATTEMPTS:
        logger.warning("hosted_cleanup_stuck account=%s attempts=%s reason=%s", account.id, attempts, reason)
    _write(ws, state=CLEANUP_FAILED_RETRYABLE, now=now, reason=reason,
           next_retry_at=now + timezone.timedelta(seconds=_backoff_seconds(attempts)))
    return "retry", "step_failed"
