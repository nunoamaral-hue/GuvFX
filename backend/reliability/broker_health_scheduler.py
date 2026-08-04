"""WP3 (ADR-0030) — Broker Health scheduler *framework*.

This is deliberately inert. It provides the cadence / backoff / jitter / quota / single-flight
machinery a future arming step would use to re-validate accounts on a schedule, but it performs NO
recurring live validation itself:

* When ``BROKER_CONNECTIVITY_HEALTH_ENABLED`` is OFF, ``run_cycle`` is a hard no-op (no DB writes).
* Even when ON, ``run_cycle`` requires an explicitly injected ``validator``. With none it stays inert
  (``no_validator``) — the framework never invents a live broker login. Tests pass mocks; wiring a
  real validator is a separate, Sponsor-gated arming step.

Backoff and jitter are fully deterministic (jitter is derived from a hash of identity, never a random
source), so a given account + clock always schedules to the same instant.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .broker_health import record_validation_outcome
from .constants import broker_health_config, broker_health_enabled
from .models import BrokerAccountHealth

State = BrokerAccountHealth.State


def next_interval_s(consecutive_failures: int, cfg: dict) -> float:
    """Deterministic exponential backoff clamped to ``max_interval_s``. Zero failures → base interval;
    each failure multiplies by ``backoff_factor``. Computed as an iterative product with an early
    clamp so it is overflow-safe for *any* configured factor (float multiplication saturates to
    ``inf``, which the ``>= max_s`` check catches before it can propagate)."""
    base = cfg["base_interval_s"]
    factor = cfg["backoff_factor"]
    max_s = cfg["max_interval_s"]
    n = max(0, int(consecutive_failures))
    if factor <= 1.0 or n == 0:
        return float(min(base, max_s))
    interval = float(base)
    for _ in range(min(n, 64)):  # bounded work; with factor > 1 the clamp fires well before 64
        interval *= factor
        if interval >= max_s:
            return float(max_s)
    return float(min(interval, max_s))


def _jitter_fraction(seed: str) -> float:
    """Deterministic fraction in [0, 1) derived from a hash of the identity seed — never random, so a
    replayed cycle schedules identically."""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / float(0x100000000)


def compute_next_check_at(health: BrokerAccountHealth, now, cfg: dict):
    """When this account should next be re-validated: ``now`` + backoff(failures), spread by a
    deterministic *downward-only* jitter so the scheduled interval stays within
    ``[interval·(1-jitter_frac), interval]`` and therefore never exceeds ``max_interval_s``."""
    interval = next_interval_s(health.consecutive_failures, cfg)  # already ≤ max_interval_s
    jitter_frac = min(max(0.0, cfg["jitter_frac"]), 1.0)
    if jitter_frac > 0:
        seed = f"{health.account_id}:{health.state_version}:{health.consecutive_failures}"
        interval -= interval * jitter_frac * _jitter_fraction(seed)
    return now + timedelta(seconds=interval)


def select_due_query(now, cfg):
    """Accounts eligible for a scheduled check: not terminal, and due (never scheduled, or past due),
    ordered soonest-first, capped at the per-cycle quota. Does NOT lock — callers that mutate must use
    ``run_cycle`` (which locks with ``skip_locked`` for single-flight)."""
    quota = cfg["quota_per_cycle"]
    return (
        BrokerAccountHealth.objects
        .exclude(state=State.TOMBSTONED)
        .filter(Q(next_check_at__isnull=True) | Q(next_check_at__lte=now))
        .order_by(F("next_check_at").asc(nulls_first=True), "account_id")[:quota]
    )


def run_cycle(*, now=None, validator=None, cfg=None) -> dict:
    """Run one scheduler cycle.

    DARK / inert paths (no live validation ever performed by WP3 itself):
      * flag OFF                → ``{"ran": False, "reason": "disabled", ...}`` (no DB writes)
      * flag ON, validator None → ``{"ran": False, "reason": "no_validator", ...}`` (no DB writes)

    Armed path (tests inject a mock validator; production arming is separate + Sponsor-gated): claim up
    to ``quota_per_cycle`` due accounts under a ``skip_locked`` lock (single-flight across concurrent
    cycles), advancing ``next_check_at`` on claim so a peer cycle won't re-pick them; then, outside the
    lock, run ``validator(account)`` (expected to append a validation attempt) and fold the result via
    ``record_validation_outcome``. ``validator`` must be a callable; WP3 supplies none."""
    result = {"ran": False, "reason": "", "claimed": 0, "validated": 0, "errors": 0}
    if not broker_health_enabled():
        result["reason"] = "disabled"
        return result
    if validator is None:
        result["reason"] = "no_validator"
        return result

    now = now or timezone.now()
    cfg = cfg or broker_health_config()

    # Phase 1 — claim due accounts atomically (single-flight via skip_locked) and push their next check
    # forward so a concurrent cycle skips them. We snapshot the accounts to validate outside the lock.
    claimed = []
    with transaction.atomic():
        due = list(
            BrokerAccountHealth.objects
            .select_for_update(skip_locked=True)
            .exclude(state=State.TOMBSTONED)
            .filter(Q(next_check_at__isnull=True) | Q(next_check_at__lte=now))
            .order_by(F("next_check_at").asc(nulls_first=True), "account_id")[: cfg["quota_per_cycle"]]
        )
        for health in due:
            health.next_check_at = compute_next_check_at(health, now, cfg)
            health.save(update_fields=["next_check_at", "updated_at"])
            claimed.append(health.account)
    result["claimed"] = len(claimed)

    # Phase 2 — validate each claimed account (outside the lock). A single account's failure must not
    # abort the cycle: isolate it, count it, continue.
    for account in claimed:
        try:
            validator(account)
            record_validation_outcome(account, now=now, config=cfg)
            result["validated"] += 1
        except Exception:  # noqa: BLE001 — one bad account can't take down the cycle
            result["errors"] += 1

    result["ran"] = True
    result["reason"] = "ok"
    return result
