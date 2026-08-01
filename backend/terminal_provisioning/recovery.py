"""Customer Zero orphan-reclaim + failed-runtime recovery — governed operator helpers (ADR-0024).

Two SEPARATE operator-gated phases, shared gate-free helpers here:

* **Phase 1 — reclaim** (``reclaim_beta_runtime``): reclaim a BETA runtime's orphaned agent slot occupancy by
  driving the signed **STOP -> TOMBSTONE -> RELEASE** lifecycle, advancing the slot's generation and returning
  it to Available. Every op is driven through ``provisioner._step`` so the deployed PR#252 timeout / in-attempt
  reconcile governance applies unchanged. **Never** touches ``slots.sqlite`` / the host filesystem directly.
* **Phase 2 — recover** (``recover_beta_runtime``): move the backend runtime from a terminal state to a
  retryable one and create **exactly one** claimable ``PROVISION`` job (the prior failed job is retained as
  history). Pure backend — no agent contact, provisioner stays DARK.

Phase 3 (arming + the actual retry to RUNNING) is a later, separately-authorised operation — nothing here
arms the provisioner or advances a job.

CRITICAL SAFETY (agent idempotency): the agent keys idempotency on ``(job_id, operation)``. A re-send of STOP
under a NEW job_id AFTER a TOMBSTONE removed the slot's owner marker would hit the integrity gate and
QUARANTINE the slot. So the whole reclaim sequence — and every re-invocation — MUST reuse a **stable job_id**
(defaulting to the runtime's retained PROVISION job). This module makes that the caller's explicit input.
"""
import itertools
import time

from django.db import transaction

from .beta_capacity import (
    HELD_STATES, _require_beta, beta_runtimes_enabled, clear_quarantine, quarantine_runtime)
from .models import AccountRuntime, ProvisioningJob, RuntimeState
from .runtime_state import record_transition

#: States from which reclaiming an orphaned agent occupancy is sensible (a runtime that is NOT holding a live,
#: healthy slot in the normal flow). RUNNING / STARTING / etc. are deliberately excluded — those are driven by
#: the normal lifecycle, not the operator reclaim tool.
RECLAIMABLE_STATES = frozenset({
    RuntimeState.FAILED, RuntimeState.STOPPED, RuntimeState.REMOVED,
    RuntimeState.BLOCKED, RuntimeState.DEGRADED,
})
_ACTIVE_JOB = (ProvisioningJob.Status.QUEUED, ProvisioningJob.Status.RUNNING)


class ReclaimError(Exception):
    """A reclaim/recovery precondition or step failed. ``reason_code`` is user-safe/sanitised."""
    def __init__(self, reason_code: str, *, detail: str = ""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(reason_code)


# ── resolution + guards (all fail-closed) ──────────────────────────────────────────────────────────────────
def resolve_runtime(*, runtime_uuid=None, account_runtime_id=None) -> AccountRuntime:
    """Resolve EXACTLY ONE runtime from a uuid XOR an AccountRuntime id. Ambiguous/missing input fails closed."""
    if bool(runtime_uuid) == bool(account_runtime_id):
        raise ReclaimError("ambiguous_selector", detail="pass exactly one of runtime_uuid / account_runtime_id")
    qs = AccountRuntime.objects.filter(
        **({"runtime_uuid": runtime_uuid} if runtime_uuid else {"pk": account_runtime_id}))
    rt = qs.first()
    if rt is None:
        raise ReclaimError("runtime_not_found")
    return rt


def assert_beta(rt: AccountRuntime) -> None:
    """Refuse anything that is not a BETA runtime — a PRODUCTION (Nuno) runtime can never be reclaimed."""
    if rt.cohort != AccountRuntime.Cohort.BETA:
        raise ReclaimError("not_a_beta_runtime")


def assert_no_active_job(rt: AccountRuntime) -> None:
    """Refuse if a worker may already own a job for this runtime (never race the autonomous worker)."""
    if ProvisioningJob.objects.filter(runtime=rt, status__in=_ACTIVE_JOB).exists():
        raise ReclaimError("active_job_present")


def assert_dark_or_allow(*, allow_armed: bool) -> None:
    """Keep the pool DARK during a manual reclaim unless the operator explicitly opts in — an armed worker
    could act on the runtime concurrently. (A signed operator op is not itself gated by the flag.)"""
    if beta_runtimes_enabled() and not allow_armed:
        raise ReclaimError("provisioner_not_dark")


def assert_probe_matches(rt: AccountRuntime, probe: dict, *, expect_slot=None, expect_generation=None,
                         allow_running: bool = False) -> None:
    """Fail closed unless the agent's live occupancy probe matches the runtime we intend to reclaim: same
    runtime UUID, the expected slot + generation (when asserted), and no live process (unless overridden)."""
    if str(probe.get("runtime_uuid") or "") not in ("", str(rt.runtime_uuid)):
        raise ReclaimError("uuid_mismatch")
    if expect_slot is not None and probe.get("slot") not in (None, int(expect_slot)):
        raise ReclaimError("slot_mismatch")
    if expect_generation is not None and probe.get("generation") not in (None, int(expect_generation)):
        raise ReclaimError("generation_mismatch")
    if probe.get("running") and not allow_running:
        raise ReclaimError("runtime_process_present")


def stable_reclaim_job_id(rt: AccountRuntime) -> int:
    """The runtime's retained PROVISION job id — the stable idempotency anchor the reclaim sequence reuses.
    Reusing it makes STOP/TOMBSTONE/RELEASE re-sends replay the agent's stored (job_id, op) result rather than
    re-executing (which, for STOP after a TOMBSTONE, would quarantine the slot)."""
    job = (ProvisioningJob.objects.filter(runtime=rt, op=ProvisioningJob.Op.PROVISION)
           .order_by("id").first())
    if job is None:
        raise ReclaimError("no_provision_job_anchor",
                           detail="no PROVISION job exists to anchor idempotency; pass --job-id explicitly")
    return job.id


# ── Phase 1: drive the signed STOP -> TOMBSTONE -> RELEASE (through PR#252 _step) ──────────────────────────
def make_reclaim_client(*, job_id: int, correlation_id: str = ""):
    """Build the signed-channel client bound to the STABLE job_id, negotiate the versioned contract, and
    REFUSE unless the agent advertises RELEASE (an agent predating RELEASE cannot complete the lifecycle)."""
    from .beta_worker import make_http_transport
    from .mgmt_client import AgentWindowsProvisioner
    client = AgentWindowsProvisioner(job_id=job_id, transport=make_http_transport(),
                                     correlation_id=correlation_id or f"reclaim-job-{job_id}")
    info = client.assert_compatible()   # NEGOTIATE + protocol/ops/version checks (raises on mismatch)
    if "RELEASE" not in set(info.get("supported_operations") or []):
        raise ReclaimError("agent_release_unsupported")
    return client


def make_probe_client(*, correlation_id: str = ""):
    """A client for the read-only occupancy probe ONLY. It MUST NOT reuse the mutation/idempotency anchor:
    the agent memoises every op (incl. VERIFY) on ``(job_id, operation)`` durably, so a probe under a job_id
    that ever carried a VERIFY (e.g. the runtime's provisioning-time VERIFY) would replay a STALE snapshot.
    A fresh, single-use probe job_id guarantees a LIVE agent read every time. VERIFY resolves the occupancy by
    the runtime UUID, not the job_id, so a throwaway id is correct."""
    from .beta_worker import make_http_transport
    from .mgmt_client import AgentWindowsProvisioner
    probe_job_id = _fresh_probe_job_id()
    return AgentWindowsProvisioner(job_id=probe_job_id, transport=make_http_transport(),
                                   correlation_id=correlation_id or f"reclaim-probe-{probe_job_id}")


_probe_seq = itertools.count()


def _fresh_probe_job_id() -> int:
    """A large, single-use id in a dedicated probe namespace — never collides with a real (small) job id and
    is unique per call, so the agent never replays a memoised VERIFY for it. Uses the FULL nanosecond clock
    (NOT ``time_ns() % 1e9``, which kept only the sub-second component and could repeat across two runs that
    landed at the same fractional second → a stale VERIFY snapshot) plus a process-local counter so two probes
    in the same process are distinct even at identical clock resolution. The value is only ever a signed-envelope
    idempotency key (``sign_request`` accepts any int and it is never persisted), so its magnitude is irrelevant."""
    return 2_000_000_000 + time.time_ns() + next(_probe_seq)


def drive_reclaim_op(rt, fn, reason_code, *, max_attempts=None):
    """Drive ONE agent op through ``provisioner._step`` (so PR#252's in-attempt reconcile for a timeout /
    same-runtime ``runtime_busy`` applies), with a bounded OUTER retry for a genuine *retryable* channel error
    (idempotent because the job_id is stable). Fail CLOSED on a proven-partial / non-retryable / budget-exhausted
    ambiguous error — never keep hammering, never fabricate success."""
    from . import provisioner as prov
    attempts = max_attempts or prov.MAX_ATTEMPTS
    last = None
    for i in range(1, attempts + 1):
        try:
            return prov._step(rt, fn, reason_code)
        except prov.ProvisionStepError as e:
            last = e
            if e.ambiguous or not e.retryable:
                raise                       # fail closed: unresolvable / proven failure
            if i < attempts:
                prov._reconcile_sleep(min(prov._RECONCILE_BACKOFF_START * i, prov._RECONCILE_BACKOFF_MAX))
                continue                    # bounded backoff, then idempotent resend under the stable job_id
            raise
    raise last  # pragma: no cover


def drive_reclaim_sequence(rt, client) -> dict:
    """STOP -> TOMBSTONE -> RELEASE. RELEASE is only reached if STOP and TOMBSTONE succeeded, so a partial
    teardown never advances the generation. Returns the RELEASE result ({released, available, slot, generation,
    occupancy_id})."""
    from . import provisioner as prov
    drive_reclaim_op(rt, lambda: client.stop(rt), "stop_failed")
    drive_reclaim_op(rt, lambda: client.teardown(rt), "teardown_failed")   # TOMBSTONE
    try:
        return drive_reclaim_op(rt, lambda: client.release(rt), "release_failed")
    except prov.ProvisionStepError as e:
        # RELEASE runs OUTSIDE the agent per-runtime lock, so unlike STOP/TOMBSTONE a reconcile re-send is not
        # serialised. Once a RELEASE has committed the slot is unassigned, so a resend deterministically
        # resolves to ``runtime_not_assigned`` — that is idempotent SUCCESS (the slot IS released), never a
        # failure/quarantine. (A rarer post-advance ``slot_integrity_mismatch`` remains fail-closed → a
        # reversible quarantine, never state corruption; the generation advanced exactly once.)
        if e.reason_code == "runtime_not_assigned":
            return {"released": True, "available": True, "slot": None, "generation": None,
                    "occupancy_id": None, "idempotent": True}
        raise


def mark_reclaimed(rt: AccountRuntime) -> AccountRuntime:
    """After a proven RELEASE, record the honest post-reclaim backend state: REMOVED (the runtime holds no slot;
    nothing is materialised). Immutable RuntimeEvent evidence, correlated to the reclaim."""
    return record_transition(rt, RuntimeState.REMOVED, event_type="RECLAIM", reason_code="slot_reclaimed")


def quarantine_on_reclaim_failure(rt: AccountRuntime, reason_code: str) -> None:
    """A reclaim step failed unrecoverably — record an IMMUTABLE FAILURE RuntimeEvent (the failure path is the
    one that most needs a durable audit trail; ``quarantine_runtime`` alone only appends an event for HELD
    states, so a FAILED/STOPPED/… reclaim would otherwise leave the immutable chain empty), then quarantine
    (blocks re-provisioning until cleared). DO NOT mark REMOVED — the agent occupancy state is unknown/partial."""
    code = (reason_code or "reclaim_failed")[:64]
    record_transition(rt, rt.state, event_type="FAILURE", reason_code="reclaim_failed",
                      detail=f"reclaim step failed: {code}")
    quarantine_runtime(rt, reason=code)


# ── Phase 2: backend failed-state recovery -> exactly one claimable PROVISION job ──────────────────────────
def recover_to_provisionable(rt: AccountRuntime, *, require_removed: bool = True) -> ProvisioningJob:
    """Move a reclaimed BETA runtime back to a retryable state and enqueue EXACTLY ONE ``PROVISION`` job. The
    prior FAILED job is retained as history. Serialised on the runtime row; provisioner stays DARK (the job is
    inert until a separate arming). Idempotent: a re-run when an active PROVISION job already exists returns the
    winner and never creates a duplicate."""
    from .provisioner import enqueue_op
    with transaction.atomic():
        locked = AccountRuntime.objects.select_for_update().get(pk=rt.pk)
        _require_beta(locked)
        # An active NON-PROVISION job (e.g. a QUEUED DEPROVISION) must block — a worker may own it, and the
        # per-(runtime, op) unique constraint would otherwise let a second-op job coexist.
        active = ProvisioningJob.objects.filter(runtime=locked, status__in=_ACTIVE_JOB)
        if active.exclude(op=ProvisioningJob.Op.PROVISION).exists():
            raise ReclaimError("active_non_provision_job")
        # Idempotent short-circuit: if an active PROVISION job already exists this runtime is already prepared
        # — return it regardless of the current state (a re-run after a prior recover left it NOT_PROVISIONED
        # must not trip the require-REMOVED guard).
        existing = active.filter(op=ProvisioningJob.Op.PROVISION).order_by("id").first()
        if existing is not None:
            return existing
        # NEVER reset a HELD / live runtime — that would drop it from capacity accounting while its agent slot
        # is still assigned and its terminal running (an orphan/desync). ``--force-from-failed`` widens the
        # floor to FAILED, never to a live state.
        if locked.state in HELD_STATES:
            raise ReclaimError("runtime_is_held",
                               detail=f"state={locked.state} is live/held; stop + reclaim it first")
        if require_removed and locked.state != RuntimeState.REMOVED:
            raise ReclaimError("not_removed",
                               detail="run reclaim first (Phase 1) or pass --force-from-failed")
        clear_quarantine(locked)                             # idempotent; reserve gate refuses a quarantined rt
        locked = record_transition(locked, RuntimeState.NOT_PROVISIONED,
                                   event_type="RECOVER", reason_code="recover_reset")
        job = enqueue_op(locked, ProvisioningJob.Op.PROVISION)
    # POST-ASSERT the "exactly one claimable job" invariant (proven, not assumed).
    active = ProvisioningJob.objects.filter(
        runtime=rt, op=ProvisioningJob.Op.PROVISION, status__in=_ACTIVE_JOB).count()
    if active != 1:
        raise ReclaimError("exactly_one_job_violated", detail=f"active PROVISION jobs = {active}")
    return job
