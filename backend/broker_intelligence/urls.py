"""broker_intelligence URL routing (read-only member API + DARK mailbox-connect flow)."""
from django.urls import path

from .views import (MailboxCallbackView, MailboxConnectView, MailboxRevokeView, WithdrawalMetricsView)

urlpatterns = [
    path("withdrawals/metrics/", WithdrawalMetricsView.as_view(), name="broker-intelligence-withdrawal-metrics"),
    path("mailboxes/connect/", MailboxConnectView.as_view(), name="broker-intelligence-mailbox-connect"),
    path("mailboxes/callback/", MailboxCallbackView.as_view(), name="broker-intelligence-mailbox-callback"),
    path("mailboxes/<int:mailbox_id>/revoke/", MailboxRevokeView.as_view(), name="broker-intelligence-mailbox-revoke"),
]
