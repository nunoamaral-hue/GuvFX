"""WP1A — Broker Connectivity backend service layer (ADR-0028).

The customer-facing broker-account lifecycle built ON the certified in-place broker-login validation
primitive (ADR-0027). Everything here is gated by the ``BROKER_CONNECTIVITY_ENABLED`` feature flag
(default OFF) so incomplete work merges DARK in production.

Invariants (see ADR-0028):
- **Fail-closed.** The validator never raises; a mis-provisioned channel yields an ``UNAVAILABLE``
  outcome, never a false credential verdict.
- **Secret-safe.** Nothing here returns or logs a password, ciphertext, envelope, HMAC or host path —
  only the allow-listed ``ValidationOutcome`` fields.
- **Disconnect is a TOMBSTONE.** Soft-disconnect (``is_active=False`` + ``disconnected_at``) plus verified
  credential destruction (P3-D), NEVER a row delete — immutable ``Trade``/execution history and PROTECT
  relations are preserved.
- **Customer-flow persistence only.** ``run_broker_validation`` persists ``validation_status`` because it
  is invoked from a customer account flow (add/edit/test/retry/replace). The ADR-0027 manual certification
  path does NOT call this and stays stateless (the Customer-Zero-stateless invariant).
"""
from __future__ import annotations

import os

from django.utils import timezone

from .models import BrokerAccountValidationAttempt, TradingAccount


def broker_connectivity_enabled() -> bool:
    """Master WP1A flag. Default OFF: absent/false → the whole customer broker-connectivity surface is
    dark (endpoints 404, services refuse)."""
    return os.getenv("BROKER_CONNECTIVITY_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


# ValidationOutcome.status (ADR-0027) → durable TradingAccount.ValidationStatus.
_STATUS_MAP = {
    "HEALTHY": TradingAccount.ValidationStatus.VALIDATED,
    "NEEDS_ATTENTION": TradingAccount.ValidationStatus.CONNECTION_FAILED,
    "UNAVAILABLE": TradingAccount.ValidationStatus.TECHNICAL_ERROR,
}

# WS-C (2026-08-05) — outcomes that are NOT a verdict on the customer's credential or on the broker: they mean
# "the check could not be completed" (host/platform/transient — the validation host's MT5 IPC was unavailable,
# the single-flight validator was busy, a runner/agent platform fault, etc.). Such an outcome must NEVER
# downgrade a previously-successful validation — a busy / host-IPC-unavailable / could-not-verify attempt is
# not evidence the credential went bad. A prior VALIDATED account keeps VALIDATED (and its ``validated_at``);
# the failed attempt is still recorded in the append-only history and shown as the latest attempt. This is the
# exact set of UNAVAILABLE-bucket reasons in ``terminal_provisioning.broker_login_validation._TAXONOMY`` (a
# consistency test guards the two from drifting). Authoritative verdicts — HEALTHY, or a real credential/broker
# rejection (invalid_password/invalid_login/account_disabled/server_not_found/classification_mismatch/
# credential_missing/broker_server_missing) — are deliberately EXCLUDED, so they still update the durable status.
_NON_AUTHORITATIVE_REASONS = frozenset({
    "validation_ipc_unavailable", "validation_busy", "validation_unconfigured", "could_not_verify",
    "login_timeout", "server_unavailable", "bridge_unavailable", "mt5_unavailable", "runtime_unavailable",
    "validation_runner_unavailable", "validation_runner_timeout", "diagnostic_capture_failed",
    "credential_scrub_unverified", "validation_baseline_dirty", "isolation_check_failed",
    "credential_unsealable", "impl_integrity_mismatch", "payload_missing", "payload_digest_mismatch",
    "agent_internal_error",
})


def _make_validator():
    """Production validator resolves its signed channel + agent base URL from settings/env, exactly like
    the certified path. Injectable in tests via the ``validator`` argument. NOTE: the backend is
    seal-only; the HMAC signing keyring is provisioned to the caller at ARMING time (a gated deploy
    concern) — never committed here. Absent that, ``validate`` returns ``validation_unconfigured``
    (UNAVAILABLE), which is the correct fail-closed behaviour while the flag is OFF."""
    from terminal_provisioning.broker_login_validation import BrokerLoginValidator
    return BrokerLoginValidator()


def run_broker_validation(account, *, trigger, actor="", request=None, validator=None) -> BrokerAccountValidationAttempt:
    """Run ONE non-destructive broker-login validation for a customer account flow, RECORD a secret-safe
    ``BrokerAccountValidationAttempt``, and persist the durable ``validation_status``/``validated_at``.
    Returns the created attempt. ``validate`` never raises; a defensive guard maps any unexpected error to
    a fail-closed UNAVAILABLE attempt so the caller always gets a durable record."""
    _fail = {"status": "UNAVAILABLE", "reason": "could_not_verify", "retryable": True,
             "server": "", "login_masked": "", "is_demo": None, "correlation_id": ""}
    try:
        # Validator CONSTRUCTION is inside the guard too: an import/config breakage must fail closed to a
        # durable UNAVAILABLE attempt, never a 500.
        v = validator or _make_validator()
        got = v.validate(account).as_dict()
        d = got if isinstance(got, dict) else dict(_fail)
    except Exception:  # noqa: BLE001 — no-raise contract; stay fail-closed on ANY error
        d = dict(_fail)

    status = str(d.get("status") or "UNAVAILABLE")
    is_demo = d.get("is_demo")
    # Defensive truncation to the exact column widths — a long-but-valid upstream value (e.g. a 160-char
    # ``server_name``) must never raise at insert and defeat the fail-closed contract.
    attempt = BrokerAccountValidationAttempt.objects.create(
        account=account,
        trigger=trigger,
        status=status[:20],
        reason_code=str(d.get("reason") or "")[:64],
        retryable=bool(d.get("retryable")),
        is_demo=is_demo if isinstance(is_demo, bool) else None,
        server=str(d.get("server") or "")[:160],
        login_masked=str(d.get("login_masked") or "")[:32],
        correlation_id=str(d.get("correlation_id") or "")[:128],
    )

    # Durable per-account state — customer-flow only (see module docstring).
    reason = str(d.get("reason") or "")
    new_status = _STATUS_MAP.get(status, TradingAccount.ValidationStatus.TECHNICAL_ERROR)
    # WS-C: preserve the last successful validation. A non-authoritative outcome (see _NON_AUTHORITATIVE_REASONS)
    # against an already-VALIDATED account must not overwrite the durable VALIDATED status — the attempt is still
    # persisted above (so the history / latest-attempt view reflects it), but the badge stays "Validated".
    was_validated = (account.validation_status == TradingAccount.ValidationStatus.VALIDATED
                     and account.validated_at is not None)
    if status != "HEALTHY" and was_validated and reason in _NON_AUTHORITATIVE_REASONS:
        new_status = TradingAccount.ValidationStatus.VALIDATED
    account.validation_status = new_status
    update_fields = ["validation_status", "updated_at"]
    if status == "HEALTHY":
        account.validated_at = timezone.now()
        update_fields.append("validated_at")
    account.save(update_fields=update_fields)

    # WP1B/WP2 convergence: fold this fresh outcome into the WP3 health engine so the convergence
    # contract reflects the latest evidence *immediately* — a freshly-validated account converges to
    # HEALTHY on the customer flow, not only on the next (inert) scheduler cycle. No-op when the health
    # engine is DARK. Fail-open: a health-engine error must never break the customer validation flow.
    try:
        from reliability.broker_health import record_validation_outcome
        record_validation_outcome(account)
        # WP1B/WP2: reconcile the durable broker-pause record with the freshly-folded contract, so a
        # validation that degrades health persists a pause (and a recovery records resume eligibility) —
        # never resuming. No-op unless both broker-connectivity flags are on.
        from execution.runtime_pause import process_broker_health_pause
        process_broker_health_pause(account)
    except Exception:  # noqa: BLE001 — health ingestion is best-effort; the durable attempt already exists
        from core.audit import log_event
        log_event(request, "BROKER_HEALTH_INGEST_ERROR", severity="WARN",
                  entity_type="TradingAccount", entity_id=getattr(account, "pk", None), metadata={})

    # WP5.2 — mirror this committed validation outcome onto the operational read model (DARK/fail-open/
    # on-commit; the attempt row is the authoritative record — this is a projection only).
    from operational_events import broker_projection
    broker_projection.project_validation(
        account, attempt_id=attempt.id, status=attempt.status, reason_code=attempt.reason_code,
        retryable=attempt.retryable, is_demo=attempt.is_demo, trigger=attempt.trigger,
        correlation_id=attempt.correlation_id)
    return attempt


def replace_credentials(account, new_password, *, actor="", request=None, revalidate=False, validator=None) -> dict:
    """Replace the stored broker credential (re-encrypt at rest; plaintext dropped immediately) and
    **atomically invalidate prior broker eligibility** (WP1B/WP2, ADR-0029): the old credential's
    validation can no longer authorise execution. In one transaction the credential is rotated,
    ``validation_status`` returns to NEVER, ``validated_at`` is cleared, and — when the WP3 health engine
    is enabled — health is reset to UNKNOWN (non-eligible, no resume) until a fresh successful validation.
    The append-only validation-attempt history is preserved. Returns a secret-safe result — never the
    password or ciphertext. Any optional re-validation runs *after* the atomic invalidation (its network
    I/O must not hold the transaction open)."""
    from django.db import transaction

    from core.audit import log_customer_credential_event, log_event

    from .crypto import encrypt_password

    # Atomic: rotate + invalidate together, so a failure can never leave a new credential paired with a
    # stale VALIDATED status (which would let the gate authorise execution on unverified credentials).
    with transaction.atomic():
        account.password_enc = encrypt_password(new_password)
        account.broker_password = ""
        account.validation_status = TradingAccount.ValidationStatus.NEVER
        account.validated_at = None
        account.save(update_fields=[
            "password_enc", "broker_password", "validation_status", "validated_at", "updated_at"])
        try:
            from reliability.broker_health import invalidate_for_credential_replacement
            invalidate_for_credential_replacement(account)  # no-op when the health engine is DARK
        except Exception:  # noqa: BLE001 — health invalidation must not abort the credential rotation;
            # the execution gate already fails closed on validation_status=NEVER regardless of health.
            log_event(request, "BROKER_HEALTH_INVALIDATION_ERROR", severity="WARN",
                      entity_type="TradingAccount", entity_id=getattr(account, "pk", None), metadata={})
    log_customer_credential_event("ROTATED", account=account, actor=actor, request=request, purpose="replace")
    log_event(request, "BROKER_VALIDATION_INVALIDATED", severity="INFO",
              entity_type="TradingAccount", entity_id=getattr(account, "pk", None),
              metadata={"trigger": "credential_replace"})

    # WP5.2 — project the committed credential replacement (before any re-validation, which self-projects).
    from operational_events import broker_projection
    broker_projection.project_credential_rotation(
        account, resulting_status=str(account.validation_status),
        updated_at_iso=account.updated_at.isoformat() if account.updated_at else "",
        validation_invalidated=True)

    result = {"replaced": True, "validation_invalidated": True}
    if revalidate:
        attempt = run_broker_validation(account, trigger="replace", actor=actor, request=request, validator=validator)
        result["validation"] = attempt_public(attempt)
    return result


def disconnect_account(account, *, actor="", request=None) -> dict:
    """TOMBSTONE a broker account: verified credential destruction (P3-D) + soft-disconnect
    (``is_active=False`` + ``disconnected_at``) + reset ``validation_status``. NEVER row-deletes — the
    account row, its immutable ``Trade``/execution history and PROTECT relations are all retained.
    Idempotent + fail-closed."""
    from django.db import transaction

    from .credential_lifecycle import destroy_customer_credential

    # Atomic tombstone: the credential destruction + soft-disconnect commit together or not at all, so a
    # failure can never leave a credential-destroyed-but-still-active account.
    with transaction.atomic():
        evidence = destroy_customer_credential(account, actor=actor, request=request)
        account.is_active = False
        account.disconnected_at = timezone.now()
        account.validation_status = TradingAccount.ValidationStatus.NEVER
        account.save(update_fields=["is_active", "disconnected_at", "validation_status", "updated_at"])

    # WP5.2 — project the committed disconnect/tombstone (credential-destroyed folded into metadata).
    from operational_events import broker_projection
    broker_projection.project_disconnect(
        account, disconnected_at_iso=account.disconnected_at.isoformat() if account.disconnected_at else "",
        credential_destroyed=bool(evidence.get("had_credential")))
    return {
        "disconnected": True,
        "credential_destroyed": bool(evidence.get("had_credential")),
        "row_deleted": False,
    }


def attempt_public(attempt) -> dict:
    """The secret-safe, CUSTOMER-facing public projection of a validation attempt (no account internals).
    Phase-4 WS-C (S2): ``correlation_id`` is an operator diagnostic and is deliberately NOT included — this
    dict is returned to the customer on the replace-credentials flow, and it must match the customer-facing
    ``BrokerValidationAttemptSerializer`` allow-list (which dropped correlation_id in WS-P3). The staff
    validation-timeline endpoint reads the correlation id from the model directly (staff-gated)."""
    return {
        "id": attempt.id,
        "trigger": attempt.trigger,
        "status": attempt.status,
        "reason_code": attempt.reason_code,
        "retryable": attempt.retryable,
        "is_demo": attempt.is_demo,
        "server": attempt.server,
        "login_masked": attempt.login_masked,
        "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
    }
