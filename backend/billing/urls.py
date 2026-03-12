from django.urls import path

from .views import MySubscriptionView, MyInvoicesView
from .payment_webhooks import PaymentWebhookView

urlpatterns = [
    path("subscription/", MySubscriptionView.as_view(), name="billing-subscription"),
    path("invoices/", MyInvoicesView.as_view(), name="billing-invoices"),
    path(
        "webhooks/<str:provider_name>/",
        PaymentWebhookView.as_view(),
        name="billing-webhook",
    ),
]
