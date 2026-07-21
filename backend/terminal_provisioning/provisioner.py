"""GFX-BETA-HEADLESS Increment 2 — provisioning executor (driver + Windows provisioner interface).

Drives one BETA ``AccountRuntime`` through the durable state machine by executing a ``ProvisioningJob``.
Enqueue-only, idempotent, persist-then-act, bounded retries, immutable ``RuntimeEvent`` evidence, and —
critically — it **never reports RUNNING before the runtime is verified logged in to its OWN assigned
broker account** (compensating control 8), and it **never places an order** (this is provisioning, not
execution). Credentials are decrypted transiently and handed to the provisioner via an argument that a
real provisioner injects over an authenticated channel — never into a command line, URL, or log
(control 10). Nuno's PRODUCTION runtimes are refused (control 14).
"""
from typing import Protocol

from django.db import transaction
from django.utils import timezone

from trading.crypto import decrypt_password

from .beta_capacity import CapacityError, _require_beta, reserve_beta_slot
from .models import AccountRuntime, ProvisioningJob, RuntimeState
from .runtime_state import record_transition

MAX_ATTEMPTS = 3
LEASE_TTL_SECONDS = 300


class ProvisionStepError(Exception):
    """A provisioning step failed. ``reason_code`` is user-safe; ``detail`` is admin-only; ``retryable``
    controls whether the driver retries (bad credentials, for example, are NOT retryable)."""
    def __init__(self, reason_code: str, *, detail: str = "", retryable: bool = True):
        self.reason_code = reason_code
        self.detail = detail
        self.retryable = retryable
        super().__init__(reason_code)


class WindowsProvisioner(Protocol):
    """The Windows-side capability the driver orchestrates. A real implementation talks to the box's
    beta-provisioner over the authenticated management channel; ``FakeProvisioner`` is used in tests."""
    def materialise(self, runtime: AccountRuntime) -> None: ...
    def configure(self, runtime: AccountRuntime, *, login: str, server: str, password: str) -> None: ...
    def start(self, runtime: AccountRuntime) -> None: ...
    def verify(self, runtime: AccountRuntime) -> dict: ...   # {running, logged_in, login, server}
    def stop(self, runtime: AccountRuntime) -> None: ...
    def teardown(self, runtime: AccountRuntime) -> None: ...


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


# ── Enqueue (enqueue-only: callers create jobs; a worker advances them) ──
def enqueue_op(runtime: AccountRuntime, op: str) -> ProvisioningJob:
    _require_beta(runtime)
    return ProvisioningJob.objects.create(runtime=runtime, op=op)


# ── Driver ──
def advance_provisioning_job(job: ProvisioningJob, provisioner: WindowsProvisioner) -> ProvisioningJob:
    """Claim (lease) and advance a job. Idempotent + resumable: dispatch is by the runtime's durable
    state, so a re-claimed job continues from where it left off and never repeats a completed step."""
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
    except ValueError as e:
        return _fail_terminal(j, "invalid_runtime")

    j.status = ProvisioningJob.Status.DONE
    j.finished_at = timezone.now()
    j.lease_expires_at = None
    j.last_error = ""
    j.save(update_fields=["status", "finished_at", "lease_expires_at", "last_error"])
    return j


def _step(runtime, fn, reason_code):
    """Run one provisioner side-effect, converting any raw failure into a sanitised ProvisionStepError."""
    try:
        return fn()
    except ProvisionStepError:
        raise
    except Exception as exc:  # noqa: BLE001 — never leak a raw agent string to the user path
        raise ProvisionStepError(reason_code, detail=str(exc)[:2000], retryable=True)


def _start_and_verify(rt: AccountRuntime, p: WindowsProvisioner) -> None:
    """Shared STARTING → AUTHENTICATING → RUNNING path used by both PROVISION and START, so a restart
    also verifies broker identity before reaching RUNNING (control 8). Resumable per state."""
    if rt.state == RuntimeState.STARTING:
        _step(rt, lambda: p.start(rt), "start_failed")
        rt = record_transition(rt, RuntimeState.AUTHENTICATING, reason_code="started")
    if rt.state == RuntimeState.AUTHENTICATING:
        v = _step(rt, lambda: p.verify(rt), "verify_failed") or {}
        login, server = _expected_login_server(rt)
        if not v.get("running"):
            raise ProvisionStepError("terminal_not_running", retryable=True)
        if not v.get("logged_in"):
            raise ProvisionStepError("broker_login_failed", retryable=False)
        if str(v.get("login") or "") != login:
            # Authenticated to the WRONG account — fail closed, do NOT run it (controls 5/8).
            raise ProvisionStepError("broker_identity_mismatch", retryable=False)
        # Server is verified only when we have a reliable expected value (a normalised broker_server
        # server_name). Free-text broker_name is not the MT5 server string, so we don't hard-fail on it.
        if server is not None and (v.get("server") or "") != server:
            raise ProvisionStepError("broker_identity_mismatch", retryable=False)
        record_transition(rt, RuntimeState.RUNNING, reason_code="verified")


def _drive_provision(rt: AccountRuntime, p: WindowsProvisioner) -> None:
    # Reserve a slot if not held (idempotent). BLOCKED runtimes re-attempt the reservation (capacity
    # may have freed). A denial raises CapacityError, handled by advance_provisioning_job (never DONE).
    if rt.state in (RuntimeState.NOT_PROVISIONED, RuntimeState.BLOCKED):
        reserve_beta_slot(rt.trading_account)
        rt = AccountRuntime.objects.get(pk=rt.pk)

    # QUEUED/PROVISIONING → materialise the isolated portable dir, then inject credentials. Both are
    # idempotent; the STARTING transition happens ONLY after both succeed — so a mid-step failure
    # leaves the runtime in a resumable (QUEUED/PROVISIONING) state that the next advance re-runs.
    if rt.state in (RuntimeState.QUEUED, RuntimeState.PROVISIONING):
        if rt.state == RuntimeState.QUEUED:
            rt = record_transition(rt, RuntimeState.PROVISIONING, reason_code="materialising")
        _step(rt, lambda: p.materialise(rt), "materialise_failed")
        login, server = _expected_login_server(rt)
        acct = rt.trading_account
        password = decrypt_password(acct.password_enc) if getattr(acct, "password_enc", "") else ""
        _step(rt, lambda: p.configure(rt, login=login, server=server or "", password=password),
              "configure_failed")
        rt = record_transition(rt, RuntimeState.STARTING, reason_code="configured")

    _start_and_verify(rt, p)


def _drive_start(rt: AccountRuntime, p: WindowsProvisioner) -> None:
    if rt.state in (RuntimeState.STOPPED, RuntimeState.STOPPING):
        rt = record_transition(rt, RuntimeState.STARTING, reason_code="restart")
    if rt.state in (RuntimeState.STARTING, RuntimeState.AUTHENTICATING):
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
    def __init__(self, verify_result=None, fail_on=None):
        self.calls = []
        self._verify = verify_result or {"running": True, "logged_in": True, "login": None, "server": None}
        self._fail_on = fail_on or {}   # {"materialise": ProvisionStepError(...), ...}

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
            v["login"], v["server"] = login, server
        self.calls.append(("verify", runtime.runtime_uuid))
        return v

    def stop(self, runtime):
        self.calls.append(("stop", runtime.runtime_uuid)); self._maybe_fail("stop")

    def teardown(self, runtime):
        self.calls.append(("teardown", runtime.runtime_uuid)); self._maybe_fail("teardown")
