"""ADR-0034 Execution Engine (G9) — active-account switch → pause / safe resume (DARK, demo-only).

If the user switches the active broker account inside MT5, strategies bound to the previous account must
stop. GuvFX never calls ``mt5.login()``, never auto-switches the account, and never replays long-stale
signals.

Authority: the fail-closed readiness gate is the pause/resume authority, re-evaluated FRESH on every
dispatch (``broker_gate.evaluate_dispatch_gate`` → ``readiness``). So:

- **Pause on switch** — when the observer persists a mismatch/disconnect (``proj_account_match`` False,
  a non-``EXECUTION_READY`` canonical state, or stale ``last_decision_at``), the account is no longer
  execution-eligible; new jobs fail closed at creation and at claim; existing stale signals are DROPPED
  (re-validated at dispatch and rejected), never blindly replayed. This reuses the ONE existing gate rather
  than inventing a parallel pause system.
- **Safe resume on return** — when the expected account returns AND is connected AND trade-allowed AND the
  observation is fresh AND the workspace is still armed, readiness passes again and execution eligibility is
  restored. Resume is therefore automatic *re-eligibility*, never a blind un-pause — every condition is
  re-checked, so a resume can never fire while disconnected / mismatched / stale / unarmed. Idempotent
  (pure function of persisted state).

This module exposes that pause/resume view + the Phase-1 signal-queue policy. It performs no order, attach,
login, launch, or account switch, and writes no canonical state (that stays the M3c writer's).
"""
from __future__ import annotations

from execution.hosted_routing import resolve_hosted_route


def hosted_execution_effectively_paused(account) -> bool:
    """G9 pause view: True when a Hosted Workspace account is NOT currently execution-eligible (active-account
    mismatch / disconnect / non-ready / stale / unarmed / dark). Fail-closed: any non-OK route ⇒ paused.
    Because this is the readiness-driven gate, it is exactly what stops execution on an account switch."""
    return not resolve_hosted_route(account).ok


def should_drop_stale_hosted_signal(account) -> bool:
    """Phase-1 signal-queue policy: DROP (do not queue/bank) a hosted signal while the account is not
    execution-eligible. There is no unbounded queue; every dispatch re-validates, so a signal valid when
    created is never blindly executed later. Returns True when the signal must be dropped."""
    return hosted_execution_effectively_paused(account)


def hosted_switch_status(account) -> dict:
    """A small, secret-free operational view of the switch/pause state for observability."""
    route = resolve_hosted_route(account)
    return {
        "execution_eligible": route.ok,
        "effectively_paused": not route.ok,
        "reason_code": route.reason_code,
        "stale_signal_policy": "drop",
    }
