from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from trading.serializers import TradingAccountSerializer


class AddAccountWithMt5LoginView(APIView):
    """ADR-0021 — thin wrapper over the ONE canonical creation contract (``trading.account_service``).

    Historically this endpoint logged into MT5 via a SHARED instance and only created the account if the
    login was valid (stamping VALIDATED / CONNECTION_FAILED), which required a leased Windows instance and
    therefore ``409``'d every dedicated-runtime customer. Under the canonical contract, adding a broker
    account records CUSTOMER INTENT ONLY: the account is created (``mt5_instance=None``) and its OWN
    runtime is provisioned asynchronously — broker-login validation is DEFERRED to the provisioning stage
    (behind ``PROVISIONING_REQUIRE_BROKER_LOGIN``).

    The Accounts-page UI is unchanged: same URL, same request body, same ``{ok, valid, created, account}``
    response shape. ``valid`` now means "customer intent recorded", not "broker login verified" — the
    truthful broker-connection outcome is surfaced by the account-status lifecycle
    (Account received → Provisioning runtime → Connecting to broker → Validated / Connection failed).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}

        # Preserve the endpoint's input contract (the Accounts form always sends these three).
        name = str(data.get("name") or "").strip()
        account_number = str(data.get("account_number") or "").strip()
        password = str(data.get("password") or "").strip()
        if not name or not account_number or not password:
            return Response(
                {"ok": False, "detail": "name/account_number/password required"}, status=400)

        payload = {
            "name": name,
            "account_number": account_number,
            "password": password,
            "is_demo": bool(data.get("is_demo", True)),
        }
        # Broker identity: a BrokerServer id takes precedence; otherwise a free-text server name.
        broker_server_id = data.get("broker_server")
        if broker_server_id:
            payload["broker_server"] = broker_server_id
        else:
            payload["broker_name"] = str(data.get("broker_name") or "").strip()

        # Validate through the serializer (broker-identity presence + demo/live classification + field
        # rules) so this path enforces exactly the same input contract as the ViewSet create.
        serializer = TradingAccountSerializer(data=payload, context={"request": request})
        serializer.is_valid(raise_exception=True)

        from trading.account_service import create_customer_account
        account, created = create_customer_account(request, serializer)

        out = TradingAccountSerializer(account, context={"request": request}).data
        return Response(
            {"ok": True, "valid": True, "created": created, "account": out},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
