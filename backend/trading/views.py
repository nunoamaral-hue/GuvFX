from __future__ import annotations

import urllib.request
import json
import os

def _windows_agent_post_json(path: str, payload: dict) -> dict:
    base = (os.getenv("WINDOWS_AGENT_BASE") or os.getenv("GUVFX_AGENT_URL") or "").rstrip("/")
    token = (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or "").strip()

    if not base or not token:
        return {"ok": False, "error": "missing_agent_env", "base": base}

    url = f"{base}{path}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GuvFX-Agent-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            return json.loads(raw) if raw else {"ok": False, "error": "empty_response"}
    except Exception as e:
        return {"ok": False, "error": "agent_request_failed", "message": str(e)}


from django.db import transaction
from django.conf import settings
from pathlib import Path
import json
import os
import urllib.request
from typing import Any
from rest_framework.exceptions import ValidationError
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from mt5.models import Mt5Instance
from .models import TradingAccount, Trade
from .serializers import TradingAccountSerializer, TradeSerializer


def _get_user_mt5_instance(user) -> Mt5Instance | None:
    """
    Prefer the instance currently leased to the user.
    Fallback to the Windows instance if present.
    """
    inst = (
        Mt5Instance.objects.filter(is_leased=True, leased_to=user)
        .order_by("hostname")
        .first()
    )
    if inst:
        return inst

    # fallback (your Windows instance naming)
    inst = Mt5Instance.objects.filter(hostname="WIN-FH-01").first()
    return inst


def _windows_agent_validate_ea(username: str, login: str, server: str) -> dict[str, Any]:
    """
    Calls the Windows agent EA validation endpoint.
    Requires env:
      WINDOWS_AGENT_BASE=http://10.50.0.2:8787
      WINDOWS_AGENT_TOKEN=...
    """
    base = (os.getenv("WINDOWS_AGENT_BASE") or "").rstrip("/")
    token = (os.getenv("WINDOWS_AGENT_TOKEN") or "").strip()

    if not base or not token:
        return {
            "ok": False,
            "valid": False,
            "reason": "windows_agent_not_configured",
            "detail": "Missing WINDOWS_AGENT_BASE or WINDOWS_AGENT_TOKEN",
        }

    url = f"{base}/validate-mt5-ea"
    payload = json.dumps(
        {"username": username, "login": str(login), "server": str(server)}
    ).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GuvFX-Agent-Token": token,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return json.loads(body) if body else {"ok": False, "valid": False, "reason": "empty_response"}
    except Exception as e:
        return {"ok": False, "valid": False, "reason": "request_failed", "detail": str(e)}

def _server_name_for_account(acc: TradingAccount) -> str:
    # Prefer normalized broker_server server_name if set, otherwise broker_name free-text.
    if getattr(acc, "broker_server_id", None) and getattr(acc, "broker_server", None):
        return acc.broker_server.server_name
    return (acc.broker_name or "").strip()

class TradingAccountViewSet(viewsets.ModelViewSet):

    @action(detail=True, methods=["POST"], url_path="test-mt5")
    def test_mt5(self, request, pk=None):
        """
        Check whether MT5 (on the user's instance) is CURRENTLY logged in to this trading account.
        Uses Windows agent: /validate-mt5-ea (EA writes FILE_COMMON status).
        """
        acc = self.get_object()

        if not acc.mt5_instance_id:
            return Response({"ok": False, "detail": "Account has no mt5_instance assigned."}, status=status.HTTP_409_CONFLICT)

        inst = acc.mt5_instance
        if not inst or not getattr(inst, "windows_username", None):
            return Response({"ok": False, "detail": "MT5 instance is missing windows_username."}, status=status.HTTP_409_CONFLICT)

        base = (os.getenv("WINDOWS_AGENT_BASE") or os.getenv("GUVFX_AGENT_URL") or "").rstrip("/")
        token = (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or "").strip()

        if not base or not token:
            return Response({"ok": False, "detail": "Windows agent is not configured."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        payload = {
            "username": inst.windows_username,
            "login": str(acc.account_number),
            "server": _server_name_for_account(acc),
        }

        try:
            req = urllib.request.Request(
                f"{base}/validate-mt5-ea",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-GuvFX-Agent-Token": token,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return Response({"ok": False, "detail": f"Windows agent request failed: {e}"}, status=status.HTTP_502_BAD_GATEWAY)

        # passthrough
        return Response(data, status=status.HTTP_200_OK)

    """
    CRUD for trading accounts.

    Rules:
    - Non-staff users only see/manage their own accounts.
    - On CREATE: we auto-assign mt5_instance (leased_to user if possible; else WIN-FH-01).
    - Active toggle:
        * Activating deactivates other accounts for SAME (user, mt5_instance)
        * Deactivating the last active account for that (user, mt5_instance) is blocked
    """

    serializer_class = TradingAccountSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = TradingAccount.objects.select_related("user", "broker_server", "mt5_instance").all()
        if not user.is_staff:
            qs = qs.filter(user=user)
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        user = self.request.user

        if user.is_staff:
            serializer.save(user=user)
            return

        inst = _get_user_mt5_instance(user)

        # If we can't determine an instance yet, still allow create (inactive).
        if not inst:
            serializer.save(user=user, mt5_instance=None, is_active=False)
            return

        # If user already has an active account on this instance,
        # default new accounts to INACTIVE unless explicitly requested active.
        requested = self.request.data.get("is_active", None)
        if isinstance(requested, str):
            requested_active = requested.strip().lower() in ("1", "true", "yes", "on")
        elif requested is None:
            requested_active = False
        else:
            requested_active = bool(requested)

        has_active = TradingAccount.objects.filter(user=user, mt5_instance=inst, is_active=True).exists()
        make_active = requested_active or (not has_active)

        with transaction.atomic():
            if make_active:
                TradingAccount.objects.filter(user=user, mt5_instance=inst).update(is_active=False)

            serializer.save(
                user=user,
                mt5_instance=inst,
                is_active=make_active,
            )

    from django.db import transaction
    from rest_framework.decorators import action
    from rest_framework.response import Response
    from rest_framework import status

    @action(detail=True, methods=["POST"], url_path="set-active")
    def set_active(self, request, pk=None):
        """
        Toggle active/inactive.

        Rules:
        - One account MUST always be active per mt5_instance.
        - Activating this account deactivates other accounts on SAME mt5_instance for the user.
        - Deactivating is blocked if it would leave ZERO active on that mt5_instance.
        - Optional safety: When activating, MT5 must currently be logged into this account (EA check).
        """
        acc = self.get_object()
        user = acc.user

        raw = request.data.get("is_active", True)
        if isinstance(raw, str):
            is_active = raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            is_active = bool(raw)

        if not acc.mt5_instance_id:
            return Response({"detail": "Account has no mt5_instance assigned."}, status=status.HTTP_409_CONFLICT)

        # Block turning off the last active account for this instance
        if (not is_active) and acc.is_active:
            active_count = TradingAccount.objects.filter(
                user=user,
                mt5_instance_id=acc.mt5_instance_id,
                is_active=True,
            ).count()
            if active_count <= 1:
                return Response(
                    {"detail": "At least one trading account must remain active for this MT5 instance."},
                    status=status.HTTP_409_CONFLICT,
                )

        with transaction.atomic():
            if is_active:
                # SAFETY: only allow activation if MT5 is currently logged into this account
                # (EA validation check)
                try:
                    # Reuse the same logic as test endpoint by calling it internally
                    # (simple: call the action logic inline)
                    inst = acc.mt5_instance
                    base = (os.getenv("WINDOWS_AGENT_BASE") or os.getenv("GUVFX_AGENT_URL") or "").rstrip("/")
                    token = (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or "").strip()

                    if not inst or not inst.windows_username or not base or not token:
                        raise RuntimeError("Windows agent / instance not configured")

                    payload = {
                        "username": inst.windows_username,
                        "login": str(acc.account_number),
                        "server": _server_name_for_account(acc),
                    }

                    req = urllib.request.Request(
                        f"{base}/validate-mt5-ea",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json", "X-GuvFX-Agent-Token": token},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8"))

                    if not data.get("valid"):
                        reason = data.get("reason") or "mt5_not_logged_into_this_account"
                        raise ValidationError(str(reason))

                except ValidationError as e:
                    return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)
                except Exception as e:
                    return Response({"detail": f"MT5 validation error: {e}"}, status=status.HTTP_502_BAD_GATEWAY)

                # Deactivate others on same instance
                TradingAccount.objects.filter(
                    user=user,
                    mt5_instance_id=acc.mt5_instance_id,
                ).exclude(id=acc.id).update(is_active=False)

            acc.is_active = is_active
            acc.save(update_fields=["is_active", "updated_at"])

        return Response({"ok": True, "id": acc.id, "is_active": acc.is_active}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["POST"], url_path="test")
    def test_connection(self, request, pk=None):
        """
        Tests whether the MT5 instance (windows user) is currently logged in
        to THIS account's login/server, by calling the Windows agent EA endpoint.
        """
        acc = self.get_object()

        if not acc.mt5_instance_id:
            return Response(
                {"ok": False, "detail": "Account has no mt5_instance assigned."},
                status=status.HTTP_409_CONFLICT,
            )

        inst = acc.mt5_instance
        win_user = getattr(inst, "windows_username", "") or ""
        if not win_user:
            return Response(
                {"ok": False, "detail": "MT5 instance has no windows_username set."},
                status=status.HTTP_409_CONFLICT,
            )

        data = _windows_agent_validate_ea(
            username=win_user,
            login=acc.account_number,
            server=(acc.broker_server.server_name if acc.broker_server_id else acc.broker_name),
        )

        ok = bool(data.get("ok", False))
        valid = bool(data.get("valid", False))
        reason = str(data.get("reason") or "")

        return Response(
            {
                "ok": ok,
                "valid": valid,
                "reason": reason,
                "agent": data,
            },
            status=status.HTTP_200_OK,
        )

class TradeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TradeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Trade.objects.select_related("account", "account__user", "account__broker_server").all()
        if not user.is_staff:
            qs = qs.filter(account__user=user)
        return qs

