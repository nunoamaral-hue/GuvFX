"""GFX-BETA-PHASE0 Increment 3 — Account Status panel (truthful, per-account).

Builds a per-account status made of ordered stages, each with a TRUTHFUL state. Runtime/terminal stages
are derived ONLY from the durable AccountRuntime (Increment 2) — they NEVER imply that an MT5 terminal
exists or is connected while the architecture-dependent provisioning system is undeployed. Unsupported
stages show NOT_CONFIGURED / BLOCKED, never a false RUNNING/HEALTHY.
"""
from .models import AccountRuntime, RuntimeState
from .runtime_state import user_facing_state

HEALTHY = "HEALTHY"
WARNING = "WARNING"
FAILED = "FAILED"
NOT_CONFIGURED = "NOT_CONFIGURED"


def _stage(key, label, state, detail, *, at=None):
    return {"key": key, "label": label, "state": state, "detail": detail, "at": at}


def _runtime_detail(rt_state):
    return {
        "NOT_CONFIGURED": "Waiting to start — your dedicated runtime is provisioned automatically.",
        "QUEUED": "Provisioning queued.",
        "BLOCKED": "Blocked — waiting on a prerequisite.",
        "PROVISIONING": "Provisioning the isolated MT5 runtime…",
        "RUNNING": "Isolated MT5 runtime is running.",
        "DEGRADED": "Runtime degraded — auto-repairing.",
        "STOPPED": "Runtime stopped.",
        "FAILED": "Provisioning failed — see diagnostics.",
        "REMOVED": "Runtime removed.",
        "REMOVING": "Removing runtime…",
    }.get(rt_state, "Not configured.")


# ── Explicit customer-facing lifecycle (ADR-0021) ────────────────────────────────────────────────────
# Account received → Provisioning runtime → Connecting to broker → Validated / Connection failed → Retry.
# Derived from the durable runtime + validation state (never from whether an operation call succeeded).
# The "Connecting to broker" phase applies ONLY when broker-login is required
# (``PROVISIONING_REQUIRE_BROKER_LOGIN``); otherwise the runtime reaching RUNNING IS the completed state,
# so that phase is truthfully absent from the sequence.
_LC_ACCOUNT_RECEIVED = "account_received"
_LC_PROVISIONING = "provisioning_runtime"
_LC_CONNECTING = "connecting_broker"
_LC_VALIDATED = "validated"
_LC_FAILED = "connection_failed"

_LC_COPY = {
    _LC_ACCOUNT_RECEIVED: ("Account received",
        "We've recorded your broker account. Setup of your dedicated terminal begins automatically."),
    _LC_PROVISIONING: ("Provisioning runtime",
        "We're preparing your dedicated trading runtime. This usually takes a minute or two."),
    _LC_CONNECTING: ("Connecting to broker",
        "Your runtime is up — we're logging in to your broker to validate the connection."),
    _LC_VALIDATED: ("Validated",
        "Your dedicated terminal is ready."),
    _LC_FAILED: ("Connection failed",
        "We couldn't complete setup. Please check your login details and try again."),
}


def _lifecycle(rt_state, vstatus, broker_required):
    """Compute the explicit customer-facing lifecycle from durable state. Returns the current ``phase``,
    its ``label``/``detail``, a ``retryable`` flag (true only on a failure the customer can retry), and an
    ordered ``steps`` stepper (each ``done``/``current``/``pending``/``failed``)."""
    failed_at = None
    if rt_state == "FAILED":
        phase, failed_at = _LC_FAILED, _LC_PROVISIONING
    elif vstatus in ("CONNECTION_FAILED", "TECHNICAL_ERROR"):
        phase, failed_at = _LC_FAILED, _LC_CONNECTING
    elif rt_state == "RUNNING":
        phase = _LC_CONNECTING if (broker_required and vstatus != "VALIDATED") else _LC_VALIDATED
    elif rt_state in ("QUEUED", "PROVISIONING", "BLOCKED", "DEGRADED", "STOPPED", "REMOVING"):
        phase = _LC_PROVISIONING
    else:   # NOT_CONFIGURED / REMOVED — the account record exists but its runtime is not up yet
        phase = _LC_ACCOUNT_RECEIVED

    order = [_LC_ACCOUNT_RECEIVED, _LC_PROVISIONING]
    if broker_required:
        order.append(_LC_CONNECTING)
    order.append(_LC_VALIDATED)

    steps = []
    if phase == _LC_FAILED:
        fail_idx = order.index(failed_at) if failed_at in order else len(order) - 1
        for i, key in enumerate(order):
            status = "done" if i < fail_idx else ("failed" if i == fail_idx else "pending")
            steps.append({"key": key, "label": _LC_COPY[key][0], "status": status})
    else:
        cur_idx = order.index(phase)
        for i, key in enumerate(order):
            status = "done" if i < cur_idx else ("current" if i == cur_idx else "pending")
            if key == _LC_VALIDATED and phase == _LC_VALIDATED:
                status = "done"   # the terminal success state is complete, not "in progress"
            steps.append({"key": key, "label": _LC_COPY[key][0], "status": status})

    label, detail = _LC_COPY[phase]
    return {"phase": phase, "label": label, "detail": detail,
            "retryable": phase == _LC_FAILED, "steps": steps}


def build_account_status(account) -> dict:
    """Return a truthful, ordered status for one broker account. Read-only; never creates a runtime row.
    Runtime/terminal stages reflect the durable AccountRuntime state (NOT_CONFIGURED while provisioning
    is undeployed) — they never imply a live terminal."""
    from strategies.models import StrategyAssignment
    from execution.models import ExecutionJob

    runtime = AccountRuntime.objects.filter(trading_account=account).first()
    rt_state = user_facing_state(runtime) if runtime is not None else NOT_CONFIGURED
    # "Hosted terminal available" reflects the durable RUNNING state (display-only; the authoritative
    # arming gate re-checks full ``runtime_ready``). IPR Area B: canonical readiness is
    # ``account_runtime_ready``; this status read is intentionally left at the optimistic RUNNING check.
    rt_running = runtime is not None and runtime.state == RuntimeState.RUNNING
    rt_last_error = (runtime.last_error if runtime is not None else "") or ""

    stages = []
    # 1. Account created
    stages.append(_stage("account_created", "Account created", HEALTHY, "Account exists."))

    # 2. Broker account configured (credentials stored ≠ terminal connected)
    has_creds = bool(getattr(account, "account_number", ""))
    stages.append(_stage(
        "broker_configured", "Broker account configured",
        HEALTHY if has_creds else NOT_CONFIGURED,
        "Broker credentials stored." if has_creds else "Add broker credentials."))

    # 2b. Credentials validated (TB-3) — durable per-account validation result, distinct from merely
    #     "stored". VALIDATED → HEALTHY; a failed validation → FAILED; stored-but-never-validated →
    #     WARNING; no credentials yet → NOT_CONFIGURED.
    vstatus = getattr(account, "validation_status", "NEVER") or "NEVER"
    if not has_creds:
        v_state, v_detail = NOT_CONFIGURED, "Add broker credentials."
    elif vstatus == "VALIDATED":
        v_state, v_detail = HEALTHY, "Credentials validated with the broker."
    elif vstatus in ("CONNECTION_FAILED", "TECHNICAL_ERROR"):
        v_state, v_detail = "FAILED", ("Connection failed — check the login/password/server."
                                       if vstatus == "CONNECTION_FAILED" else "Validation could not run.")
    else:
        v_state, v_detail = WARNING, "Credentials stored but not yet validated."
    stages.append(_stage("credentials_validated", "Credentials validated", v_state, v_detail))

    # 3. MT5 runtime — durable runtime state; NEVER implies a terminal exists while undeployed
    stages.append(_stage("mt5_runtime", "MT5 runtime", rt_state,
                         (rt_last_error if rt_state == "FAILED" and rt_last_error else _runtime_detail(rt_state))))

    # 4. Hosted terminal — "available" ONLY if the runtime is actually RUNNING
    stages.append(_stage(
        "hosted_terminal", "Hosted terminal",
        "RUNNING" if rt_running else NOT_CONFIGURED,
        "Terminal available." if rt_running else "Not provisioned yet."))

    # 5/6. Strategy assigned / enabled (AUTO_DEMO)
    auto = StrategyAssignment.objects.filter(account=account, execution_mode="AUTO_DEMO")
    assigned = auto.exists()
    enabled = assigned and auto.filter(is_active=True).exists()
    stages.append(_stage("strategy_assigned", "Strategy assigned",
                         HEALTHY if assigned else NOT_CONFIGURED,
                         "A strategy is assigned." if assigned else "Assign a strategy."))
    stages.append(_stage(
        "strategy_enabled", "Strategy enabled",
        HEALTHY if enabled else (WARNING if assigned else NOT_CONFIGURED),
        "Enabled." if enabled else ("Assigned but not enabled." if assigned else "Not enabled.")))

    # 7. Last execution — truthful per status (a FAILED last job must not read green/HEALTHY). A past
    #    failure is surfaced as WARNING (attention) rather than FAILED, so it does not over-escalate the
    #    overall (which stays driven by the runtime chain).
    last_job = ExecutionJob.objects.filter(account=account).order_by("-created_at").first()
    if last_job is None:
        last_state = NOT_CONFIGURED
    elif last_job.status == "SUCCESS":
        last_state = HEALTHY
    else:
        last_state = WARNING  # FAILED / PENDING / RUNNING / other → not green
    stages.append(_stage(
        "last_execution", "Last execution", last_state,
        (f"{last_job.job_type} {last_job.status}" if last_job is not None else "No executions yet."),
        at=last_job.created_at.isoformat() if last_job is not None else None))

    # 8. Last heartbeat — a runtime heartbeat only exists once provisioned
    stages.append(_stage("last_heartbeat", "Last heartbeat", NOT_CONFIGURED,
                         "No runtime heartbeat yet."))

    # 9. Last notification — per-account notification tracking is wired in a later increment
    stages.append(_stage("last_notification", "Last notification", NOT_CONFIGURED,
                         "No per-account notification history yet."))

    # Explicit customer-facing lifecycle (ADR-0021). Additive: the granular ``stages`` above are
    # unchanged; this is the compact, ordered progression the onboarding panel renders. The
    # "Connecting to broker" phase is present only when a broker login is actually required.
    try:
        from terminal_provisioning.provisioner import _require_broker_login
        broker_required = _require_broker_login()
    except Exception:  # noqa: BLE001 — never let the flag lookup break a read-only status build
        broker_required = False
    lifecycle = _lifecycle(rt_state, vstatus, broker_required)

    return {
        "account_id": account.id,
        "account_number": getattr(account, "account_number", ""),
        "overall": _overall(stages),
        # explicit, so the UI can never assume a terminal from a green overall:
        "terminal_provisioning_available": False,
        "lifecycle": lifecycle,
        "stages": stages,
    }


def _overall(stages) -> str:
    states = {s["state"] for s in stages}
    if FAILED in states:
        return FAILED
    if "DEGRADED" in states:
        return "DEGRADED"
    # "healthy" only if the runtime + strategy chain is actually up
    keyed = {s["key"]: s["state"] for s in stages}
    if keyed.get("hosted_terminal") == "RUNNING" and keyed.get("strategy_enabled") == HEALTHY:
        return HEALTHY
    return NOT_CONFIGURED
