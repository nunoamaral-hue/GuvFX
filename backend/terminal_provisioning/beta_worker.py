"""CVM-Inc-3 B1 — beta ProvisioningJob WORKER core.

Claims one durable beta ``ProvisioningJob``, NEGOTIATES the versioned contract (protocol/agent/manifest/
supported-ops) before sending any provisioning request, then advances the job through the signed
management channel. Requirement 9 discipline lives in the driver it delegates to: single-flight lease,
persist-then-advance, ambiguous-timeout is never treated as failure (the agent's (job_id, op)
idempotency prevents re-launch on resend), and repeated ambiguity quarantines instead of re-launching.

Split so the worker LOGIC is unit-testable with an injected client factory (no live agent, no HTTP).
"""
import logging

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .beta_capacity import beta_runtimes_enabled
from .mgmt_client import AgentWindowsProvisioner, ManagementChannelError, ManagementChannelTimeout
from .models import AccountRuntime, ProvisioningJob
from .provisioner import advance_provisioning_job

logger = logging.getLogger(__name__)

DEFAULT_TRANSPORT_TIMEOUT = 20      # fallback READ timeout for an unmapped operation
CONNECT_TIMEOUT = 10                # bound how long we wait to CONNECT, independent of the (long) read budget
MAX_TRANSPORT_READ_TIMEOUT = 600    # hard ceiling: no override can produce an unbounded HTTP wait

#: Per-operation READ timeout (seconds). Rationale (Customer Zero 2026-08-01): a single 20s timeout was
#: applied to EVERY op, but MATERIALISE copies the ~380MB golden into a slot (measured ~41s on the beta host)
#: and legitimately runs far longer than a handshake — so the client timed out mid-copy, blind-re-POSTed, and
#: burned the retry budget on ``runtime_busy`` while the copy actually completed. NEGOTIATE/VERIFY stay short
#: so a hung agent fast-fails; MATERIALISE gets a bounded, generous read budget; CONNECT stays short for every
#: op (a (connect, read) tuple) so an unreachable agent fails quickly regardless of the read budget. Centrally
#: governed, overridable via ``settings.BETA_AGENT_OP_TIMEOUTS`` (or the ``BETA_AGENT_OP_TIMEOUTS`` env JSON),
#: every value CLAMPED to ``MAX_TRANSPORT_READ_TIMEOUT``.
OP_TRANSPORT_TIMEOUTS = {
    "NEGOTIATE": 10, "VERIFY": 15, "START": 60, "STOP": 90,
    "TOMBSTONE": 120, "RELEASE": 30, "MATERIALISE": 300,
}


def _op_read_timeout(operation: str, default: int = DEFAULT_TRANSPORT_TIMEOUT) -> int:
    """READ timeout for one signed operation: a ``settings``/env override, else the per-op default, else the
    scalar fallback — ALWAYS clamped to ``MAX_TRANSPORT_READ_TIMEOUT`` so no configuration can wait forever."""
    override = getattr(settings, "BETA_AGENT_OP_TIMEOUTS", None)
    if override is None:
        import json
        import os
        raw = os.getenv("BETA_AGENT_OP_TIMEOUTS", "")
        try:
            override = json.loads(raw) if raw else {}
        except ValueError:
            override = {}
    val = override.get(operation) if isinstance(override, dict) else None
    if val is None:
        val = OP_TRANSPORT_TIMEOUTS.get(operation, default)
    try:
        val = int(val)
    except (TypeError, ValueError):
        val = default
    return max(1, min(val, MAX_TRANSPORT_READ_TIMEOUT))


def claim_next_beta_job():
    """The next claimable BETA ProvisioningJob: QUEUED, or RUNNING with an expired lease (a crashed
    worker). PRODUCTION-runtime jobs are structurally excluded."""
    now = timezone.now()
    return (ProvisioningJob.objects
            .filter(runtime__cohort=AccountRuntime.Cohort.BETA)
            .filter(Q(status=ProvisioningJob.Status.QUEUED)
                    | Q(status=ProvisioningJob.Status.RUNNING, lease_expires_at__lt=now))
            .order_by("created_at")
            .first())


def make_http_transport(timeout: int = DEFAULT_TRANSPORT_TIMEOUT):
    """Real transport: POST the signed request to the private-network agent. The READ timeout is selected
    PER OPERATION (see ``OP_TRANSPORT_TIMEOUTS``) from the already-signed ``operation`` field — the transport
    only READS the request, never re-signs or mutates it — while CONNECT stays short for every op. A read
    timeout is AMBIGUOUS → ``ManagementChannelTimeout`` (never treated as failure; the driver reconciles)."""
    import requests

    def transport(base_url: str, req: dict) -> dict:
        if not base_url:
            raise ManagementChannelError("agent_base_url_unset")
        url = base_url.rstrip("/") + "/provision"
        op = req.get("operation", "") if isinstance(req, dict) else ""
        read_timeout = _op_read_timeout(op, default=timeout)
        try:
            resp = requests.post(url, json=req, timeout=(CONNECT_TIMEOUT, read_timeout))
        except requests.Timeout:
            raise ManagementChannelTimeout()
        except requests.RequestException:
            raise ManagementChannelError("transport_error")
        try:
            return resp.json()
        except ValueError:
            raise ManagementChannelError("bad_agent_response")

    return transport


def default_client_factory(job: ProvisioningJob) -> AgentWindowsProvisioner:
    return AgentWindowsProvisioner(job_id=job.id, transport=make_http_transport())


def _hb(status: str, job_id=None) -> None:
    """ADR-0021 durable heartbeat write. Fail-open — a heartbeat write must never break the worker loop."""
    try:
        import os as _os

        from .models import ProvisionerHeartbeat
        ProvisionerHeartbeat.touch(_os.getenv("MT5_WORKER_ID", "beta-provisioner"), status, job_id)
    except Exception:  # noqa: BLE001
        logger.debug("beta worker: heartbeat write skipped", exc_info=True)


def process_one(client_factory=default_client_factory, *, negotiate: bool = True) -> str:
    """Claim + advance ONE beta job. Returns a short status string. Never raises to the caller.

    ADR-0021 liveness — FAIL-CLOSED ordering:
    * ``IDLE_READY`` (healthy) is written ONLY when the worker is genuinely idle-and-OK — disabled or no
      job queued. It is deliberately NOT written unconditionally at the top of the loop, because that
      would erase a ``DEGRADED``/``ERROR`` recorded on the previous iteration and mask a dead agent.
    * ``PROCESSING`` (healthy) is written ONLY AFTER a successful contract negotiation — i.e. once the
      agent has actually answered. Marking healthy BEFORE the (up-to-transport-timeout) negotiation would
      let a NEW-runtime reservation fail OPEN against a hung/unreachable agent.
    * ``DEGRADED`` (agent unreachable / contract mismatch) and ``ERROR`` (unexpected failure) persist —
      the next iteration only overwrites them with a healthy state once the agent answers again.
    The per-step heartbeat callback keeps a long multi-step job fresh at ``PROCESSING``."""
    if not beta_runtimes_enabled():
        _hb("IDLE_READY")          # dark but alive; nothing to provision
        return "disabled"          # dark by default; the worker does nothing until armed
    job = claim_next_beta_job()
    if job is None:
        _hb("IDLE_READY")          # alive + idle; no work queued
        return "no_job"
    # A job is claimed. Do NOT mark PROCESSING yet — negotiation may block on a hung/unreachable agent,
    # and PROCESSING is a HEALTHY state. The heartbeat holds its prior value across the negotiation.
    client = client_factory(job)
    if negotiate:
        try:
            client.assert_compatible()   # versioned contract BEFORE any provisioning request
        except (ManagementChannelError, ManagementChannelTimeout) as e:
            # Cannot agree the contract / agent unreachable — leave the job QUEUED for a later attempt.
            logger.warning("beta worker: negotiation failed for job=%s: %s", job.id,
                           getattr(e, "reason_code", "timeout"))
            _hb("DEGRADED", job.id)      # agent connectivity degraded — stays unhealthy for reservation
            return "negotiation_failed"
    # Negotiation succeeded — the agent answered — so the worker is genuinely PROCESSING (healthy).
    _hb("PROCESSING", job.id)
    try:
        # The heartbeat callback fires after EVERY provisioning step, so a long multi-step job keeps
        # refreshing PROCESSING mid-run and never reads stale.
        advance_provisioning_job(job, client, heartbeat=lambda: _hb("PROCESSING", job.id))
    except Exception:  # noqa: BLE001 — never raise to the caller; surface worker-level trouble as ERROR
        logger.exception("beta worker: advance failed for job=%s", job.id)
        _hb("ERROR", job.id)
        return "error"
    _hb("PROCESSING", job.id)  # refresh after the step
    return "advanced"
