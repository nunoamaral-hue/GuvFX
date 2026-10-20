"""broker_intelligence URL routing (read-only member API + DARK mailbox-connect flow)."""
from django.urls import path

from .views import (AccountBrokerContactCreateView, AccountBrokerContactListView, AccountBrokerContactVerifyView,
                    IdentityListView, IdentityVerifyView, MailboxCallbackView, MailboxConnectView,
                    MailboxRevokeView, MailboxStatusView, WithdrawalHistoryView, WithdrawalMetricsView)

urlpatterns = [
    path("withdrawals/metrics/", WithdrawalMetricsView.as_view(), name="broker-intelligence-withdrawal-metrics"),
    path("withdrawals/history/", WithdrawalHistoryView.as_view(), name="broker-intelligence-withdrawal-history"),
    path("identities/", IdentityListView.as_view(), name="broker-intelligence-identities"),
    path("identities/verify/", IdentityVerifyView.as_view(), name="broker-intelligence-identity-verify"),
    path("accounts/<int:account_id>/contacts/", AccountBrokerContactListView.as_view(),
         name="broker-intelligence-account-contacts"),
    path("accounts/<int:account_id>/contacts/create/", AccountBrokerContactCreateView.as_view(),
         name="broker-intelligence-account-contact-create"),
    path("accounts/<int:account_id>/contacts/<int:contact_id>/verify/", AccountBrokerContactVerifyView.as_view(),
         name="broker-intelligence-account-contact-verify"),
    path("mailboxes/status/", MailboxStatusView.as_view(), name="broker-intelligence-mailbox-status"),
    path("mailboxes/connect/", MailboxConnectView.as_view(), name="broker-intelligence-mailbox-connect"),
    path("mailboxes/callback/", MailboxCallbackView.as_view(), name="broker-intelligence-mailbox-callback"),
    path("mailboxes/<int:mailbox_id>/revoke/", MailboxRevokeView.as_view(), name="broker-intelligence-mailbox-revoke"),
]
