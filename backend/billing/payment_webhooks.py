"""
Payment webhook ingress — security-sensitive.

Implements the approved processing sequence exactly:
    1. Receive payload
    2. Verify provider signature
    3. Persist PaymentEvent ingestion record
    4. Apply idempotency / replay check
    5. Validate ordering / staleness against current subscription state
    6. Perform service-layer subscription transition
    7. Emit AuditEvent

**No billing or subscription mutation may occur before step 6.**

All webhook payloads are treated as hostile input.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from billing.models import PaymentEvent, UserSubscriptionState, _sanitize_payload
from billing.subscription_service import (
    apply_subscription_transition,
    normalize_webhook_status,
)
from core.audit import log_event

logger = logging.getLogger(__name__)

User = get_user_model()


# ---------------------------------------------------------------------------
# Signature verification (narrowest safe abstraction)
# ---------------------------------------------------------------------------

def _verify_provider_signature(
    request: HttpRequest,
    raw_body: bytes,
    provider_name: str,
) -> bool:
    """
    Verify the webhook payload signature from the payment provider.

    Uses HMAC-SHA256 with a provider-specific signing secret stored in
    Django settings as ``PAYMENT_WEBHOOK_SECRET_<PROVIDER_NAME>``.

    The provider's signature is expected in the ``X-Webhook-Signature``
    header (or provider-specific header).

    Returns ``True`` if verified, ``False`` otherwise.
    """
    # Resolve signing secret from settings.
    secret_setting = f"PAYMENT_WEBHOOK_SECRET_{provider_name.upper()}"
    signing_secret = getattr(django_settings, secret_setting, "")
    if not signing_secret:
        logger.warning(
            "Webhook signature verification failed: no signing secret "
            "configured for provider=%s (expected setting %s)",
            provider_name,
            secret_setting,
        )
        return False

    # Read provider signature from header.
    provided_sig = (
        request.headers.get("X-Webhook-Signature", "")
        or request.headers.get("Stripe-Signature", "")
    )
    if not provided_sig:
        logger.warning(
            "Webhook signature verification failed: no signature header "
            "present for provider=%s",
            provider_name,
        )
        return False

    # Compute expected HMAC-SHA256 and compare in constant time.
    expected = hmac.new(
        signing_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, provided_sig)


# ---------------------------------------------------------------------------
# Webhook ingress view
# ---------------------------------------------------------------------------

@method_decorator(csrf_exempt, name="dispatch")
class PaymentWebhookView(View):
    """
    POST /api/billing/webhooks/<provider_name>/

    Single narrow webhook ingress endpoint.  Enforces the 7-step
    processing sequence.  No subscription mutation before step 6.
    """

    http_method_names = ["post"]

    def post(self, request: HttpRequest, provider_name: str) -> JsonResponse:
        raw_body = request.body

        # ---------------------------------------------------------------
        # Step 1: Receive payload
        # ---------------------------------------------------------------
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            _emit_audit(
                "WEBHOOK_SIGNATURE_FAILED",
                provider_name=provider_name,
                reason_code="INVALID_JSON",
            )
            return JsonResponse(
                {"error": "invalid_json"}, status=400
            )

        if not isinstance(payload, dict):
            _emit_audit(
                "WEBHOOK_SIGNATURE_FAILED",
                provider_name=provider_name,
                reason_code="PAYLOAD_NOT_OBJECT",
            )
            return JsonResponse(
                {"error": "invalid_payload"}, status=400
            )

        # ---------------------------------------------------------------
        # Step 2: Verify provider signature
        # ---------------------------------------------------------------
        sig_ok = _verify_provider_signature(request, raw_body, provider_name)
        if not sig_ok:
            _emit_audit(
                "WEBHOOK_SIGNATURE_FAILED",
                provider_name=provider_name,
                provider_event_id=payload.get("id", ""),
                reason_code="SIGNATURE_MISMATCH",
            )
            return JsonResponse(
                {"error": "signature_verification_failed"}, status=403
            )

        sig_verified_at = timezone.now()

        # ---------------------------------------------------------------
        # Extract required fields from payload
        # ---------------------------------------------------------------
        provider_event_id = str(payload.get("id", ""))
        event_type = str(payload.get("type", ""))
        subscription_ref = str(payload.get("subscription", ""))
        provider_ts_raw = payload.get("created")
        user_ref = payload.get("user_id") or payload.get("customer_email")

        provider_ts = None
        if provider_ts_raw is not None:
            try:
                # Support both epoch seconds and ISO strings.
                if isinstance(provider_ts_raw, (int, float)):
                    from datetime import datetime, timezone as tz
                    provider_ts = datetime.fromtimestamp(
                        provider_ts_raw, tz=tz.utc
                    )
                else:
                    from django.utils.dateparse import parse_datetime
                    provider_ts = parse_datetime(str(provider_ts_raw))
            except (ValueError, TypeError, OverflowError):
                provider_ts = None

        # Build idempotency key.
        idem_key = PaymentEvent.build_idempotency_key(
            provider_name=provider_name,
            provider_event_id=provider_event_id,
            event_type=event_type,
            subscription_reference=subscription_ref,
            provider_timestamp=str(provider_ts or ""),
        )

        # ---------------------------------------------------------------
        # Step 3: Persist PaymentEvent ingestion record
        # ---------------------------------------------------------------
        sanitized = _sanitize_payload(payload)

        try:
            payment_event = PaymentEvent.objects.create(
                provider_name=provider_name,
                provider_event_id=provider_event_id,
                event_type=event_type,
                idempotency_key=idem_key,
                subscription_reference=subscription_ref,
                provider_timestamp=provider_ts,
                signature_verified_at=sig_verified_at,
                raw_payload=sanitized,
                processing_status=PaymentEvent.ProcessingStatus.VERIFIED,
            )
        except IntegrityError:
            # ---------------------------------------------------------------
            # Step 4: Idempotency — duplicate detected at DB level
            # ---------------------------------------------------------------
            # The idempotency_key unique constraint rejected the insert,
            # meaning a prior event with this key already exists.
            #
            # We do NOT mutate the original PaymentEvent row.  Its
            # processing_status (processed / rejected / failed / verified)
            # reflects the historical truth of the first delivery and must
            # be preserved.  Duplicate handling is audit-only + idempotent
            # 200 response.
            _emit_audit(
                "WEBHOOK_DUPLICATE_REJECTED",
                provider_name=provider_name,
                provider_event_id=provider_event_id,
                subscription_reference=subscription_ref,
                processing_status="duplicate",
                reason_code="DUPLICATE_EVENT",
            )
            return JsonResponse({"status": "duplicate"}, status=200)

        # ---------------------------------------------------------------
        # Step 4 (continued): Idempotency — explicit service-level check
        # ---------------------------------------------------------------
        # Already handled by unique constraint above.  If we reach here,
        # the event is new and non-duplicate.

        # ---------------------------------------------------------------
        # Step 5: Validate ordering / staleness
        # ---------------------------------------------------------------
        # Resolve user for subscription lookup.
        user = _resolve_user(user_ref)
        if user is None:
            _mark_status(payment_event, PaymentEvent.ProcessingStatus.REJECTED)
            _emit_audit(
                "WEBHOOK_PROCESSING_FAILED",
                provider_name=provider_name,
                provider_event_id=provider_event_id,
                subscription_reference=subscription_ref,
                processing_status="rejected",
                reason_code="USER_NOT_FOUND",
            )
            return JsonResponse({"status": "rejected", "reason": "user_not_found"}, status=200)

        # Staleness check: if provider_timestamp is older than the last
        # subscription state change, reject as stale.
        if provider_ts is not None:
            stale = _is_stale_event(user, provider_ts)
            if stale:
                _mark_status(payment_event, PaymentEvent.ProcessingStatus.REJECTED)
                _emit_audit(
                    "WEBHOOK_STALE_REJECTED",
                    provider_name=provider_name,
                    provider_event_id=provider_event_id,
                    subscription_reference=subscription_ref,
                    processing_status="rejected",
                    reason_code="STALE_EVENT",
                )
                return JsonResponse({"status": "rejected", "reason": "stale_event"}, status=200)

        # ---------------------------------------------------------------
        # Step 6: Perform service-layer subscription transition
        # ---------------------------------------------------------------
        # Extract the target status from the payload.
        webhook_status = str(payload.get("status", ""))
        canonical_status = normalize_webhook_status(webhook_status)

        if canonical_status is None:
            _mark_status(payment_event, PaymentEvent.ProcessingStatus.REJECTED)
            _emit_audit(
                "WEBHOOK_PROCESSING_FAILED",
                provider_name=provider_name,
                provider_event_id=provider_event_id,
                subscription_reference=subscription_ref,
                processing_status="rejected",
                reason_code="UNKNOWN_STATUS",
            )
            return JsonResponse(
                {"status": "rejected", "reason": "unknown_status"}, status=200
            )

        plan_slug = payload.get("plan") or None

        result = apply_subscription_transition(
            user=user,
            target_status=canonical_status,
            plan=plan_slug,
        )

        # ---------------------------------------------------------------
        # Step 7: Emit AuditEvent
        # ---------------------------------------------------------------
        if result.success:
            _mark_status(payment_event, PaymentEvent.ProcessingStatus.PROCESSED)
            _emit_audit(
                "WEBHOOK_SUBSCRIPTION_TRANSITIONED",
                provider_name=provider_name,
                provider_event_id=provider_event_id,
                subscription_reference=subscription_ref,
                processing_status="processed",
                transition_from=result.previous_status,
                transition_to=result.new_status,
            )
            return JsonResponse({"status": "processed"}, status=200)
        else:
            _mark_status(payment_event, PaymentEvent.ProcessingStatus.FAILED)
            _emit_audit(
                "WEBHOOK_PROCESSING_FAILED",
                provider_name=provider_name,
                provider_event_id=provider_event_id,
                subscription_reference=subscription_ref,
                processing_status="failed",
                reason_code="TRANSITION_FAILED",
                transition_from=result.previous_status,
                transition_to=result.new_status,
            )
            return JsonResponse({"status": "failed"}, status=200)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _resolve_user(user_ref: Any) -> User | None:
    """
    Resolve a user from the webhook payload reference.

    Supports integer user ID or email string.
    """
    if user_ref is None:
        return None
    if isinstance(user_ref, int) or (isinstance(user_ref, str) and user_ref.isdigit()):
        return User.objects.filter(id=int(user_ref)).first()
    if isinstance(user_ref, str) and "@" in user_ref:
        return User.objects.filter(email=user_ref).first()
    return None


def _is_stale_event(user: User, provider_ts) -> bool:
    """
    Determine if a webhook event is stale by comparing its
    ``provider_timestamp`` against the subscription's
    ``last_plan_change_at`` (the domain-state ordering authority).

    If no subscription state exists, the event is not stale.
    """
    try:
        state = UserSubscriptionState.objects.get(user=user)
    except UserSubscriptionState.DoesNotExist:
        return False

    # Ordering authority: last_plan_change_at is set by every
    # subscription_service transition.  If it is None, the subscription
    # was never mutated by a webhook — so no staleness applies.
    authority = state.last_plan_change_at
    if authority is None:
        return False

    return provider_ts < authority


def _mark_status(event: PaymentEvent, status: str) -> None:
    """Update the processing status and processed_at on a PaymentEvent."""
    PaymentEvent.objects.filter(pk=event.pk).update(
        processing_status=status,
        processed_at=timezone.now(),
    )


def _emit_audit(
    event_type: str,
    *,
    provider_name: str = "",
    provider_event_id: str = "",
    subscription_reference: str = "",
    processing_status: str = "",
    reason_code: str = "",
    transition_from: str = "",
    transition_to: str = "",
) -> None:
    """Emit an AuditEvent for webhook processing outcomes."""
    severity = "INFO"
    if event_type in (
        "WEBHOOK_SIGNATURE_FAILED",
        "WEBHOOK_PROCESSING_FAILED",
    ):
        severity = "WARN"
    elif event_type == "WEBHOOK_DUPLICATE_REJECTED":
        severity = "INFO"
    elif event_type == "WEBHOOK_STALE_REJECTED":
        severity = "WARN"

    metadata: dict[str, Any] = {
        "provider_name": provider_name,
    }
    if provider_event_id:
        metadata["provider_event_id"] = provider_event_id
    if subscription_reference:
        metadata["subscription_reference"] = subscription_reference
    if processing_status:
        metadata["processing_status"] = processing_status
    if reason_code:
        metadata["reason_code"] = reason_code
    if transition_from:
        metadata["transition_from"] = transition_from
    if transition_to:
        metadata["transition_to"] = transition_to

    log_event(
        request=None,
        event_type=event_type,
        severity=severity,
        entity_type="payment_event",
        metadata=metadata,
    )
