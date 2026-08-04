"""WP3 (ADR-0030) — Continuous Broker Health Engine.

The single authoritative, deterministic broker-health state machine. It folds broker-login
validation *evidence* (``trading.BrokerAccountValidationAttempt`` outcomes) plus time (staleness)
and account lifecycle (``TradingAccount.disconnected_at`` → TOMBSTONED) into one per-account state
(``reliability.BrokerAccountHealth``). WP1B/WP2 consume the convergence contract; this module only
*emits signals* (audit + deduplicated notifications). It NEVER pauses/resumes a runtime, places an
order, logs into a broker, or reads a credential.

Ships DARK: every public entry point is a no-op unless ``BROKER_CONNECTIVITY_HEALTH_ENABLED`` is
truthy. Transitions are pure functions of (current state, counters, outcome, thresholds, clock), so
the same evidence always yields the same state.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from core.audit import log_event

from .constants import Component, broker_health_config, broker_health_enabled
from .models import AlertEvent, BrokerAccountHealth

State = BrokerAccountHealth.State

# ── Outcome classification (WP1A attempt.status → engine outcome) ──
SUCCESS = "success"
FAILURE_SOFT = "failure_soft"   # NEEDS_ATTENTION — auth/attention (→ DEGRADED)
FAILURE_HARD = "failure_hard"   # UNAVAILABLE — broker unreachable/technical (→ DISCONNECTED)

_ADVERSE_KIND_STATE = {FAILURE_SOFT: State.DEGRADED, FAILURE_HARD: State.DISCONNECTED}

# Stable, customer-safe reason codes.
REASON_VALIDATED = "validated"
REASON_RECOVERED = "recovered"
REASON_DEGRADED = "degraded_auth"
REASON_DISCONNECTED = "broker_unreachable"
REASON_STALE = "stale_no_recent_success"
REASON_TOMBSTONED = "account_disconnected"


def classify_status(status: str) -> str:
    """Map a WP1A ``BrokerAccountValidationAttempt.status`` to an engine outcome. Fail-safe: any
    unrecognised status counts as a *soft failure*, never a success — an unknown state must never
    make an account look healthier than proven."""
    s = (status or "").strip().upper()
    if s == "HEALTHY":
        return SUCCESS
    if s == "UNAVAILABLE":
        return FAILURE_HARD
    return FAILURE_SOFT  # NEEDS_ATTENTION and anything unexpected


# ── Pure transition helpers (mutate the in-memory model; caller saves) ──
def _set_state(health: BrokerAccountHealth, new_state, reason: str) -> None:
    """Set state + reason. Bump ``state_version`` by exactly one iff the state actually changes."""
    if health.state != new_state:
        health.state = new_state
        health.state_version += 1
    health.reason_code = reason


def _apply_success(health: BrokerAccountHealth, now, cfg: dict) -> None:
    if health.state == State.TOMBSTONED:
        return  # terminal
    health.consecutive_successes += 1
    health.consecutive_failures = 0
    health.last_success_at = now
    if health.state in BrokerAccountHealth.RECOVERABLE_STATES:
        if health.consecutive_successes >= cfg["success_threshold"]:
            _set_state(health, State.HEALTHY, REASON_RECOVERED)
            health.resume_eligible = True
    elif health.state == State.UNKNOWN:
        _set_state(health, State.HEALTHY, REASON_VALIDATED)
        # First-ever validation: nothing was paused, so no resume signal.
    # HEALTHY stays HEALTHY; keep reason authoritative but do not bump version.
    elif health.state == State.HEALTHY:
        health.reason_code = REASON_VALIDATED


def _apply_failure(health: BrokerAccountHealth, kind: str, now, cfg: dict) -> None:
    if health.state == State.TOMBSTONED:
        return  # terminal
    health.consecutive_failures += 1
    health.consecutive_successes = 0
    # Adverse sub-state is *latched* on the threshold-crossing failure. While already adverse we keep
    # counting but never flip DEGRADED↔DISCONNECTED (that would flap on mixed failures); only a
    # recovery to HEALTHY clears the latch and lets a fresh storm re-pick the sub-state.
    if health.state in BrokerAccountHealth.RECOVERABLE_STATES:
        return
    if health.consecutive_failures >= cfg["failure_threshold"]:
        target = _ADVERSE_KIND_STATE[kind]
        reason = REASON_DISCONNECTED if target == State.DISCONNECTED else REASON_DEGRADED
        _set_state(health, target, reason)
        health.resume_eligible = False


def _apply_stale(health: BrokerAccountHealth, now, cfg: dict) -> None:
    """Time-driven: a HEALTHY account with no recent successful validation becomes STALE."""
    if health.state != State.HEALTHY:
        return
    last = health.last_success_at
    if last is None or (now - last).total_seconds() > cfg["stale_timeout_s"]:
        _set_state(health, State.STALE, REASON_STALE)
        health.resume_eligible = False


def _apply_tombstone(health: BrokerAccountHealth) -> None:
    if health.state != State.TOMBSTONED:
        _set_state(health, State.TOMBSTONED, REASON_TOMBSTONED)
        health.resume_eligible = False


# ── Signal emission (audit + deduplicated notifications) ──
_ENTER_EVENT = {
    State.DEGRADED: "BROKER_HEALTH_DEGRADED",
    State.DISCONNECTED: "BROKER_HEALTH_DISCONNECTED",
    State.STALE: "BROKER_HEALTH_STALE_DETECTED",
    State.TOMBSTONED: "BROKER_HEALTH_TOMBSTONED",
}
_ENTER_SEVERITY = {
    State.DEGRADED: AlertEvent.Severity.WARN,
    State.DISCONNECTED: AlertEvent.Severity.CRITICAL,
    State.STALE: AlertEvent.Severity.WARN,
    State.TOMBSTONED: AlertEvent.Severity.INFO,
}
_ENTER_TITLE = {
    State.DEGRADED: "Broker account health degraded",
    State.DISCONNECTED: "Broker account disconnected",
    State.STALE: "Broker account validation stale",
    State.TOMBSTONED: "Broker account decommissioned",
}


def _dedup_key(account_id, state) -> str:
    return f"BROKER_HEALTH:{account_id}:{state}"


def _emit_signals(account, health: BrokerAccountHealth, old_state, old_resume: bool) -> None:
    """Emit audit events + durable, deduplicated notifications for the NET change of one update.
    Secret-free: only account id, states, reason and counters ever leave here."""
    meta = health.contract()
    meta["from_state"] = old_state
    if health.state != old_state:
        if health.state == State.HEALTHY:
            event = "BROKER_HEALTH_RECOVERED" if old_state != State.UNKNOWN else "BROKER_HEALTH_VALIDATED"
            severity = "INFO"
            _resolve_open_alerts(account)
        else:
            event = _ENTER_EVENT.get(health.state, "BROKER_HEALTH_TRANSITION")
            severity = "CRITICAL" if health.state == State.DISCONNECTED else "WARN"
            _open_alert(account, health)
        log_event(None, event, severity=severity, entity_type="trading_account",
                  entity_id=account.pk, metadata=meta)
        # State entering a pause-state is itself the "pause required" signal for WP1B.
        if health.pause_required:
            log_event(None, "BROKER_HEALTH_PAUSE_REQUIRED", severity="WARN",
                      entity_type="trading_account", entity_id=account.pk, metadata=meta)
    # Resume edge can occur even when the *net* state is unchanged (adverse→HEALTHY inside one fold).
    if health.resume_eligible and not old_resume:
        log_event(None, "BROKER_HEALTH_RESUME_ELIGIBLE", severity="INFO",
                  entity_type="trading_account", entity_id=account.pk, metadata=meta)


def _open_alert(account, health: BrokerAccountHealth) -> None:
    """Create (or dedup onto) a single OPEN notification for the account's current adverse state."""
    AlertEvent.objects.get_or_create(
        dedup_key=_dedup_key(account.pk, health.state), status=AlertEvent.Status.OPEN,
        defaults={
            "severity": _ENTER_SEVERITY.get(health.state, AlertEvent.Severity.WARN),
            "component": Component.MT5_BROKER,
            "trading_account": account,
            "title": _ENTER_TITLE.get(health.state, "Broker account health change"),
            "body": f"Broker health for account {account.pk} is {health.state} "
                    f"(reason={health.reason_code}).",
            "detail": {"reason_code": health.reason_code, "state": health.state,
                       "state_version": health.state_version},
        },
    )


def _resolve_open_alerts(account) -> None:
    """On recovery to HEALTHY, resolve any OPEN broker-health alerts for the account so the adverse
    notification clears rather than lingering."""
    now = timezone.now()
    AlertEvent.objects.filter(
        trading_account=account, status=AlertEvent.Status.OPEN,
        dedup_key__startswith=f"BROKER_HEALTH:{account.pk}:",
    ).update(status=AlertEvent.Status.RESOLVED, resolved_at=now)


# ── Public service API (all flag-gated; no-op when DARK) ──
def get_contract(account) -> dict | None:
    """Return the current convergence contract for WP1B/WP2, or None when the engine is DARK or no
    health row exists yet. Read-only — never creates a row."""
    if not broker_health_enabled():
        return None
    health = BrokerAccountHealth.objects.filter(account=account).first()
    return health.contract() if health else None


def record_validation_outcome(account, *, now=None, config=None) -> dict | None:
    """Fold every not-yet-consumed validation attempt for ``account`` into its health state, then run
    the staleness check. Idempotent: attempts are consumed exactly once via the id watermark, so a
    repeat call with no new evidence is a no-op that returns the unchanged contract.

    Returns the convergence contract, or None when the engine is DARK. Serialised per-account with
    ``select_for_update`` so concurrent callers cannot double-consume or clobber the version."""
    if not broker_health_enabled():
        return None
    now = now or timezone.now()
    cfg = config or broker_health_config()

    with transaction.atomic():
        health, _created = BrokerAccountHealth.objects.select_for_update().get_or_create(account=account)
        old_state, old_resume = health.state, health.resume_eligible

        if account.disconnected_at is not None:
            _apply_tombstone(health)
        elif health.state != State.TOMBSTONED:
            watermark = health.last_consumed_attempt_id or 0
            attempts = list(
                account.validation_attempts.filter(id__gt=watermark).order_by("id")
            )
            for attempt in attempts:
                kind = classify_status(attempt.status)
                if kind == SUCCESS:
                    _apply_success(health, now, cfg)
                else:
                    _apply_failure(health, kind, now, cfg)
                health.last_attempt_at = attempt.created_at or now
                health.last_consumed_attempt_id = attempt.id
            _apply_stale(health, now, cfg)

        health.save()
        _emit_signals(account, health, old_state, old_resume)
        return health.contract()


def sweep_stale(account, *, now=None, config=None) -> dict | None:
    """Run only the time-driven staleness check for one account (no attempt consumption). Used by the
    scheduler between validations. No-op when DARK or when no health row exists."""
    if not broker_health_enabled():
        return None
    now = now or timezone.now()
    cfg = config or broker_health_config()
    with transaction.atomic():
        health = BrokerAccountHealth.objects.select_for_update().filter(account=account).first()
        if health is None or health.state != State.HEALTHY:
            return health.contract() if health else None
        old_state, old_resume = health.state, health.resume_eligible
        _apply_stale(health, now, cfg)
        if health.state != old_state:
            health.save()
            _emit_signals(account, health, old_state, old_resume)
        return health.contract()
