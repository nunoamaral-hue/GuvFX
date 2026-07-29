"""Tests for genuine verification-email delivery (Customer Zero certification).

Covers the fix to the old stub: the send-verification endpoint must ACTUALLY send
mail (correct From/Reply-To/recipient/body) and return a truthful error on transport
failure instead of a false "email sent".
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

User = get_user_model()

EMAIL_SETTINGS = dict(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="GuvFX Support <support@guvfx.com>",
    EMAIL_REPLY_TO="support@guvfx.com",
    FRONTEND_BASE_URL="https://guvfx.com",
)


@override_settings(**EMAIL_SETTINGS)
class VerificationEmailSendTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="cust0@example.com", email="cust0@example.com", password="Str0ng-Pass-123")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("onboarding-email-send")

    def test_send_dispatches_real_email_with_code(self):
        mail.outbox = []
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["cust0@example.com"])
        self.assertEqual(msg.from_email, "GuvFX Support <support@guvfx.com>")
        self.assertEqual(msg.reply_to, ["support@guvfx.com"])
        self.assertIn("verification code", msg.body.lower())
        self.assertIn("guvfx.com/onboarding", msg.body)
        # the plaintext token must be present in the body but NEVER in the API response
        self.assertNotIn("token", resp.json())
        self.assertNotIn(msg.body.strip().split()[0], str(resp.json()))

    def test_token_created_and_deliverable(self):
        from onboarding.models import EmailVerificationToken
        mail.outbox = []
        self.client.post(self.url)
        self.assertTrue(EmailVerificationToken.objects.filter(user=self.user, used=False).exists())

    def test_transport_failure_returns_honest_502_not_false_success(self):
        with patch("onboarding.emails.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 502)
        self.assertNotIn("sent", resp.json()["detail"].lower())

    def test_already_verified_short_circuits_without_email(self):
        from onboarding.services import get_or_create_onboarding_state
        state = get_or_create_onboarding_state(self.user)
        state.email_verified = True
        state.save(update_fields=["email_verified"])
        mail.outbox = []
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
