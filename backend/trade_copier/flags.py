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
