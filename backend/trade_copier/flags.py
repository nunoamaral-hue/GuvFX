"""trade_copier.flags — DARK-by-default feature gates for the MT5 Master/Follower Copier.

Idiom B (settings-first-then-env, tolerant parser, read LIVE) — identical shape to
``hosted_workspace.flags`` / ``billing.beta``. Every accessor defaults OFF (empty-string default is
falsey) and is read live so a Sponsor can toggle without a restart. The whole copier ships DARK:
nothing observes, replicates, or executes until a Sponsor flips the lever.

LIVE follower execution is NOT governed here. It stays blocked by the execution subsystem's existing
five-layer LIVE wall (``execution.live_authz.live_execution_permitted``) and the demo-only bridge
close/modify handlers (ADR-0051). No copier flag can enable a real-money order: the copier only ever
*observes* (PR A) or creates SHADOW/DEMO ``ExecutionJob`` rows (PR B) that those gates continue to
govern. A copier flag is always ANDed with — never ORs past — the execution gates.
"""
import os

from django.conf import settings

_TRUTHY = ("1", "true", "yes", "on")


def _flag(name: str, default: str = "") -> bool:
    """Settings override wins, else env; empty/falsey default = OFF. Tolerant token set."""
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


def copier_enabled() -> bool:
    """Master gate for the whole copier subsystem. DEFAULT OFF."""
    return _flag("COPIER_ENABLED")


def copier_observer_enabled() -> bool:
    """Gate for the read-only master observation pass (PR A). DEFAULT OFF.

    ANDed with the master gate: the observer runs only when BOTH ``COPIER_ENABLED`` and
    ``COPIER_OBSERVER_ENABLED`` are on, so the subsystem cannot half-activate."""
    return copier_enabled() and _flag("COPIER_OBSERVER_ENABLED")


def copier_replication_enabled() -> bool:
    """Gate for the replication INTENT engine (PR B). DEFAULT OFF, ANDed with the master gate.

    This gates recording of replication INTENTS only — PR B creates no ExecutionJobs and performs no
    execution. Any future gated DEMO execution-job creation sits behind a SEPARATE, additional flag
    and is never enabled by this one."""
    return copier_enabled() and _flag("COPIER_REPLICATION_ENABLED")


def copier_ui_enabled() -> bool:
    """Gate for the customer-facing read-only Copy Trading UI + its read API (PR C). DEFAULT OFF.

    Independent of the observer/replication gates so the monitoring UI can be shown (read-only) without
    arming any observation or execution. When OFF the read API reports enabled:false and returns no
    rows, and the frontend hides the Copy Trading entry — nothing is exposed in production until armed."""
    return _flag("COPIER_UI_ENABLED")


def copier_ui_cohort_allowlist() -> frozenset:
    """Founder/operator COHORT allowlist for the read-only Copy Trading UI (default-DENY). Lowercased
    emails parsed from ``COPIER_UI_COHORT_ALLOWLIST`` (comma-separated), settings-override-then-env.
    Plain config, never a secret/token. Empty/unset ⇒ no user is in the cohort (UI dark for everyone)."""
    raw = getattr(settings, "COPIER_UI_COHORT_ALLOWLIST", None)
    if raw is None:
        raw = os.getenv("COPIER_UI_COHORT_ALLOWLIST", "")
    return frozenset(e.strip().lower() for e in str(raw).split(",") if e.strip())


def copier_ui_enabled_for(user) -> bool:
    """OWNER/FOUNDER-SCOPED gate for the read-only Copy Trading UI — the SOLE authorization boundary,
    default-DENY. A user sees the monitoring UI ONLY when their email is in
    ``copier_ui_cohort_allowlist()``. There is deliberately NO global-flag path: the legacy global
    ``copier_ui_enabled()`` is NOT consulted here, so a global boolean can never expose the UI to the
    whole member base (the broker_intelligence multi-mailbox idiom). An empty/unset allowlist therefore
    denies everyone, and a missing/anonymous user is denied (fail-closed). This gate governs VISIBILITY of
    read-only monitoring data only — it grants NO observation/replication/execution authority whatsoever."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return (getattr(user, "email", "") or "").strip().lower() in copier_ui_cohort_allowlist()


def copier_execute_enabled() -> bool:
    """Master gate for the B-exec DEMO ExecutionJob PRODUCER. DEFAULT OFF, ANDed with the master gate.

    BUILD != ARM: enabling this lets the producer run, but it creates SUPPRESSED PLACE_ORDER_SHADOW
    jobs (no order) unless ``copier_execute_demo_armed()`` is ALSO on. Even then, DEMO followers only —
    never LIVE, never bypassing the demo-only bridge close/modify walls, kill switch, pin or readiness."""
    return copier_enabled() and _flag("COPIER_EXECUTE_ENABLED")


def copier_execute_demo_armed() -> bool:
    """When on (AND execute enabled), the producer creates REAL DEMO ``PLACE_ORDER`` jobs instead of
    suppressed SHADOW jobs — DEMO followers ONLY. DEFAULT OFF. Arming is a separate operator-gated step:
    building the producer does NOT authorize arming it, and it never reaches a LIVE destination."""
    return copier_execute_enabled() and _flag("COPIER_EXECUTE_DEMO_ARMED")


def copier_reconcile_enabled() -> bool:
    """Gate for the follower-ticket reconciliation pass (ADR-0052). DEFAULT OFF, ANDed with the master gate.

    Reconciliation is READ-ONLY: it reads a follower's live positions through the identity firewall and
    records which broker ticket a copied OPEN produced (``CopierReplication.follower_ticket``). It never
    places, closes or modifies an order. It is the prerequisite for any follower-position-targeting
    lifecycle op (CLOSE/MODIFY), which must never act by symbol resemblance."""
    return copier_enabled() and _flag("COPIER_RECONCILE_ENABLED")


def execution_status(*, note: str = "Saving configuration does not activate trading.") -> dict:
    """The copier's execution posture for the UI, DERIVED LIVE from the real execute flags — never a
    hardcoded label. While the execute producer is off (the default) this is observation/shadow and reads
    EXACTLY as the historical constant did (``mode='observation'``, ``execution='off'``, label
    'Observation mode'), so nothing in the UI changes today. It changes the instant a Sponsor arms the
    producer, so the UI can never keep asserting "EXECUTION OFF" while real DEMO orders are being produced.
    LIVE follower execution is never reachable from the copier (the execution LIVE wall governs that) and
    is therefore always reported blocked. The ``note`` is surface-specific (config vs catalogue matrix)."""
    execute = copier_execute_enabled()
    demo_armed = copier_execute_demo_armed()
    if demo_armed:
        mode, execution, label, detail = (
            "demo_execution", "demo_armed", "DEMO execution armed",
            "DEMO follower orders are being produced (DEMO followers only — never LIVE).")
    elif execute:
        mode, execution, label, detail = (
            "shadow_jobs", "shadow_jobs", "Execution producer on — SHADOW jobs",
            "Suppressed SHADOW jobs only; no broker order is sent until DEMO is armed.")
    else:
        mode, execution, label, detail = (
            "observation", "off", "Observation mode", "Copy execution off")
    return {
        "mode": mode, "execution": execution, "label": label, "detail": detail, "note": note,
        # The live flag inputs this status is derived from (transparency/audit; plain booleans, never a secret).
        "copier_enabled": copier_enabled(),
        "execute_enabled": execute,
        "demo_armed": demo_armed,
        "live_follower_execution": "blocked",
    }


def execution_banner() -> str:
    """The compact safety banner, DERIVED LIVE from the execute flags. Byte-identical to the historical
    constant while the producer is off; switches to an unambiguous armed label the moment a Sponsor arms
    the DEMO producer, so the banner can never be a stale "EXECUTION OFF" over live DEMO execution."""
    execution = execution_status()["execution"]
    if execution == "off":
        return "SHADOW — EXECUTION OFF"
    if execution == "shadow_jobs":
        return "SHADOW JOBS — NO ORDER SENT"
    return "DEMO EXECUTION ARMED — DEMO ONLY"
