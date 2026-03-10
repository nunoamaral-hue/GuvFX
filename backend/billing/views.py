from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import UserSubscriptionState, Invoice
from .serializers import UserSubscriptionStateSerializer, InvoiceSerializer


class MySubscriptionView(APIView):
    """
    GET /api/billing/subscription/

    Returns the authenticated user's subscription state.
    If no UserSubscriptionState row exists, returns null (not 404/500).
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            state = UserSubscriptionState.objects.get(user=request.user)
        except UserSubscriptionState.DoesNotExist:
            return Response({"subscription": None})

        serializer = UserSubscriptionStateSerializer(state)
        return Response({"subscription": serializer.data})


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
