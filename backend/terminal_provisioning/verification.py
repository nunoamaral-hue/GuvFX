"""GFX-BETA-HEADLESS Increment 3 — Provisioning Verification Report generator.

Produces the durable audit artefact the moment a BETA runtime reaches a verified RUNNING state. Captures
runtime identity, ownership, broker identity, process/session identity, provisioning duration, heartbeat,
and the raw verification evidence. Contains NO secrets (broker password is never read here).
"""
from django.utils import timezone

from .models import AccountRuntime, ProvisioningVerificationReport, RuntimeEvent, RuntimeState

# Keys copied from the provisioner verify() payload into the report evidence — a strict allowlist of
# STRUCTURED, non-secret fields. Deliberately excludes any free-text ``journal``/``detail`` (which can
# carry connection/endpoint strings) so the "no secrets" guarantee does not depend on the Windows side.
_SAFE_EVIDENCE_KEYS = ("running", "logged_in", "login", "server", "pid", "session")


def _provisioning_duration_ms(runtime) -> int | None:
    """Wall-clock of the CURRENT provisioning cycle: from the reservation (the latest QUEUED event that
    precedes this RUNNING) to the RUNNING event. Cycle-scoped so a restart's report does not span the
    entire prior STOPPED period."""
    r = (RuntimeEvent.objects.filter(runtime=runtime, to_state=RuntimeState.RUNNING)
         .order_by("-id").values_list("id", "created_at").first())
    if not r:
        return None
    running_id, running_at = r
    q = (RuntimeEvent.objects.filter(runtime=runtime, to_state=RuntimeState.QUEUED, id__lt=running_id)
         .order_by("-id").values_list("created_at", flat=True).first())
    start = q or runtime.created_at
    if not start:
        return None
    return max(0, int((running_at - start).total_seconds() * 1000))


def build_verification_report(runtime: AccountRuntime, verify_evidence: dict, *,
                              broker_login_verified: bool = False) -> ProvisioningVerificationReport:
    """Create the immutable report for a runtime that has just verified RUNNING. Only BETA runtimes are
    ever reported here (production is never provisioned by this path).

    ``broker_login_verified`` is the PLATFORM's determination (control-8 login + identity actually
    checked) — passed by the caller, NOT inferred from the box's ``logged_in`` self-report. It defaults
    to False so the report never asserts a broker login unless the platform positively verified one; in
    the broker-INDEPENDENT phase it is always False even when the box claims a login."""
    if runtime.cohort != AccountRuntime.Cohort.BETA:
        raise ValueError("verification reports are only produced for BETA runtimes")
    v = verify_evidence or {}
    # A report is genuine evidence of a runtime whose process is verified up. ``broker_login_verified``
    # is a SEPARATE dimension (see above). We still refuse to fabricate a report for a runtime that is
    # not actually running.
    if not v.get("running"):
        raise ValueError("verification report requires a running runtime")
    acct = runtime.trading_account
    user = getattr(acct, "user", None)
    heartbeat = runtime.last_heartbeat_at or timezone.now()

    # ``broker_login``/``broker_server`` are the runtime's OWN assigned binding — NOT the box's
    # self-report — so the structured fields can never assert an identity the platform did not check.
    # ``broker_server`` is the normalised server_name when the account carries one, else blank (free-text
    # broker_name is not an MT5 server string). The box's raw self-reported login/server remain in
    # ``evidence`` for forensics.
    login = str(acct.account_number or "")
    server = (acct.broker_server.server_name or "").strip() if acct.broker_server_id else ""
    evidence = {k: v[k] for k in _SAFE_EVIDENCE_KEYS if k in v}

    return ProvisioningVerificationReport.objects.create(
        runtime=runtime,
        runtime_uuid=runtime.runtime_uuid,
        runtime_root=runtime.runtime_root,
        bridge_identity=runtime.bridge_identity,
        owner_user_id=getattr(user, "id", None),
        owner_email=getattr(user, "email", "") or "",
        trading_account_id=acct.id,
        broker_login=login[:64],
        broker_server=server[:160],
        broker_login_verified=bool(broker_login_verified),
        process_pid=v.get("pid"),
        windows_session=v.get("session"),
        provisioning_duration_ms=_provisioning_duration_ms(runtime),
        heartbeat_at=heartbeat,
        verified=bool(v.get("running")),
        evidence=evidence,
    )
