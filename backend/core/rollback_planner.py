"""core.rollback_planner — repository-only, READ-ONLY rollback planning (ADR-0035).

Answers "if we had to roll back right now, what is the safest action?" by reading the current
feature-flag posture and mapping each *armed* capability to its **flag-disable DARK rollback**. It
**executes nothing** — it changes no flag, touches no database, contacts no host. It only prints the
plan an operator (or the Chief Architect) would then perform through the sanctioned mechanism.

Guiding rule (from ``docs/operations/broker-connectivity/rollback-matrix.md``): **prefer disabling a
flag over any destructive DB rollback.** Every arming flag supports instant DARK rollback with no
data loss; there is *no* destructive database rollback in the DARK->armed direction for any flag. The
deploy-image rollback (tag ``rollback-preADR0021``) is a separate, manual, Sponsor-approved lever and is
surfaced here for reference only.
"""
from __future__ import annotations

# name -> (dark_effect description, partial_state id it returns toward). Ordered from outermost
# capability (execution) inward, so a "disable all" reads top-down safely.
_FLAG_REGISTRY = (
    ("HOSTED_MT5_EXECUTION_ENABLED",
     "Hosted Workspace (Provider B) execution disarmed; no hosted order path", "customer_journey_only"),
    ("BROKER_CONNECTIVITY_EXECUTION_GATE",
     "Execution gate becomes transparent (creation-time gate off)", "health_enabled_no_gate"),
    ("HOSTED_MT5_REMOTEAPP_ENABLED",
     "RemoteApp delivery off; no new delivery descriptors minted", "customer_journey_only"),
    ("HOSTED_WORKSPACE_ONBOARDING_ENABLED",
     "Onboarding journey endpoints 404; no new workspace requests", "operator_observability_only"),
    ("HOSTED_MT5_ACTIVE_ACCOUNT_POLLING_ENABLED",
     "Active-account attach polling stops (observation cadence)", "operator_observability_only"),
    ("BROKER_CONNECTIVITY_HEALTH_ENABLED",
     "WP3 broker-health engine stops evaluating (existing rows frozen)", "operator_observability_only"),
    ("OPERATIONS_EVENTS_ENABLED",
     "Operational event API 404s; recorders become no-ops", "dark_deployed"),
    ("VALIDATION_AGENT_MONITORING_ENABLED",
     "Agent monitor runner inert; no probes, no alerts", "dark_deployed"),
    ("HOSTED_PERSISTENT_MT5_ENABLED",
     "MASTER gate off — entire Hosted Workspace subsystem dark (all sub-flags moot)", "dark_deployed"),
    ("BROKER_CONNECTIVITY_ENABLED",
     "Broker-connectivity master off — journey/health/gate all dark", "dark_deployed"),
)

_DEPLOY_ROLLBACK = {
    "image_tag": "rollback-preADR0021",
    "reverse_migrations": ["terminal_provisioning 0008", "trading 0012"],
    "note": "Deploy-image rollback is a MANUAL, Sponsor-approved lever (docs/ADR-0021-DEPLOY-ROLLBACK-PLAN.md). "
            "Prefer flag-disable first; reach for image rollback only on a Golden STOP-check drift, an "
            "unexpected order/position, or a customer runtime reaching RUNNING without expected state.",
}


def _flag_value(name: str):
    """Resolve a flag's current boolean value read-only, tolerating settings-or-env. Returns None if the
    flag cannot be resolved (never raises)."""
    try:
        import os

        from django.conf import settings
        val = getattr(settings, name, None)
        if val is None:
            val = os.getenv(name, "")
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return None


def plan_rollback() -> dict:
    """Read the current flag posture and produce the safe DARK rollback plan. MUTATES NOTHING."""
    armed = []
    dark = []
    for name, dark_effect, partial_state in _FLAG_REGISTRY:
        v = _flag_value(name)
        row = {"flag": name, "value": v, "dark_effect": dark_effect, "returns_to": partial_state}
        if v:
            armed.append(row)
        else:
            dark.append(row)

    steps = [{
        "order": i + 1,
        "action": f"UNSET {r['flag']} (set to empty/false)",
        "effect": r["dark_effect"],
        "returns_to": r["returns_to"],
        "destructive": False,
    } for i, r in enumerate(armed)]

    return {
        "posture": "ARMED" if armed else "FULLY_DARK",
        "guiding_rule": "Prefer disabling a flag over any destructive DB rollback; all arming flags "
                        "support instant DARK rollback with no data loss.",
        "armed_flags": [r["flag"] for r in armed],
        "rollback_steps": steps,
        "already_dark": [r["flag"] for r in dark],
        "deploy_image_rollback": _DEPLOY_ROLLBACK,
        "executes_anything": False,
    }
