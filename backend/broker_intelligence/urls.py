"""broker_intelligence URL routing (read-only member API + DARK mailbox-connect flow)."""
from django.urls import path

from .views import (IdentityListView, IdentityVerifyView, MailboxCallbackView, MailboxConnectView,
                    MailboxRevokeView, WithdrawalHistoryView, WithdrawalMetricsView)

urlpatterns = [
    path("withdrawals/metrics/", WithdrawalMetricsView.as_view(), name="broker-intelligence-withdrawal-metrics"),
    path("withdrawals/history/", WithdrawalHistoryView.as_view(), name="broker-intelligence-withdrawal-history"),
    path("identities/", IdentityListView.as_view(), name="broker-intelligence-identities"),
    path("identities/verify/", IdentityVerifyView.as_view(), name="broker-intelligence-identity-verify"),
    path("mailboxes/connect/", MailboxConnectView.as_view(), name="broker-intelligence-mailbox-connect"),
    path("mailboxes/callback/", MailboxCallbackView.as_view(), name="broker-intelligence-mailbox-callback"),
    path("mailboxes/<int:mailbox_id>/revoke/", MailboxRevokeView.as_view(), name="broker-intelligence-mailbox-revoke"),
]
