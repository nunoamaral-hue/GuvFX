"""GFX-BETA-HEADLESS Increment 1 — beta runtime ownership + capacity service.

Enforces the controlled-beta blast-radius rules on the co-hosted (Option A) pool. Operates ONLY on
``AccountRuntime`` rows with ``cohort=BETA`` — Nuno's ``PRODUCTION`` runtimes are structurally excluded
from every count, cap, and control here (compensating control 14; kill switch control 16).

Compensating controls implemented:
 13  Capacity reservation before provisioning (atomic; a slot is held before any host work).
 14  Five-runtime global beta cap + one active runtime per beta user (production excluded).
 15  Production resource reserve hook (host-pressure admission — pluggable; wired by a later increment).
 16  Beta-only global kill switch (``beta_runtimes_enabled`` False ⇒ no beta provisioning; never touches PRODUCTION).
 17  Per-runtime quarantine / stop controls.

Fail-closed: unknown or over-capacity conditions raise ``CapacityError`` with a sanitised reason code;
the beta gate is CLOSED by default, so no reservation succeeds until an operator explicitly enables it.
"""
import os
from typing import Callable, Optional

from django.conf import settings
from django.db import transaction

from .beta_paths import canonical_beta_runtime_root
from .models import AccountRuntime, BetaCapacityLock, RuntimeState
from .runtime_state import record_transition

# ── Caps (initial controlled beta; raised only after the first five-runtime soak passes) ──
BETA_MAX_ACTIVE_RUNTIMES = 5      # control 14 — global
BETA_MAX_ACTIVE_PER_USER = 1      # control 14 — per beta user, initially

#: States in which a beta runtime HOLDS a pool slot (reserved through running/recovering/stopping).
#: BLOCKED does NOT hold a slot (it is blocked *because* no slot was available). Terminal/idle states
#: (NOT_PROVISIONED, STOPPED, DEPROVISIONING, REMOVED, FAILED) release the slot.
HELD_STATES = frozenset({
    RuntimeState.QUEUED, RuntimeState.PROVISIONING, RuntimeState.STARTING,
    RuntimeState.AUTHENTICATING, RuntimeState.RUNNING, RuntimeState.DEGRADED,
    RuntimeState.REPAIRING, RuntimeState.STOPPING,
})


class CapacityError(Exception):
    """Raised when a beta runtime slot cannot be reserved. ``reason_code`` is user-safe/sanitised."""
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def beta_runtimes_enabled() -> bool:
    """Master beta-runtime switch = the control-16 kill switch. **DEFAULT OFF.**
    False ⇒ no new beta reservations succeed (and the operator/watchdog stops beta runtimes). Because
    this only ever gates ``cohort=BETA``, it can never stop Nuno's ``PRODUCTION`` runtimes."""
    val = getattr(settings, "BETA_RUNTIMES_ENABLED", None)
    if val is None:
        val = os.getenv("BETA_RUNTIMES_ENABLED", "")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


# ── Production resource reserve (control 15) — pluggable host-pressure admission ──
# A later increment (host provisioner/watchdog) registers a real callable that reads live host metrics
# (RAM/CPU/disk/bridge-health) and returns False when the protected production reserve would be breached.
_host_capacity_probe: Optional[Callable[[], bool]] = None


def register_host_capacity_probe(fn: Callable[[], bool]) -> None:
    global _host_capacity_probe
    _host_capacity_probe = fn


def host_has_capacity() -> bool:
    """True if the host is below the protected-production-reserve pressure thresholds. Until the host
    metric probe is registered (later increment) this returns True — safe because ``beta_runtimes_enabled``
    is False by default, so nothing provisions regardless."""
    if _host_capacity_probe is None:
        return True
    try:
        return bool(_host_capacity_probe())
    except Exception:
        return False  # fail closed: if we cannot measure host pressure, do not admit


def active_beta_runtime_count() -> int:
    return AccountRuntime.objects.filter(
        cohort=AccountRuntime.Cohort.BETA, state__in=HELD_STATES).count()


def active_beta_runtime_count_for_user(user) -> int:
    return AccountRuntime.objects.filter(
        cohort=AccountRuntime.Cohort.BETA, trading_account__user=user,
        state__in=HELD_STATES).count()


def _require_beta(rt: AccountRuntime) -> None:
    """Structural guard (control 14): refuse to mutate a non-BETA runtime. This makes 'never touch
    Nuno's production runtime' an invariant of the mutators themselves, not a caller responsibility."""
    if rt.cohort != AccountRuntime.Cohort.BETA:
        raise ValueError("refusing to mutate a non-BETA (production) AccountRuntime")


def get_or_create_beta_runtime(account) -> AccountRuntime:
    """Ensure ``account`` owns a BETA ``AccountRuntime`` with a server-generated canonical runtime_root
    and bridge identity (controls 1/4/6). Never converts an existing PRODUCTION runtime to BETA — an
    account that already carries Nuno's production runtime is returned unchanged (and reservation will
    reject it as ``not_a_beta_runtime``). ``get_or_create`` is race-safe on the 1:1 account key."""
    rt, _ = AccountRuntime.objects.get_or_create(
        trading_account=account, defaults={"cohort": AccountRuntime.Cohort.BETA})
    if rt.cohort == AccountRuntime.Cohort.BETA:
        changed = []
        if not rt.runtime_root:
            rt.runtime_root = canonical_beta_runtime_root(rt.runtime_uuid)
            changed.append("runtime_root")
        if not rt.bridge_identity:
            rt.bridge_identity = "brt-" + rt.runtime_uuid.hex[:16]
            changed.append("bridge_identity")
        if changed:
            rt.save(update_fields=changed + ["updated_at"])
    return rt


def reserve_beta_slot(account) -> AccountRuntime:
    """Reserve a beta pool slot for ``account`` and move its runtime to QUEUED, or raise
    ``CapacityError``. The cap check AND the resulting transition (QUEUED on success, BLOCKED on a
    capacity denial) both run atomically **inside** the global ``BetaCapacityLock`` — so two
    concurrent reservations cannot both pass the cap, and a denial can never clobber a slot another
    reservation grants for the same account in the same instant."""
    if not beta_runtimes_enabled():
        raise CapacityError("beta_runtimes_disabled")

    # Ensure the BETA runtime + canonical path exist and PERSIST (own transaction, before the lock).
    get_or_create_beta_runtime(account)
    BetaCapacityLock.objects.get_or_create(pk=1)

    denial = None
    with transaction.atomic():
        BetaCapacityLock.objects.select_for_update().get(pk=1)  # global serialise
        rt = AccountRuntime.objects.select_for_update().get(trading_account=account)
        if rt.cohort != AccountRuntime.Cohort.BETA:
            raise CapacityError("not_a_beta_runtime")   # nothing to persist; rollback is fine
        if rt.quarantined:
            raise CapacityError("runtime_quarantined")
        if rt.state in HELD_STATES:
            return rt  # idempotent — already holds a slot (never re-validate/re-block a live runtime)
        # Broker-record validation via the provider abstraction — BROKER-INDEPENDENT (format/completeness
        # only, no connectivity), provider-driven (the MT5 provider is resolved, not special-cased) and
        # fail-closed. Runs AFTER the idempotency check (so a slot-holding runtime is never demoted) and
        # INSIDE the lock (so the resulting BLOCKED write commits with the decision, like any denial).
        from trading.brokers import get_broker_validator
        if not get_broker_validator(account).validate_account_record(account).ok:
            denial = "broker_record_invalid"
        elif active_beta_runtime_count_for_user(account.user) >= BETA_MAX_ACTIVE_PER_USER:
            denial = "per_user_runtime_cap"
        elif active_beta_runtime_count() >= BETA_MAX_ACTIVE_RUNTIMES:
            denial = "beta_pool_full"
        elif not host_has_capacity():
            denial = "host_at_capacity"
        if denial is None:
            return record_transition(rt, RuntimeState.QUEUED, reason_code="slot_reserved")
        # Denial: record the truthful BLOCKED state INSIDE the lock (committed with the decision).
        _block(rt, denial)
    raise CapacityError(denial)


def _block(rt: AccountRuntime, reason: str) -> None:
    """Move an un-admitted runtime to the truthful BLOCKED state (does not hold a slot)."""
    _require_beta(rt)
    if rt.state != RuntimeState.BLOCKED:
        record_transition(rt, RuntimeState.BLOCKED, reason_code=reason)


def release_beta_slot(rt: AccountRuntime, *, reason: str = "released") -> AccountRuntime:
    """Release a held slot by stopping the runtime (control 17 stop). No-op if already released."""
    _require_beta(rt)
    if rt.state in HELD_STATES:
        return record_transition(rt, RuntimeState.STOPPED, reason_code=reason)
    return rt


def quarantine_runtime(rt: AccountRuntime, *, reason: str) -> None:
    """Quarantine a runtime (control 17): stop it and mark it non-re-provisionable until cleared."""
    _require_beta(rt)
    rt.quarantined = True
    rt.quarantine_reason = (reason or "")[:64]
    rt.save(update_fields=["quarantined", "quarantine_reason", "updated_at"])
    if rt.state in HELD_STATES:
        record_transition(rt, RuntimeState.STOPPED, reason_code="quarantined")


def clear_quarantine(rt: AccountRuntime) -> None:
    _require_beta(rt)
    rt.quarantined = False
    rt.quarantine_reason = ""
    rt.save(update_fields=["quarantined", "quarantine_reason", "updated_at"])
