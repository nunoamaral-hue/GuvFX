from django.urls import path

from .views import MySubscriptionView, MyInvoicesView

urlpatterns = [
    path("subscription/", MySubscriptionView.as_view(), name="billing-subscription"),
    path("invoices/", MyInvoicesView.as_view(), name="billing-invoices"),
]
