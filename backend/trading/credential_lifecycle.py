"""Phase 3 (P3-D) — customer-credential verified destruction.

Scope (honest — do not overstate). The current envelope uses a single global MultiFernet (P3-A), so
destruction here is a **secure clear** of the stored ciphertext (``password_enc`` + the legacy
``broker_password``), recorded with an append-only ``CREDENTIAL_DESTROYED`` audit + destruction
evidence (redacted, no secret). Per-customer **crypto-shred** — destroying a per-customer key id so
the ciphertext is mathematically unrecoverable — requires the per-customer key boundary, which is an
ADR-0019 *strategic* item that is NOT yet built. This module MUST NOT claim crypto-shred.

This also serves the ADR-0019 principle that a broker password is an onboarding artefact whose
operational lifetime should be minimised: once the runtime holds its own broker session, the platform
should evict the stored password via ``destroy_customer_credential``.
"""
from django.db import transaction

from core.audit import log_customer_credential_event


def destroy_customer_credential(account, *, actor="", request=None) -> dict:
    """Securely clear a ``TradingAccount``'s stored broker credential and record destruction evidence.

    Idempotent: safe to call when there is nothing to clear (``had_credential`` is then False). Returns
    a non-secret evidence dict. Emits a redacted ``CREDENTIAL_DESTROYED`` audit (fail-open). The clear
    is persisted only when the row still exists (``pk`` set); when called just before the row is
    deleted, the delete removes the ciphertext regardless and this provides the audit trail.
    """
    password_enc = (getattr(account, "password_enc", "") or "")
    broker_password = (getattr(account, "broker_password", "") or "")
    cleared_fields = []
    if password_enc:
        cleared_fields.append("password_enc")
    if broker_password:
        cleared_fields.append("broker_password")
    had_credential = bool(cleared_fields)

    account.password_enc = ""
    account.broker_password = ""
    if getattr(account, "pk", None) is not None:
        with transaction.atomic():
            account.save(update_fields=["password_enc", "broker_password"])

    evidence = {
        "method": "secure-clear",
        "had_credential": had_credential,
        "cleared_fields": cleared_fields,
    }
    # Redacted destruction evidence — the audit record survives even after the account row is deleted.
    log_customer_credential_event(
        "DESTROYED", account=account, actor=actor, request=request, **evidence)
    return evidence
