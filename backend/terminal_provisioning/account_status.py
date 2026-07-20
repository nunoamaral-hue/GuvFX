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
        "NOT_CONFIGURED": "No isolated MT5 runtime yet (automatic provisioning not deployed).",
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


def build_account_status(account) -> dict:
    """Return a truthful, ordered status for one broker account. Read-only; never creates a runtime row.
    Runtime/terminal stages reflect the durable AccountRuntime state (NOT_CONFIGURED while provisioning
    is undeployed) — they never imply a live terminal."""
    from strategies.models import StrategyAssignment
    from execution.models import ExecutionJob

    runtime = AccountRuntime.objects.filter(trading_account=account).first()
    rt_state = user_facing_state(runtime) if runtime is not None else NOT_CONFIGURED
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
                         "No runtime heartbeat (provisioning not deployed)."))

    # 9. Last notification — per-account notification tracking is wired in a later increment
    stages.append(_stage("last_notification", "Last notification", NOT_CONFIGURED,
                         "No per-account notification history yet."))

    return {
        "account_id": account.id,
        "account_number": getattr(account, "account_number", ""),
        "overall": _overall(stages),
        # explicit, so the UI can never assume a terminal from a green overall:
        "terminal_provisioning_available": False,
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
