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
    account.validation_status = _STATUS_MAP.get(status, TradingAccount.ValidationStatus.TECHNICAL_ERROR)
    update_fields = ["validation_status", "updated_at"]
    if status == "HEALTHY":
        account.validated_at = timezone.now()
        update_fields.append("validated_at")
    account.save(update_fields=update_fields)
    return attempt


def replace_credentials(account, new_password, *, actor="", request=None, revalidate=False, validator=None) -> dict:
    """Replace the stored broker credential (re-encrypt at rest; plaintext dropped immediately) and audit
    the rotation. Optionally re-validate. Returns a secret-safe result — never the password or ciphertext."""
    from core.audit import log_customer_credential_event

    from .crypto import encrypt_password

    account.password_enc = encrypt_password(new_password)
    account.broker_password = ""
    account.save(update_fields=["password_enc", "broker_password", "updated_at"])
    log_customer_credential_event("ROTATED", account=account, actor=actor, request=request, purpose="replace")

    result = {"replaced": True}
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
    return {
        "disconnected": True,
        "credential_destroyed": bool(evidence.get("had_credential")),
        "row_deleted": False,
    }


def attempt_public(attempt) -> dict:
    """The secret-safe public projection of a validation attempt (no account internals)."""
    return {
        "id": attempt.id,
        "trigger": attempt.trigger,
        "status": attempt.status,
        "reason_code": attempt.reason_code,
        "retryable": attempt.retryable,
        "is_demo": attempt.is_demo,
        "server": attempt.server,
        "login_masked": attempt.login_masked,
        "correlation_id": attempt.correlation_id,
        "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
    }
