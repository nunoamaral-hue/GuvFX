from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .entitlements import resolve_entitlements
from .models import UserSubscriptionState, Invoice
from .serializers import (
    UserSubscriptionStateSerializer,
    EntitlementsSerializer,
    InvoiceSerializer,
)


class MySubscriptionView(APIView):
    """
    GET /api/billing/subscription/

    Returns the authenticated user's subscription state and computed
    entitlements.  If no UserSubscriptionState row exists, subscription
    is null but entitlements still resolve to safe viewer defaults.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            state = UserSubscriptionState.objects.get(user=request.user)
        except UserSubscriptionState.DoesNotExist:
            state = None

        sub_data = (
            UserSubscriptionStateSerializer(state).data if state is not None else None
        )
        ent_data = EntitlementsSerializer(resolve_entitlements(state)).data

        return Response({"subscription": sub_data, "entitlements": ent_data})


class MyInvoicesView(APIView):
    """
    GET /api/billing/invoices/

    Returns all invoices for the authenticated user, ordered by most
    recent issue_date first. User can only see their own invoice rows.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        invoices = (
            Invoice.objects.filter(user=request.user)
            .order_by("-issue_date")
        )
        serializer = InvoiceSerializer(invoices, many=True)
        return Response({"invoices": serializer.data})
