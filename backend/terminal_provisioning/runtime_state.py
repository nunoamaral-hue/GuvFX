"""GFX-BETA-PHASE0 Increment 2 — AccountRuntime state-machine service.

Records durable provisioning state transitions with immutable RuntimeEvent evidence. The user-facing
state is derived ONLY from the durable AccountRuntime record — never inferred solely from a transient
live process/health check. Phase-0: records state only; it does NOT perform provisioning.
"""
from django.db import transaction

from .models import AccountRuntime, RuntimeEvent, RuntimeState

# Panel-facing labels (Increment 3 renders these). Derived from the DURABLE state only.
USER_FACING = {
    RuntimeState.NOT_PROVISIONED: "NOT_CONFIGURED",
    RuntimeState.QUEUED: "QUEUED",
    RuntimeState.BLOCKED: "BLOCKED",
    RuntimeState.PROVISIONING: "PROVISIONING",
    RuntimeState.STARTING: "PROVISIONING",
    RuntimeState.AUTHENTICATING: "PROVISIONING",
    RuntimeState.RUNNING: "RUNNING",
    RuntimeState.DEGRADED: "DEGRADED",
    RuntimeState.REPAIRING: "DEGRADED",
    RuntimeState.STOPPING: "STOPPED",
    RuntimeState.STOPPED: "STOPPED",
    RuntimeState.DEPROVISIONING: "REMOVING",
    RuntimeState.REMOVED: "REMOVED",
    RuntimeState.FAILED: "FAILED",
}

# States that carry a failure/attention reason.
_ATTENTION = {RuntimeState.FAILED, RuntimeState.DEGRADED, RuntimeState.BLOCKED}

_UNSET = object()  # sentinel so "not passed" is distinguishable from an explicit None


def get_or_create_runtime(account) -> AccountRuntime:
    """Return the account's durable runtime record (NOT_PROVISIONED on first touch). 1:1 per account."""
    rt, _ = AccountRuntime.objects.get_or_create(trading_account=account)
    return rt


def record_transition(runtime, to_state, *, event_type="TRANSITION", reason_code="", detail="",
                      attempt=None, next_retry_at=_UNSET) -> AccountRuntime:
    """Atomically transition the runtime and append an immutable RuntimeEvent. Serialised via
    ``select_for_update`` so concurrent transitions cannot interleave or lose an event.

    ``last_error`` only ever receives the sanitised ``reason_code`` (never raw ``detail``), so no raw
    agent string reaches the user-facing field regardless of caller. The raw ``detail`` is stored on
    the immutable event (admin-only). ``next_retry_at``/``attempt`` are preserved when not passed."""
    with transaction.atomic():
        rt = AccountRuntime.objects.select_for_update().get(pk=runtime.pk)
        RuntimeEvent.objects.create(
            runtime=rt, event_type=event_type, from_state=rt.state, to_state=to_state,
            reason_code=(reason_code or "")[:64], detail=(detail or "")[:2000])
        rt.state = to_state
        if attempt is not None:
            rt.attempt = attempt
        if next_retry_at is not _UNSET:
            rt.next_retry_at = next_retry_at
        rt.last_error = (reason_code or "")[:500] if to_state in _ATTENTION else ""
        rt.save(update_fields=["state", "attempt", "next_retry_at", "last_error", "updated_at"])
    return rt


def user_facing_state(runtime) -> str:
    """Panel-facing state derived ONLY from the durable AccountRuntime.state — never from a transient
    process/health probe. (Increment 2/3 requirement.)"""
    return USER_FACING.get(runtime.state, "NOT_CONFIGURED")
