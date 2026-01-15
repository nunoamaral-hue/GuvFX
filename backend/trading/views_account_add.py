from __future__ import annotations

from django.db import IntegrityError, transaction
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from trading.models import BrokerServer, TradingAccount
from trading.serializers import TradingAccountSerializer
from trading.views import _get_user_mt5_instance, _windows_agent_post_json


class AddAccountWithMt5LoginView(APIView):
    """
    Atomic flow:
    1) Ask Windows agent to MT5 login-and-validate (EA-based)
    2) If valid -> create TradingAccount bound to user's mt5_instance
       - Enforce 1 active per MT5 instance on create
    3) If duplicate account exists -> return it (created=false) instead of 409/500
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        data = request.data or {}

        name = str(data.get("name") or "").strip()
        account_number = str(data.get("account_number") or "").strip()
        password = str(data.get("password") or "").strip()
        is_demo = bool(data.get("is_demo", True))

        broker_server_id = data.get("broker_server")
        broker_name = str(data.get("broker_name") or "").strip()

        if not name or not account_number or not password:
            return Response(
                {"ok": False, "detail": "name/account_number/password required"},
                status=400,
            )

        # Resolve server name
        server_name = ""
        broker_server = None
        if broker_server_id:
            broker_server = BrokerServer.objects.filter(id=broker_server_id, is_active=True).first()
            if not broker_server:
                return Response({"ok": False, "detail": "Invalid broker_server"}, status=400)
            server_name = broker_server.server_name
        else:
            if not broker_name:
                return Response({"ok": False, "detail": "Provide broker_server or broker_name"}, status=400)
            server_name = broker_name

        inst = _get_user_mt5_instance(user)
        if not inst or not getattr(inst, "windows_username", ""):
            return Response({"ok": False, "detail": "No MT5 instance/windows user assigned"}, status=409)

        # 1) Ask Windows Agent to login+validate (EA-based)
        agent_payload = {
            "username": inst.windows_username,
            "login": account_number,
            "password": password,
            "server": server_name,
        }
        agent = _windows_agent_post_json("/mt5/login-and-validate", agent_payload)

        if not bool(agent.get("ok", False)):
            return Response({"ok": False, "detail": "Windows agent error", "agent": agent}, status=502)

        if not bool(agent.get("valid", False)):
            return Response(
                {
                    "ok": True,
                    "valid": False,
                    "created": False,
                    "reason": str(agent.get("reason") or "invalid"),
                    "agent": agent,
                },
                status=200,
            )

        # Normalized values used for uniqueness
        norm_broker_name = broker_name or (broker_server.server_name if broker_server else "")

        # Build queryset for "existing account" lookup (used in duplicate path)
        qs = TradingAccount.objects.filter(user=user, account_number=account_number)
        if broker_server:
            qs = qs.filter(broker_server=broker_server)
        else:
            qs = qs.filter(broker_server__isnull=True, broker_name=norm_broker_name)

        with transaction.atomic():
            # Enforce "one active per MT5 instance" for this user+instance
            TradingAccount.objects.filter(user=user, mt5_instance=inst).update(is_active=False)

            # IMPORTANT: nested atomic creates a SAVEPOINT.
            # If create throws IntegrityError, only savepoint is rolled back, outer transaction stays usable.
            try:
                with transaction.atomic():
                    ta = TradingAccount.objects.create(
                        user=user,
                        name=name,
                        mt5_instance=inst,
                        broker_server=broker_server,
                        broker_name=norm_broker_name,
                        account_number=account_number,
                        is_demo=is_demo,
                        is_active=True,
                    )

                    # Apply password encryption through serializer logic
                    ser_in = TradingAccountSerializer(
                        ta,
                        data={"password": password},
                        partial=True,
                        context={"request": request},
                    )
                    ser_in.is_valid(raise_exception=True)
                    ser_in.save()

                    ser_out = TradingAccountSerializer(ta, context={"request": request})
                    return Response(
                        {"ok": True, "valid": True, "created": True, "reason": "ok", "account": ser_out.data, "agent": agent},
                        status=201,
                    )

            except IntegrityError:
                # Already exists: lock it, bind to instance, make active, return 200
                existing = qs.select_for_update().order_by("id").first()
                if not existing:
                    return Response(
                        {"ok": False, "valid": True, "created": False, "reason": "db_integrity_error", "detail": "duplicate but cannot find existing row"},
                        status=409,
                    )

                if existing.mt5_instance_id != inst.id:
                    existing.mt5_instance = inst
                existing.is_active = True
                existing.save(update_fields=["mt5_instance", "is_active", "updated_at"])

                ser_out = TradingAccountSerializer(existing, context={"request": request})
                return Response(
                    {"ok": True, "valid": True, "created": False, "reason": "already_linked", "account": ser_out.data, "agent": agent},
                    status=200,
                )
