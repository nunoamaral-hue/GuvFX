"""GFX-BETA-HEADLESS Increment 2 — provisioning executor (driver + Windows provisioner interface).

Drives one BETA ``AccountRuntime`` through the durable state machine by executing a ``ProvisioningJob``.
Enqueue-only, idempotent, persist-then-act, bounded retries, immutable ``RuntimeEvent`` evidence, and —
critically — it **never reports RUNNING before the runtime is verified logged in to its OWN assigned
broker account** (compensating control 8), and it **never places an order** (this is provisioning, not
execution). Credentials are decrypted transiently and handed to the provisioner via an argument that a
real provisioner injects over an authenticated channel — never into a command line, URL, or log
(control 10). Nuno's PRODUCTION runtimes are refused (control 14).
"""
import contextvars
import logging
import os
import time
from typing import Protocol

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from trading.crypto import decrypt_password
from core.audit import log_customer_credential_event

from .beta_activation import ActivationDenied, assert_beta_activation_allowed
from .beta_capacity import CapacityError, _require_beta, reserve_beta_slot
from .mgmt_client import ManagementChannelError, ManagementChannelTimeout
from .models import AccountRuntime, ProvisioningJob, RuntimeState
from .runtime_state import record_transition

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# Raised from 300 (Customer Zero 2026-08-01): a single PROVISION attempt runs materialise + start + verify,
# each of which may enter the in-attempt reconcile below, and the lease MUST outlast the whole attempt's
# worst-case wall-clock so a job still legitimately in flight is never re-claimed by another worker (which
# would fire a concurrent, colliding op). The honest bound is enforced by ``assert_lease_covers_op_timeouts``
# (also at worker startup + CI ``tests_beta_worker_timeouts.LeaseCouplingTests``).
LEASE_TTL_SECONDS = 1500

# ── Ambiguous long-operation reconcile (poll-not-repost) — Customer Zero remediation ──
# The MATERIALISE golden copy can exceed even a right-sized transport read timeout on a slow host. Rather
# than blind-re-POSTing (which hammers the still-held per-runtime lock into ``runtime_busy`` and burns the
# retry budget in milliseconds — the Customer Zero incident), an ambiguous timeout OR the SAME runtime's
# ``runtime_busy`` is reconciled IN the same attempt: WAIT, then re-send the SAME idempotent (job_id, op);
# the agent returns its stored result once the op completes. Bounded by a wall-clock budget (long for the big
# MATERIALISE copy, short for start/verify) — never an indefinite wait.
PROVISIONING_MATERIALISE_MAX_WAIT_SECONDS = 300
_RECONCILE_SHORT_WAIT_SECONDS = 60            # non-materialise ops don't do a big copy; keep their wait short
_RECONCILE_BACKOFF_START = 5
_RECONCILE_BACKOFF_MAX = 30

#: ONLY ``runtime_busy`` triggers the in-attempt reconcile from ``_step`` — it is the sole reply that is
#: POSITIVE evidence THIS runtime's op holds the per-runtime lock (genuinely in flight). ``agent_busy`` (the
#: agent's GLOBAL mutation semaphore, saturated by OTHER runtimes) and ``agent_stopping`` (drain) are raised
#: BEFORE this op runs — nothing was mutated — so they are ordinary retryable channel errors that re-queue,
#: never a reconcile→quarantine of an op that never started.
RECONCILE_BUSY_REASONS = frozenset({"runtime_busy"})
#: Once already reconciling (this op WAS in flight), a re-probe returning any of these is NOT resolution — the
#: original op may still be running — so keep waiting within the budget rather than bailing to a false failure.
_RECONCILE_CONTINUE_REASONS = frozenset({
    "runtime_busy", "agent_busy", "agent_stopping", "transport_error", "bad_agent_response",
})
#: A PROVEN-partial / integrity / containment refusal from the agent on a mutating op. Definitive (a re-drive
#: won't fix it and the slot may be corrupt/escaped): fail closed AND quarantine — a partial or escaped slot
#: must never be silently re-materialised as success or blindly re-driven. Derived from the SLOT_POOL agent's
#: INTEGRITY reason codes (deploy/beta-agent/lifecycle.py) reachable on MATERIALISE — NOT a hand-picked subset;
#: the live pool agent emits ``reparse_escape`` / ``slot_integrity_mismatch`` (the legacy uuid_dir model emits
#: ``reparse_escape_after_materialise``). Coupling asserted by tests so it cannot silently fall behind.
PARTIAL_REASONS = frozenset({
    # stage-copy partial / refusal
    "stage_copy_incomplete", "stage_copy_precheck_failed", "stage_copy_refused",
    # integrity + containment refusals (INTEGRITY category) reachable on the MATERIALISE path
    "impl_integrity_mismatch", "path_escape",
    "reparse_escape", "reparse_escape_after_materialise", "reparse_escapes_namespace",
    "reparse_point_present", "reparse_point_in_tree",
    "image_outside_slot", "image_not_owned", "slot_integrity_mismatch", "audit_chain_corrupt",
    "unauthorised_namespace", "capability_violation", "occupancy_binding_mismatch",
})

# Injectable for tests so the reconcile budget is consumed deterministically with no real sleeping / wall
# clock: tests replace both with a fake clock (sleep advances the clock the caller reads).
_reconcile_sleep = time.sleep
_reconcile_now = time.monotonic


def _materialise_max_wait() -> int:
    """The MATERIALISE reconcile budget (settings-overridable), parsed defensively — a malformed value falls
    back to the module default rather than raising out of the caller (mirrors ``_op_read_timeout``)."""
    try:
        return int(getattr(settings, "PROVISIONING_MATERIALISE_MAX_WAIT_SECONDS",
                           PROVISIONING_MATERIALISE_MAX_WAIT_SECONDS))
    except (TypeError, ValueError):
        return PROVISIONING_MATERIALISE_MAX_WAIT_SECONDS


def _reconcile_budget(reason_code: str) -> int:
    """Wall-clock reconcile budget for the step: the long MATERIALISE budget only for the copy, a short budget
    for start/verify (which do no big copy). Keeps the worst-case attempt bounded under the lease."""
    return _materialise_max_wait() if reason_code == "materialise_failed" else _RECONCILE_SHORT_WAIT_SECONDS


def assert_lease_covers_op_timeouts() -> None:
    """Fail-closed coupling guard: the job lease MUST outlast the HONEST worst-case single PROVISION attempt —
    materialise + start + verify each (read timeout + their reconcile budget), plus one trailing full-read
    overshoot (a probe may start just under the deadline and block a full read). Else a long-but-healthy
    attempt lets the lease expire and a second worker re-claims + re-fires an op. Uses an explicit raise (not a
    bare ``assert``) so it survives ``python -O``; a future timeout bump that breaks the coupling fails here at
    worker startup and in CI (``tests_beta_worker_timeouts.LeaseCouplingTests``)."""
    from .beta_worker import _op_read_timeout
    materialise = _op_read_timeout("MATERIALISE")
    full, short = _reconcile_budget("materialise_failed"), _reconcile_budget("start_failed")
    required = ((materialise + full) + (_op_read_timeout("START") + short)
                + (_op_read_timeout("VERIFY") + short) + materialise)   # + trailing full-read overshoot
    if LEASE_TTL_SECONDS <= required:
        raise AssertionError(
            f"LEASE_TTL_SECONDS ({LEASE_TTL_SECONDS}) must exceed the worst-case PROVISION attempt "
            f"(materialise+start+verify read+reconcile + overshoot) = {required}")

# ADR-0021 — optional per-step heartbeat. The worker sets this so the durable liveness heartbeat is
# refreshed after EVERY provisioning side-effect (materialise/start/verify/…), so a genuinely long
# provisioning run keeps proving liveness (PROCESSING) and never reads stale mid-stage. Default None
# (no-op) — the driver is fully usable without a worker/heartbeat.
_STEP_HEARTBEAT: "contextvars.ContextVar" = contextvars.ContextVar("provisioner_step_heartbeat", default=None)


def _require_broker_login() -> bool:
    """Whether provisioning must verify a live broker LOGIN before a runtime reaches RUNNING.

    **DEFAULT OFF** — the broker-INDEPENDENT phase: a runtime reaches RUNNING on process/session
    verification alone and its Verification Report records ``broker_login_verified=False``. Broker-login
    verification is a distinct, LATER stage; flipping this ON (once a disposable demo broker account
    exists) restores the strict identity/login fail-closed checks (control 8). Because it only ever
    gates BETA provisioning, it can never affect Nuno's PRODUCTION runtimes."""
    val = getattr(settings, "PROVISIONING_REQUIRE_BROKER_LOGIN", None)
    if val is None:
        val = os.getenv("PROVISIONING_REQUIRE_BROKER_LOGIN", "")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


class ProvisionStepError(Exception):
    """A provisioning step failed. ``reason_code`` is user-safe; ``detail`` is admin-only; ``retryable``
    controls whether the driver retries (bad credentials, for example, are NOT retryable). ``ambiguous``
    marks a channel TIMEOUT — the op MAY have executed; the driver must never treat it as proof of
    failure, and on repeated ambiguity it quarantines rather than re-launching (requirement 9)."""
    def __init__(self, reason_code: str, *, detail: str = "", retryable: bool = True,
                 ambiguous: bool = False):
        self.reason_code = reason_code
        self.detail = detail
        self.retryable = retryable
        self.ambiguous = ambiguous
        super().__init__(reason_code)


class WindowsProvisioner(Protocol):
    """The Windows-side capability the driver orchestrates. A real implementation talks to the box's
    beta-provisioner over the authenticated management channel; ``FakeProvisioner`` is used in tests."""
    def materialise(self, runtime: AccountRuntime) -> None: ...
    def configure(self, runtime: AccountRuntime, *, login: str, server: str, password: str) -> None: ...
    def start(self, runtime: AccountRuntime) -> None: ...
    # verify → {running, logged_in, login, server, is_demo, login_error?, init_error?}. When broker-login
    # is required (PR B): ``is_demo`` is the CONNECTED account's demo/live classification; ``login_error``
    # is the structured reason on a failed login (invalid_credentials / server_unavailable / timeout /
    # mt5_init_failed / terminal_crashed); ``init_error`` is set when the terminal never initialised.
    def verify(self, runtime: AccountRuntime) -> dict: ...
    def stop(self, runtime: AccountRuntime) -> None: ...
    def teardown(self, runtime: AccountRuntime) -> None: ...
    # RELEASE (ADR-0014): free the slot + advance its generation after a TOMBSTONE. Used only by the operator
    # reclaim path (never the normal PROVISION drive), so it is an OPTIONAL provisioner method.
    def release(self, runtime: AccountRuntime) -> dict: ...


def _expected_login_server(runtime: AccountRuntime):
    """Return (login, server) the runtime MUST authenticate to. ``login`` (the MT5 account number) is
    the strong identity and is always verified exactly. ``server`` is the MT5 server string only when a
    normalised ``broker_server`` is set — free-text ``broker_name`` is NOT the MT5 server name, so we
    return ``None`` there and skip the (unreliable) server comparison rather than false-block a login."""
    acct = runtime.trading_account
    login = str(acct.account_number)
    if getattr(acct, "broker_server_id", None):
        return login, (acct.broker_server.server_name or "").strip()
    return login, None


# ADR-0021 PR B — broker-login failure taxonomy. The verify result carries a structured ``login_error``
# reason when ``logged_in`` is False; each maps to a durable, sanitised runtime failure code and a
# retry policy. A **retryable** failure is transient (server/agent/timeout) and re-attempted up to
# MAX_ATTEMPTS; a **non-retryable** failure is a definitive rejection (bad credentials / wrong account /
# demo-live mismatch) that fails the job immediately so a customer never loops on unfixable input.
_LOGIN_FAILURE = {
    "invalid_credentials": ("broker_login_failed", False),
    "server_unavailable":  ("broker_server_unavailable", True),
    "timeout":             ("broker_login_timeout", True),
    "mt5_init_failed":     ("mt5_init_failed", True),
    "terminal_crashed":    ("terminal_crashed", True),
}


def _login_failure(reason):
    """Map a verify ``login_error`` reason to ``(durable_code, retryable)``. Unknown / missing ⇒ treat as
    a definitive credential rejection (non-retryable) so an unrecognised login failure never loops."""
    return _LOGIN_FAILURE.get(reason or "", ("broker_login_failed", False))


def _broker_classification_matches(account, v: dict) -> bool:
    """PR B — the CONNECTED account's demo/live classification must match the DECLARED ``is_demo``. The
    agent reports the connected classification as a **genuine boolean** ``is_demo`` (derived from the MT5
    ``trade_mode``: DEMO/CONTEST ⇒ demo, REAL ⇒ live). FAIL CLOSED on anything that is not a real bool —
    a missing key, JSON ``null`` (agent could not determine), or a non-boolean value is UNVERIFIED and
    must never pass (strict identity, not truthiness — ``bool(None)==bool(False)`` would wrongly pass a
    live account with an undetermined classification)."""
    reported = v.get("is_demo")
    if not isinstance(reported, bool):
        return False
    return reported == bool(getattr(account, "is_demo", False))


# ── Enqueue (enqueue-only: callers create jobs; a worker advances them) ──
def enqueue_op(runtime: AccountRuntime, op: str) -> ProvisioningJob:
    """Enqueue ONE provisioning job — idempotent under the ``uniq_active_job_per_runtime_op`` invariant.
    A concurrent identical enqueue that lost the race raises IntegrityError; we recover the winning active
    job and return it, so a caller never stacks a duplicate and never sees a 500."""
    from django.db import IntegrityError
    _require_beta(runtime)
    try:
        with transaction.atomic():   # savepoint — a unique violation here must not poison the outer tx
            return ProvisioningJob.objects.create(runtime=runtime, op=op)
    except IntegrityError:
        existing = (ProvisioningJob.objects
                    .filter(runtime=runtime, op=op,
                            status__in=[ProvisioningJob.Status.QUEUED, ProvisioningJob.Status.RUNNING])
                    .order_by("id").first())
        if existing is None:
            raise   # a different integrity error — surface it
        return existing


# ── Driver ──
def advance_provisioning_job(job: ProvisioningJob, provisioner: WindowsProvisioner,
                             heartbeat=None) -> ProvisioningJob:
    """Claim (lease) and advance a job. Idempotent + resumable: dispatch is by the runtime's durable
    state, so a re-claimed job continues from where it left off and never repeats a completed step.

    ``heartbeat`` (optional): a no-arg callable invoked after each provisioning step, so a long-running
    job keeps the durable liveness heartbeat fresh. Failures in the callback never affect provisioning."""
    hb_token = _STEP_HEARTBEAT.set(heartbeat)
    try:
        return _advance_provisioning_job_inner(job, provisioner)
    finally:
        _STEP_HEARTBEAT.reset(hb_token)


def _advance_provisioning_job_inner(job: ProvisioningJob, provisioner: WindowsProvisioner) -> ProvisioningJob:
    # Single-flight claim: only one worker may hold a job at a time. ``attempt`` is incremented at
    # CLAIM time so a hard worker crash (re-claimed on lease expiry) is bounded by MAX_ATTEMPTS too —
    # not just clean ProvisionStepError failures.
    with transaction.atomic():
        j = ProvisioningJob.objects.select_for_update().get(pk=job.pk)
        if j.status in (ProvisioningJob.Status.DONE, ProvisioningJob.Status.FAILED):
            return j
        now = timezone.now()
        if j.status == ProvisioningJob.Status.RUNNING and j.lease_expires_at and j.lease_expires_at > now:
            return j  # a live worker already holds the lease
        j.attempt += 1
        if j.attempt > MAX_ATTEMPTS:
            j.status = ProvisioningJob.Status.FAILED
            j.finished_at = now
            j.lease_expires_at = None
            j.last_error = "attempts_exhausted"
            j.save(update_fields=["status", "attempt", "finished_at", "lease_expires_at", "last_error"])
            _fail_runtime(j.runtime_id, "attempts_exhausted")
            return j
        j.status = ProvisioningJob.Status.RUNNING
        j.started_at = j.started_at or now
        j.lease_expires_at = now + timezone.timedelta(seconds=LEASE_TTL_SECONDS)
        j.save(update_fields=["status", "attempt", "started_at", "lease_expires_at"])

    rt = AccountRuntime.objects.get(pk=j.runtime_id)
    try:
        _require_beta(rt)   # never act on a PRODUCTION runtime, even if a job slipped through
        if j.op == ProvisioningJob.Op.PROVISION:
            _drive_provision(rt, provisioner)
        elif j.op == ProvisioningJob.Op.START:
            _drive_start(rt, provisioner)
        elif j.op == ProvisioningJob.Op.STOP:
            _drive_stop(rt, provisioner)
        elif j.op == ProvisioningJob.Op.DEPROVISION:
            _drive_deprovision(rt, provisioner)
    except ProvisionStepError as e:
        return _fail_step(j, rt, e)
    except CapacityError as e:
        # A capacity/kill-switch/quarantine denial is not a transient step error — the runtime is left
        # BLOCKED (or NOT_PROVISIONED for the disabled case) and the job fails truthfully with the reason.
        return _fail_terminal(j, e.reason_code)
    except ActivationDenied as e:
        # A narrow-activation denial (control 2) — refuse to launch; fail the job truthfully. No box work
        # was performed (the gate runs before any materialise/launch side-effect).
        return _fail_terminal(j, e.reason_code)
    except ValueError as e:
        return _fail_terminal(j, "invalid_runtime")

    j.status = ProvisioningJob.Status.DONE
    j.finished_at = timezone.now()
    j.lease_expires_at = None
    j.last_error = ""
    j.save(update_fields=["status", "finished_at", "lease_expires_at", "last_error"])
    return j


def _fire_step_heartbeat() -> None:
    """Refresh the durable liveness heartbeat between provisioning steps (ADR-0021). Never let a heartbeat
    error affect provisioning — liveness reporting is strictly best-effort."""
    cb = _STEP_HEARTBEAT.get()
    if cb is None:
        return
    try:
        cb()
    except Exception:  # noqa: BLE001
        pass


def _step(runtime, fn, reason_code):
    """Run one provisioner side-effect, converting any raw failure into a sanitised ProvisionStepError.
    On success the durable liveness heartbeat is refreshed (a long multi-step job never reads stale)."""
    try:
        result = fn()
        _fire_step_heartbeat()
        return result
    except ProvisionStepError:
        raise
    except ManagementChannelTimeout:
        # AMBIGUOUS — the op may still be running on the agent. Reconcile IN-attempt (poll-not-repost)
        # rather than blind-re-POSTing: the resend returns the agent's stored idempotent result once the op
        # completes; only a proven-partial or an exhausted wait budget fails. (Customer Zero remediation.)
        return _reconcile(fn, reason_code)
    except ManagementChannelError as exc:
        rc = getattr(exc, "reason_code", "") or ""
        if rc in RECONCILE_BUSY_REASONS:
            # runtime_busy ONLY: the ORIGINAL op still holds the per-runtime lock (genuinely in flight).
            # Reconcile, never re-POST — the exact reply the incident mis-classified as ``materialise_failed``.
            # (agent_busy / agent_stopping are raised BEFORE this op runs, so they fall through to a plain
            # retryable re-queue below — never a reconcile→quarantine of an op that never started.)
            return _reconcile(fn, reason_code)
        if rc in PARTIAL_REASONS:
            # PROVEN partial / integrity / containment refusal — fail closed, non-retryable (quarantined).
            raise ProvisionStepError(rc, detail="proven_partial", retryable=False)
        # Another channel error (transport_error, agent_busy, agent_stopping, agent_denied, …): retryable,
        # carrying the agent's own sanitised code so the operator sees what actually happened.
        raise ProvisionStepError(rc or reason_code, detail="channel_error", retryable=True)
    except Exception as exc:  # noqa: BLE001 — never leak a raw agent string to the user path
        raise ProvisionStepError(reason_code, detail=str(exc)[:2000], retryable=True)


def _reconcile(fn, reason_code):
    """Bounded in-attempt reconcile for a long op that WAS in flight (ambiguous timeout / same-runtime
    ``runtime_busy``): WAIT, then re-send the SAME idempotent (job_id, op) — the agent returns its stored
    result once the op completes, or busy/timeout while it is still running. NEVER re-POSTs before waiting;
    NEVER an indefinite wait (budget is long for MATERIALISE's copy, short for start/verify).

    Returns the agent result on completion. Raises ``ProvisionStepError``: non-retryable on a proven-partial
    (fail closed, quarantined by ``_fail_step``); retryable on a definitive channel error; ambiguous
    (→ quarantine) on wall-clock budget exhaustion — a deterministic terminal outcome, never a
    "safe to re-launch" FAILED."""
    budget = max(0, _reconcile_budget(reason_code))
    logger.info("provisioner: reconciling %s (in-attempt poll-not-repost, budget=%ss)", reason_code, budget)
    deadline = _reconcile_now() + budget
    backoff = _RECONCILE_BACKOFF_START
    while _reconcile_now() < deadline:
        _reconcile_sleep(backoff)          # WAIT before re-probing — never hammer the held lock
        _fire_step_heartbeat()             # keep the durable liveness heartbeat PROCESSING-fresh across the wait
        backoff = min(backoff * 2, _RECONCILE_BACKOFF_MAX)
        try:
            result = fn()                  # re-send the SAME signed (job_id, op) — fresh nonce, agent dedupes on (job_id, op)
        except ManagementChannelTimeout:
            continue                       # still copying → keep waiting
        except ManagementChannelError as exc:
            rc = getattr(exc, "reason_code", "") or ""
            if rc in _RECONCILE_CONTINUE_REASONS:
                continue                   # not resolution (busy / drain / transient transport) → keep waiting
            if rc in PARTIAL_REASONS:
                raise ProvisionStepError(rc, detail="proven_partial", retryable=False)
            raise ProvisionStepError(rc or reason_code, detail="channel_error", retryable=True)
        except ProvisionStepError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProvisionStepError(reason_code, detail=str(exc)[:2000], retryable=True)
        _fire_step_heartbeat()
        logger.info("provisioner: reconciled %s → resolved (agent returned a result)", reason_code)
        return result
    logger.warning("provisioner: reconcile budget exhausted for %s → ambiguous_timeout (quarantine)", reason_code)
    raise ProvisionStepError("op_ambiguous_timeout", detail="reconcile_budget_exhausted",
                             retryable=False, ambiguous=True)


def _start_and_verify(rt: AccountRuntime, p: WindowsProvisioner) -> None:
    """Shared STARTING → AUTHENTICATING → RUNNING path used by both PROVISION and START. A runtime only
    reaches RUNNING once its process is verified up in the right session. Resumable per state.

    Broker-login verification is a SEPARATE, later stage. In the broker-INDEPENDENT phase (the default;
    ``_require_broker_login()`` False) the runtime reaches RUNNING on process verification alone and its
    Verification Report records ``broker_login_verified=False`` — no broker connectivity is required.
    When ``PROVISIONING_REQUIRE_BROKER_LOGIN`` is ON, the runtime must additionally be logged in to its
    OWN assigned broker account, with an exact identity match, before RUNNING (control 8)."""
    if rt.state == RuntimeState.STARTING:
        _step(rt, lambda: p.start(rt), "start_failed")
        rt = record_transition(rt, RuntimeState.AUTHENTICATING, reason_code="started")
    if rt.state == RuntimeState.AUTHENTICATING:
        v = _step(rt, lambda: p.verify(rt), "verify_failed") or {}
        # Evaluate the mode ONCE for this verification, so the RUNNING gate and the report's
        # ``broker_login_verified`` can never disagree (no OFF→ON flip observed mid-check).
        require_login = _require_broker_login()
        # Process verification is ALWAYS required — a runtime is never reported RUNNING unless its
        # terminal is actually up. A reported MT5 initialisation failure is distinguished from a
        # still-starting terminal (both retryable, but recorded with a truthful, distinct code).
        if not v.get("running"):
            if v.get("init_error"):
                raise ProvisionStepError("mt5_init_failed", retryable=True,
                                         detail=str(v.get("init_error"))[:200])
            raise ProvisionStepError("terminal_not_running", retryable=True)
        if require_login:
            login, server = _expected_login_server(rt)
            # (0) a genuine MT5 login REQUIRES a real server string. A free-text ``broker_name`` is NOT an
            # MT5 server, so an account without a normalised ``broker_server`` cannot be broker-login
            # validated — fail closed with a definitive config error rather than reach RUNNING claiming a
            # verified login while the server leg was never checked (a per-server login number could
            # otherwise match the wrong broker's account).
            if not server:
                raise ProvisionStepError("broker_server_required", retryable=False)
            # (1) genuine broker session — a failed login carries a structured reason (bad creds vs
            # server-unavailable vs timeout vs init/crash) mapped to a durable code + retry policy.
            if not v.get("logged_in"):
                code, retryable = _login_failure(v.get("login_error"))
                raise ProvisionStepError(code, retryable=retryable)
            # (2) returned account identity MUST match the submitted account — else fail closed, do NOT
            # run it (controls 5/8).
            if str(v.get("login") or "") != login:
                raise ProvisionStepError("broker_identity_mismatch", retryable=False)
            # (3) broker/server identity consistency — now ALWAYS verified (a server is guaranteed present
            # by check (0)).
            if (v.get("server") or "") != server:
                raise ProvisionStepError("broker_identity_mismatch", retryable=False)
            # (4) demo/live classification MUST match the declared account type (separate demo/live
            # posture). A missing classification is treated as unverified — fail closed.
            if not _broker_classification_matches(rt.trading_account, v):
                raise ProvisionStepError("demo_live_mismatch", retryable=False)
        # The RUNNING transition, the heartbeat stamp, and the durable Verification Report (Increment 3)
        # commit as ONE unit — so a runtime can never end up verified-RUNNING without its audit artefact.
        # If the report create fails, RUNNING rolls back to AUTHENTICATING and the retry re-attempts it.
        from .verification import build_verification_report
        with transaction.atomic():
            rt = record_transition(rt, RuntimeState.RUNNING, reason_code="verified")
            rt.last_heartbeat_at = timezone.now()
            rt.save(update_fields=["last_heartbeat_at", "updated_at"])
            # Reaching here with the flag ON means the login + identity checks above passed, so the
            # broker login IS platform-verified; with the flag OFF the platform verified no login.
            build_verification_report(rt, v, broker_login_verified=require_login)


def _drive_provision(rt: AccountRuntime, p: WindowsProvisioner) -> None:
    # Reserve a slot if not held (idempotent). BLOCKED runtimes re-attempt the reservation (capacity
    # may have freed). A denial raises CapacityError, handled by advance_provisioning_job (never DONE).
    if rt.state in (RuntimeState.NOT_PROVISIONED, RuntimeState.BLOCKED):
        reserve_beta_slot(rt.trading_account)
        rt = AccountRuntime.objects.get(pk=rt.pk)

    # CONTROL-2 narrow-activation gate — re-verify EVERY activation condition before ANY box side-effect
    # (materialise/launch). The global kill switch alone is not sufficient; a non-admitted user can never
    # reach materialise/launch even with the flag on.
    assert_beta_activation_allowed(rt)

    # QUEUED/PROVISIONING → materialise the isolated portable dir, then inject credentials. Both are
    # idempotent; the STARTING transition happens ONLY after both succeed — so a mid-step failure
    # leaves the runtime in a resumable (QUEUED/PROVISIONING) state that the next advance re-runs.
    if rt.state in (RuntimeState.QUEUED, RuntimeState.PROVISIONING):
        if rt.state == RuntimeState.QUEUED:
            rt = record_transition(rt, RuntimeState.PROVISIONING, reason_code="materialising")
        _step(rt, lambda: p.materialise(rt), "materialise_failed")
        login, server = _expected_login_server(rt)
        acct = rt.trading_account
        if getattr(acct, "password_enc", ""):
            password = decrypt_password(acct.password_enc)
            # Customer-credential access audit (Phase 3): broker password read to configure the
            # runtime. Redacted, no secret; background driver has no request.
            log_customer_credential_event(
                "ACCESSED", account=acct, actor="terminal_provisioning", purpose="runtime-configure")
        else:
            password = ""
        _step(rt, lambda: p.configure(rt, login=login, server=server or "", password=password),
              "configure_failed")
        rt = record_transition(rt, RuntimeState.STARTING, reason_code="configured")

    _start_and_verify(rt, p)


def _drive_start(rt: AccountRuntime, p: WindowsProvisioner) -> None:
    if rt.state in (RuntimeState.STOPPED, RuntimeState.STOPPING):
        rt = record_transition(rt, RuntimeState.STARTING, reason_code="restart")
    if rt.state in (RuntimeState.STARTING, RuntimeState.AUTHENTICATING):
        # CONTROL-2 gate before any (re)launch box side-effect.
        assert_beta_activation_allowed(rt)
        _start_and_verify(rt, p)


def _drive_stop(rt: AccountRuntime, p: WindowsProvisioner) -> None:
    # Only stop a runtime that actually holds resources; ignore NOT_PROVISIONED/BLOCKED/REMOVED/etc.
    from .beta_capacity import HELD_STATES
    if rt.state in HELD_STATES and rt.state != RuntimeState.STOPPING:
        rt = record_transition(rt, RuntimeState.STOPPING, reason_code="stop_requested")
    if rt.state == RuntimeState.STOPPING:
        _step(rt, lambda: p.stop(rt), "stop_failed")
        record_transition(rt, RuntimeState.STOPPED, reason_code="stopped")


def _drive_deprovision(rt: AccountRuntime, p: WindowsProvisioner) -> None:
    # Only tear down a runtime that was materialised; NOT_PROVISIONED/REMOVED have nothing to remove.
    if rt.state in (RuntimeState.NOT_PROVISIONED, RuntimeState.REMOVED):
        return
    if rt.state != RuntimeState.DEPROVISIONING:
        rt = record_transition(rt, RuntimeState.DEPROVISIONING, reason_code="deprovision_requested")
    _step(rt, lambda: p.teardown(rt), "teardown_failed")
    record_transition(rt, RuntimeState.REMOVED, reason_code="removed")


def _fail_terminal(job: ProvisioningJob, reason_code: str) -> ProvisioningJob:
    """Fail a job terminally with a sanitised reason (used for capacity denials / invalid runtime)."""
    job.status = ProvisioningJob.Status.FAILED
    job.finished_at = timezone.now()
    job.lease_expires_at = None
    job.last_error = reason_code[:64]
    job.save(update_fields=["status", "finished_at", "lease_expires_at", "last_error"])
    return job


def _fail_runtime(runtime_id: int, reason_code: str) -> None:
    rt = AccountRuntime.objects.filter(pk=runtime_id).first()
    if rt and rt.cohort == AccountRuntime.Cohort.BETA and rt.state != RuntimeState.FAILED:
        record_transition(rt, RuntimeState.FAILED, reason_code=reason_code)


def _fail_step(job: ProvisioningJob, rt: AccountRuntime, e: ProvisionStepError) -> ProvisioningJob:
    """Record the failure (sanitised on the runtime, raw on the immutable event) and apply the retry
    policy. Retryable + attempts remaining → LEAVE the runtime in its resumable state (so the next
    advance re-runs the failed idempotent step) and re-queue the job. Else terminal → FAILED."""
    rt = AccountRuntime.objects.get(pk=rt.pk)  # current durable state (the passed obj may be stale)
    record_transition(rt, rt.state, event_type="FAILURE",
                      reason_code=e.reason_code, detail=e.detail)
    # ``attempt`` was already incremented at claim time — exhausted iff this was the last allowed attempt.
    exhausted = job.attempt >= MAX_ATTEMPTS
    if e.retryable and not exhausted:
        job.status = ProvisioningJob.Status.QUEUED   # re-queue; runtime stays in its resumable state
        job.lease_expires_at = None
    elif e.ambiguous:
        # Repeated AMBIGUITY (channel timeouts), not proven failure: a terminal MAY be running that we
        # cannot confirm. Do NOT declare FAILED (which could imply "safe to re-launch") and do NOT
        # re-launch — set the quarantine flag (blocks re-provisioning; the reserve gate refuses a
        # quarantined runtime) and record the ambiguity WITHOUT forcing a STOPPED state we cannot verify.
        rt_q = AccountRuntime.objects.get(pk=rt.pk)
        rt_q.quarantined = True
        rt_q.quarantine_reason = "ambiguous_timeout"
        rt_q.save(update_fields=["quarantined", "quarantine_reason", "updated_at"])
        record_transition(rt_q, rt_q.state, event_type="FAILURE",
                          reason_code="ambiguous_timeout_quarantined")
        job.status = ProvisioningJob.Status.FAILED
        job.finished_at = timezone.now()
        job.lease_expires_at = None
    elif e.reason_code in PARTIAL_REASONS:
        # PROVEN partial / integrity / containment refusal on a mutating op: fail closed AND quarantine so it
        # is never silently re-driven as success — the reserve gate then refuses the quarantined runtime until
        # an operator reclaims it via the lifecycle. ``quarantine_reason`` carries the AGENT's actual code
        # (e.g. stage_copy_precheck_failed vs reparse_escape vs slot_integrity_mismatch) — a fixed label would
        # mislead an operator about whether a slot is half-copied or a pre-copy containment escape.
        rt_q = AccountRuntime.objects.get(pk=rt.pk)
        rt_q.quarantined = True
        rt_q.quarantine_reason = e.reason_code[:64]
        rt_q.save(update_fields=["quarantined", "quarantine_reason", "updated_at"])
        record_transition(rt_q, RuntimeState.FAILED, reason_code=e.reason_code)
        job.status = ProvisioningJob.Status.FAILED
        job.finished_at = timezone.now()
        job.lease_expires_at = None
    else:
        record_transition(rt, RuntimeState.FAILED, reason_code=e.reason_code)
        job.status = ProvisioningJob.Status.FAILED
        job.finished_at = timezone.now()
        job.lease_expires_at = None
    rt2 = AccountRuntime.objects.get(pk=rt.pk)
    rt2.last_failure_reason = e.reason_code[:64]
    rt2.save(update_fields=["last_failure_reason", "updated_at"])
    job.last_error = e.reason_code[:64]
    job.save(update_fields=["status", "lease_expires_at", "finished_at", "last_error"])
    return job


class FakeProvisioner:
    """In-memory provisioner for tests: records calls and returns a scriptable ``verify`` result."""
    def __init__(self, verify_result=None, fail_on=None, release_slot=2, release_generation=4):
        self.calls = []
        self._verify = verify_result or {"running": True, "logged_in": True, "login": None, "server": None}
        self._fail_on = fail_on or {}   # {"materialise": ProvisionStepError(...), ...}
        self._release_slot = release_slot            # RELEASE reports the RELEASED occupancy's OWN generation
        self._release_generation = release_generation   # (matches the real agent op_release; slot then Available at gen+1)

    def _maybe_fail(self, name):
        if name in self._fail_on:
            raise self._fail_on[name]

    def materialise(self, runtime):
        self.calls.append(("materialise", runtime.runtime_uuid)); self._maybe_fail("materialise")

    def configure(self, runtime, *, login, server, password):
        # password is intentionally NOT stored — asserts callers never persist/log it here.
        self.calls.append(("configure", login, server, bool(password))); self._maybe_fail("configure")

    def start(self, runtime):
        self.calls.append(("start", runtime.runtime_uuid)); self._maybe_fail("start")

    def verify(self, runtime):
        self._maybe_fail("verify")
        v = dict(self._verify)
        if v.get("login") is None:  # default: report the expected identity (happy path)
            login, server = _expected_login_server(runtime)
            v["login"], v["server"] = login, (server or "")
        if "is_demo" not in v:  # default: report the DECLARED classification (happy path matches)
            v["is_demo"] = bool(getattr(runtime.trading_account, "is_demo", False))
        v.setdefault("pid", 4242)
        v.setdefault("session", 1)
        self.calls.append(("verify", runtime.runtime_uuid))
        return v

    def stop(self, runtime):
        self.calls.append(("stop", runtime.runtime_uuid)); self._maybe_fail("stop")

    def teardown(self, runtime):
        self.calls.append(("teardown", runtime.runtime_uuid)); self._maybe_fail("teardown")

    def release(self, runtime):
        self.calls.append(("release", runtime.runtime_uuid)); self._maybe_fail("release")
        return {"released": True, "available": True, "slot": self._release_slot,
                "generation": self._release_generation}
