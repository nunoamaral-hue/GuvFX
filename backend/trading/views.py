from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import TradingAccount, Trade
from .serializers import TradingAccountSerializer, TradeSerializer


class TradingAccountViewSet(viewsets.ModelViewSet):
    """
    CRUD for trading accounts.

    - Non-staff users see and manage only their own accounts.
    - Staff users can see all accounts.
    """

    serializer_class = TradingAccountSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = TradingAccount.objects.select_related("user", "broker_server").all()
        if not user.is_staff:
            qs = qs.filter(user=user)
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        """Ensure non-staff users can only create accounts for themselves."""
        # Non-staff users: always force ownership.
        if not self.request.user.is_staff:
            serializer.save(user=self.request.user)
            return

        # Staff users: allow creating for a specified user if the serializer exposes it,
        # otherwise default to the staff user.
        if "user" in getattr(serializer, "validated_data", {}):
            serializer.save()
        else:
            serializer.save(user=self.request.user)


class TradeViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only access to trades.

    - Non-staff users see trades only from their own accounts.
    - Staff can see all trades.
    - Optional filters: ?account=<id>&symbol=EURUSD
    """

    serializer_class = TradeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Trade.objects.select_related("account", "account__user", "account__broker_server").all()

        if not user.is_staff:
            qs = qs.filter(account__user=user)

        account_id = self.request.query_params.get("account")
        symbol = self.request.query_params.get("symbol")

        if account_id:
            qs = qs.filter(account_id=account_id)

        if symbol:
            qs = qs.filter(symbol__iexact=symbol)

        return qs