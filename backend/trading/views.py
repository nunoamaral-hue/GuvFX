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


# =============================================================================
# On-Demand Trade Sync
# =============================================================================
from datetime import datetime
from decimal import Decimal
from django.utils.dateparse import parse_datetime
from rest_framework.views import APIView
from core.audit import log_trades_ingested


def _to_decimal(x, default="0"):
    """Safely convert to Decimal."""
    if x is None:
        return Decimal(default)
    return Decimal(str(x))


def _to_datetime(x):
    """
    Safely convert to datetime.
    Handles:
    - ISO strings (via parse_datetime)
    - Unix timestamps (integer seconds since epoch)
    """
    if not x:
        return None
    # If it's an integer (Unix timestamp), convert to aware UTC datetime
    if isinstance(x, (int, float)):
        try:
            # Use datetime.timezone.utc (Python stdlib), not Django's timezone module
            import datetime as dt_module
            return dt_module.datetime.utcfromtimestamp(x).replace(tzinfo=dt_module.timezone.utc)
        except (ValueError, OSError):
            return None
    # Otherwise try parsing as ISO string
    return parse_datetime(str(x))


def _normalize_side(d: dict) -> str:
    """
    Normalize trade side from deal data.
    - If "side" field exists, use it (BUY/SELL)
    - Otherwise map MT5 "type" field: 0=BUY, 1=SELL
    - Default to BUY if unknown
    """
    side = d.get("side")
    if side:
        s = str(side).strip().upper()
        if s in ("BUY", "SELL"):
            return s

    # MT5 deal type codes: 0=DEAL_TYPE_BUY, 1=DEAL_TYPE_SELL
    mt5_type = d.get("type")
    if mt5_type is not None:
        try:
            t = int(mt5_type)
            if t == 0:
                return "BUY"
            elif t == 1:
                return "SELL"
        except (ValueError, TypeError):
            pass

    return "BUY"  # Default


def _find_demo_comment_for_close(account: TradingAccount, symbol: str, magic: int) -> str:
    """
    For SELL deals with empty comment, try to find a matching BUY deal's comment.
    This handles the case where MT5 close deals don't carry the original comment.

    Looks for recent BUY trades (last 10 minutes) on same account+symbol with
    magic_number=1 (demo) and comment starting with GUVFX_DEMO_JOB:.

    Returns the comment string or empty string if not found.
    """
    from datetime import timedelta

    # Only apply for demo magic number
    if magic != 1:
        return ""

    cutoff = timezone.now() - timedelta(minutes=10)
    recent_buy = (
        Trade.objects.filter(
            account=account,
            symbol=symbol,
            side="BUY",
            magic_number=1,
            open_time__gte=cutoff,
            comment__startswith="GUVFX_DEMO_JOB:",
        )
        .order_by("-open_time")
        .first()
    )

    if recent_buy and recent_buy.comment:
        return recent_buy.comment

    return ""


def _upsert_trades(account: TradingAccount, deals: list) -> tuple[int, int, int, list]:
    """
    Upsert deals into Trade model.
    Returns (inserted_count, updated_count, skipped_count, skip_reasons).

    Handles MT5 deal snapshots from Windows agent:
    - ticket: coerced to string
    - time: Unix seconds -> used for BOTH open_time and close_time
    - price: used for BOTH open_price and close_price
    - type: 0=BUY, 1=SELL

    Guarantees:
    - ticket is always a string
    - open_time is NEVER None
    - close_time is NEVER None
    - open_price is NEVER None (uses price field, defaults to 0)
    - close_price is NEVER None (uses price field, defaults to 0)

    Demo attribution:
    - For SELL deals with empty comment and magic=1, copies comment from
      recent matching BUY deal for attribution continuity.
    """
    inserted = 0
    updated = 0
    skipped = 0
    skip_reasons = []

    for d in deals:
        try:
            # Extract ticket (required) - always coerce to string
            ticket_raw = d.get("ticket") or d.get("position_ticket") or d.get("deal_id") or ""
            ticket = str(ticket_raw).strip()
            if not ticket:
                skipped += 1
                if len(skip_reasons) < 3:
                    skip_reasons.append("missing_ticket")
                continue

            symbol = (d.get("symbol") or "").strip()
            side = _normalize_side(d)
            vol = _to_decimal(d.get("volume") or d.get("lots") or "0")

            # Timestamp handling: use "time" field (Unix seconds) as primary source
            unix_time = _to_datetime(d.get("time"))

            # open_time and close_time: both set to unix_time, fallback to now()
            open_time = unix_time or timezone.now()
            close_time = unix_time or timezone.now()

            # Price handling: use "price" field for BOTH open_price and close_price
            # This ensures we never have 0 prices when actual price data exists
            price_raw = d.get("price")
            if price_raw is not None:
                price = Decimal(str(price_raw))
            else:
                price = Decimal("0")

            open_price = price
            close_price = price

            profit = _to_decimal(d.get("profit") or d.get("pnl") or "0")
            commission = _to_decimal(d.get("commission") or "0")
            swap = _to_decimal(d.get("swap") or "0")

            magic = d.get("magic") if d.get("magic") is not None else d.get("magic_number")
            try:
                magic = int(magic) if magic is not None else None
            except Exception:
                magic = None

            comment = str(d.get("comment") or "").strip()

            # Demo attribution: For SELL deals with empty comment, try to copy
            # from recent matching BUY deal so close legs show correct strategy
            if not comment and side == "SELL" and magic == 1 and symbol:
                comment = _find_demo_comment_for_close(account, symbol, magic)

            obj, created = Trade.objects.get_or_create(
                account=account,
                ticket=ticket,
                defaults={
                    "symbol": symbol,
                    "side": side,
                    "volume": vol,
                    "open_time": open_time,
                    "close_time": close_time,
                    "open_price": open_price,
                    "close_price": close_price,
                    "profit": profit,
                    "commission": commission,
                    "swap": swap,
                    "magic_number": magic,
                    "comment": comment,
                    "opened_by": "EA",
                },
            )
            if created:
                inserted += 1
                continue

            # Update existing trade if values changed
            changed = False

            # Fix open_price if it was stored as 0 but we now have a price
            if obj.open_price == Decimal("0") and open_price != Decimal("0"):
                obj.open_price = open_price
                changed = True

            if obj.close_price != close_price:
                obj.close_price = close_price
                changed = True
            if obj.close_time != close_time:
                obj.close_time = close_time
                changed = True
            if obj.profit != profit:
                obj.profit = profit
                changed = True
            if obj.commission != commission:
                obj.commission = commission
                changed = True
            if obj.swap != swap:
                obj.swap = swap
                changed = True

            # Also update comment if it was empty and we now have one (demo attribution fix)
            if not obj.comment and comment:
                obj.comment = comment
                changed = True

            if changed:
                obj.save()
                updated += 1

        except Exception as e:
            # Skip malformed deal rows to avoid 500 errors
            skipped += 1
            if len(skip_reasons) < 3:
                skip_reasons.append(f"exception:{str(e)[:50]}")
            continue

    return inserted, updated, skipped, skip_reasons


class SyncNowView(APIView):
    """
    POST /api/trading/sync-now/

    On-demand trade ingestion from Windows agent.
    Requires cookie auth + CSRF.

    Body:
    {
        "account_id": <int>,
        "windows_username": "<string>"
    }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        account_id = request.data.get("account_id")
        windows_username = request.data.get("windows_username", "").strip()

        # Validate inputs
        if not account_id:
            return Response(
                {"ok": False, "error": "missing_account_id", "message": "account_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not windows_username:
            return Response(
                {"ok": False, "error": "missing_windows_username", "message": "windows_username is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate account ownership
        try:
            account = TradingAccount.objects.get(id=account_id, user=user)
        except TradingAccount.DoesNotExist:
            return Response(
                {"ok": False, "error": "account_not_found", "message": "Account not found or not owned by you."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Get Windows agent config
        agent_base = (os.getenv("WINDOWS_AGENT_BASE") or os.getenv("GUVFX_AGENT_URL") or "").rstrip("/")
        agent_token = (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or "").strip()

        if not agent_base or not agent_token:
            return Response(
                {"ok": False, "error": "agent_not_configured", "message": "Windows agent is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Call Windows agent to get deals
        import urllib.parse
        url = f"{agent_base}/mt5/snapshots/deals?username={urllib.parse.quote(windows_username)}"

        try:
            req = urllib.request.Request(
                url,
                method="GET",
                headers={"X-GuvFX-Agent-Token": agent_token},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
                data = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            return Response(
                {"ok": False, "error": "agent_http_error", "message": f"Agent returned HTTP {e.code}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except urllib.error.URLError as e:
            return Response(
                {"ok": False, "error": "agent_unreachable", "message": f"Cannot reach agent: {e.reason}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except json.JSONDecodeError:
            return Response(
                {"ok": False, "error": "agent_invalid_json", "message": "Agent returned invalid JSON."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception as e:
            return Response(
                {"ok": False, "error": "agent_error", "message": str(e)[:200]},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Parse deals robustly (multiple possible response shapes)
        deals = (
            data.get("deals")
            or (data.get("data") or {}).get("deals")
            or []
        )
        if data.get("ok") is True and "data" in data and "deals" in data["data"]:
            deals = data["data"]["deals"]

        # Upsert trades
        inserted, updated, skipped, skip_reasons = _upsert_trades(account, deals)

        # Audit log
        log_trades_ingested(
            request=request,
            account_id=account.id,
            inserted=inserted,
            updated=updated,
            deals_count=len(deals),
        )

        response_data = {
            "ok": True,
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "deals_count": len(deals),
        }
        if skip_reasons:
            response_data["skip_reasons"] = skip_reasons

        return Response(response_data, status=status.HTTP_200_OK)

