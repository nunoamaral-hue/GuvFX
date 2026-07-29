"""Customer-facing onboarding emails.

Genuine email delivery (Customer Zero certification). The verification token is a
copy-paste "code" the customer enters on the Email Verification onboarding step
(the frontend posts it to ``/api/onboarding/email/verify/``). We deliver the code
plus a link back to the onboarding page.

All transport/identity config lives in settings (env-driven, Google Workspace SMTP).
This module never handles the SMTP password — it only asks Django's mail layer to send.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


def _reply_to() -> list[str]:
    addr = getattr(settings, "EMAIL_REPLY_TO", "") or settings.DEFAULT_FROM_EMAIL
    return [addr]


def send_verification_email(user, plaintext_token: str) -> None:
    """Send the email-verification code to ``user``.

    Raises on transport failure (the caller turns that into an honest error to the
    customer — we NEVER claim "sent" when it was not). The plaintext token is only
    ever placed in the email body, never logged.
    """
    onboarding_url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/onboarding"
    subject = "Verify your GuvFX email address"
    body = (
        "Welcome to GuvFX.\n\n"
        "To confirm your email address, enter this verification code on the "
        "Email Verification step:\n\n"
        f"    {plaintext_token}\n\n"
        f"Return to onboarding here: {onboarding_url}\n\n"
        "This code expires in 24 hours. If you did not create a GuvFX account, "
        "you can safely ignore this email.\n"
    )
    message = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
        reply_to=_reply_to(),
    )
    # fail_silently=False so a transport/auth failure propagates — the view returns a
    # truthful error instead of a false "email sent" (the old stub's core defect).
    message.send(fail_silently=False)
    logger.info("verification email dispatched to user_id=%s", user.id)
