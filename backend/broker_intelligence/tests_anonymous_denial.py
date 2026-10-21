"""Anonymous-denial regression for the member Withdrawal Intelligence endpoints.

These endpoints are protected by the DRF global default (IsAuthenticated) and set it explicitly, but nothing pinned
that an UNauthenticated request is rejected. DRF runs the permission check in initial() BEFORE the handler's DARK
flag-404, so an anonymous request is denied with 401/403 regardless of flag state. We arm the relevant flags here so
the denial is provably from authentication (not the DARK 404), then assert anonymous access is refused.
"""
from django.test import override_settings
from rest_framework.test import APITestCase


@override_settings(
    BROKER_WITHDRAWAL_UX_ENABLED="1",
    BROKER_MAILBOX_STATUS_ENABLED="1",
    BROKER_MAILBOX_CONNECT_ENABLED="1",
)
class AnonymousDenialTests(APITestCase):
    """Every member Withdrawal Intelligence endpoint refuses an unauthenticated request (401/403), flags ON."""

    def test_withdrawals_metrics_denies_anonymous(self):
        r = self.client.get("/api/broker-intelligence/withdrawals/metrics/")
        self.assertIn(r.status_code, (401, 403), r.content)

    def test_withdrawals_history_denies_anonymous(self):
        r = self.client.get("/api/broker-intelligence/withdrawals/history/")
        self.assertIn(r.status_code, (401, 403), r.content)

    def test_mailbox_status_denies_anonymous(self):
        r = self.client.get("/api/broker-intelligence/mailboxes/status/")
        self.assertIn(r.status_code, (401, 403), r.content)

    def test_mailbox_revoke_denies_anonymous(self):
        # POST to a revoke route; anonymous is rejected before any mailbox-existence/ownership check.
        r = self.client.post("/api/broker-intelligence/mailboxes/1/revoke/")
        self.assertIn(r.status_code, (401, 403), r.content)

    def test_anonymous_denial_holds_even_with_flags_off(self):
        # Defence-in-depth: with the UX flag OFF the authenticated path would 404 (DARK), but an anonymous
        # request must still be denied by auth (401/403), never served and never a 404 that leaks nothing useful.
        with override_settings(BROKER_WITHDRAWAL_UX_ENABLED=""):
            r = self.client.get("/api/broker-intelligence/withdrawals/metrics/")
        self.assertIn(r.status_code, (401, 403), r.content)

    def test_endpoints_declare_isauthenticated_explicitly(self):
        # Stronger than the effective-property tests above: each member view must declare IsAuthenticated on the
        # view itself, so protection does not silently depend only on the DRF global default (which a future change
        # could weaken). Catches a regression that deletes the explicit per-view permission line.
        from rest_framework.permissions import IsAuthenticated
        from broker_intelligence import views as v
        for view in (v.WithdrawalMetricsView, v.WithdrawalHistoryView, v.MailboxStatusView, v.MailboxRevokeView):
            self.assertIn(IsAuthenticated, view.permission_classes, view.__name__)
