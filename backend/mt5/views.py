import os
import json
from pathlib import Path
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework import status
from mt5.models import Mt5Instance
from .models import Mt5Credential
from .crypto import encrypt_password
from trading.models import TradingAccount
from trading.crypto import decrypt_password as trading_decrypt_password
HANDOFF_VALIDATE = Path("/app/.guvfx_handoff_validate")
HANDOFF = Path("/app/.guvfx_handoff")
POOL_ROOT = Path("/srv/guvfx/mt5_pool")
HANDOFF_POOL = Path("/app/.guvfx_pool")

class Mt5ReleaseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        hostname = str(request.data.get("hostname") or "").strip()

        qs = Mt5Instance.objects.filter(is_admin=False, is_leased=True)
        if hostname:
            inst = qs.filter(hostname=hostname).first()
            if not inst:
                return Response({"detail": "Instance not leased or not found"}, status=status.HTTP_404_NOT_FOUND)
            if (inst.leased_to_id != request.user.id) and (not request.user.is_staff):
                return Response({"detail": "Not allowed"}, status=status.HTTP_403_FORBIDDEN)
        else:
            inst = qs.filter(leased_to=request.user).first()
            if not inst:
                return Response({"detail": "No active MT5 lease found"}, status=status.HTTP_404_NOT_FOUND)

        inst_dir = POOL_ROOT / inst.hostname
        inst_dir.mkdir(parents=True, exist_ok=True)
        (inst_dir / "reset_request.json").write_text(
            json.dumps({"ts": timezone.now().isoformat(), "reason": "release", "by": request.user.email}),
            encoding="utf-8",
        )

        inst.is_leased = False
        inst.leased_to = None
        inst.lease_expires_at = None
        inst.save(update_fields=["is_leased", "leased_to", "lease_expires_at", "updated_at"])

        return Response({"status": "RELEASED", "instance": inst.hostname})

class Mt5PoolStatusView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        rows = []
        for inst in Mt5Instance.objects.order_by("hostname"):
            rows.append({
                "hostname": inst.hostname,
                "is_admin": inst.is_admin,
                "is_leased": inst.is_leased,
                "leased_to": getattr(inst.leased_to, "email", None),
                "lease_expires_at": inst.lease_expires_at.isoformat() if inst.lease_expires_at else None,
                "last_seen_at": inst.last_seen_at.isoformat() if inst.last_seen_at else None,
            })
        return Response({"instances": rows})

class ValidateMt5View(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        login = str(request.data.get("login","")).strip()
        password = str(request.data.get("password","")).strip()
        server = str(request.data.get("server","")).strip()

        if not (login and password and server):
            return Response({"detail":"login/password/server required"}, status=status.HTTP_400_BAD_REQUEST)

        cred, _ = Mt5Credential.objects.get_or_create(
            user=request.user,
            defaults={"login":login, "server":server, "password_enc": encrypt_password(password)},
        )
        cred.login = login
        cred.server = server
        cred.password_enc = encrypt_password(password)
        cred.last_status = "PENDING"
        cred.last_error = ""
        cred.save()

        # Write EPHEMERAL validate request into handoff for mt5_worker to process
        udir = HANDOFF_VALIDATE / "free" / str(request.user.id)
        udir.mkdir(parents=True, exist_ok=True)

        # Write creds for worker (EPHEMERAL; worker must delete)
        (udir / "validate_cred.json").write_text(json.dumps({
            "login": login,
            "password": password,
            "server": server,
        }), encoding="utf-8")

        (udir / "validate_request.json").write_text(json.dumps({
            "ts": timezone.now().isoformat(),
        }), encoding="utf-8")

        return Response({"status":"PENDING"}, status=status.HTTP_202_ACCEPTED)


class Mt5StatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cred = Mt5Credential.objects.filter(user=request.user).first()
        if not cred:
            return Response({"credential": None})
        # Prefer validate handoff (headless validator) if mounted; fallback to normal handoff
        udir = (HANDOFF_VALIDATE / "free" / str(request.user.id))
        res_path = udir / "validate_result.json"
        if not res_path.exists():
            udir = (HANDOFF / "free" / str(request.user.id))
            res_path = udir / "validate_result.json"
        if res_path.exists() and cred:
            try:
                data = json.loads(res_path.read_text(encoding="utf-8"))
                ok = bool(data.get("ok"))
                err = str(data.get("error") or "")
                if ok:
                    cred.last_status = "SUCCESS"
                    cred.last_error = ""
                    cred.last_verified_at = timezone.now()
                else:
                    cred.last_status = "FAILED"
                    cred.last_error = err or "Invalid credentials"
                cred.save(update_fields=["last_status","last_error","last_verified_at","updated_at"])
            finally:
                try:
                    res_path.unlink()
                except FileNotFoundError:
                    pass
        return Response({"credential": {
            "login": cred.login,
            "server": cred.server,
            "last_status": cred.last_status,
            "last_verified_at": cred.last_verified_at,
            "last_error": cred.last_error,
            "updated_at": cred.updated_at,
        }})

from .guac_json import build_mt5_desktop_payload, sign_and_encrypt_json, build_guac_data_url
from .pool import lease_instance_for_user


def _server_name_for_account(account: TradingAccount) -> str:
    """Return the MT5 server string from a TradingAccount."""
    if account.broker_server_id:
        return account.broker_server.server_name
    return account.broker_name or ""


class Mt5DesktopLinkView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        base_url = os.getenv("GUAC_BASE_URL", "https://guac.guvfx.com/guacamole").rstrip("/")
        secret_hex = os.getenv("GUAC_JSON_SECRET_KEY_HEX", "").strip()
        if not secret_hex:
            return Response({"detail": "GUAC_JSON_SECRET_KEY_HEX not configured"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        user_label = f"user-{request.user.id}"

        # ── HARD GATE 1: user must have an active TradingAccount ──
        account = (
            TradingAccount.objects
            .select_related("broker_server", "mt5_instance")
            .filter(user=request.user, is_active=True)
            .first()
        )
        if not account:
            return Response(
                {"detail": "No active trading account. Add and activate an account first."},
                status=status.HTTP_409_CONFLICT,
            )

        if not account.mt5_instance_id:
            return Response(
                {"detail": "Trading account is not bound to an MT5 instance."},
                status=status.HTTP_409_CONFLICT,
            )

        # ── Lease instance ──
        inst = lease_instance_for_user(request.user)
        if not inst:
            return Response({"detail": "No MT5 instances available"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        # ── HARD GATE 2: account must be bound to the leased instance ──
        if account.mt5_instance_id != inst.id:
            return Response(
                {"detail": "Active account is bound to a different MT5 instance."},
                status=status.HTTP_409_CONFLICT,
            )

        # ── Resolve account credentials for handoff ──
        server_name = _server_name_for_account(account)
        pw = trading_decrypt_password(account.password_enc)

        # ── Single handoff path: HANDOFF_POOL (pool-aware) ──
        udir = HANDOFF_POOL / inst.hostname
        udir.mkdir(parents=True, exist_ok=True)

        (udir / "launch_account.json").write_text(json.dumps({
            "user_id": request.user.id,
            "login": account.account_number,
            "password": pw,
            "server": server_name,
        }), encoding="utf-8")
        os.chmod(udir / "launch_account.json", 0o600)

        (udir / "launch_request.json").write_text(json.dumps({
            "ts": timezone.now().isoformat(),
            "user_id": request.user.id,
        }), encoding="utf-8")
        os.chmod(udir / "launch_request.json", 0o600)

        # ── Build signed Guacamole desktop link ──
        payload = build_mt5_desktop_payload(username=user_label, host_override=inst.hostname)
        data_b64 = sign_and_encrypt_json(payload, secret_hex=secret_hex)
        url = build_guac_data_url(base_url=base_url, data_b64=data_b64)

        return Response({"url": url})

class Mt5LaunchApplyView(APIView):
    """Queue a launch request for the user's active TradingAccount."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from trading.views import _get_user_mt5_instance

        account = (
            TradingAccount.objects
            .select_related("broker_server", "mt5_instance")
            .filter(user=request.user, is_active=True)
            .first()
        )
        if not account:
            return Response({"detail": "No active trading account"}, status=status.HTTP_409_CONFLICT)

        inst = _get_user_mt5_instance(request.user)
        if not inst:
            return Response({"detail": "No MT5 instance assigned"}, status=status.HTTP_409_CONFLICT)

        server_name = _server_name_for_account(account)
        pw = trading_decrypt_password(account.password_enc)

        udir = HANDOFF_POOL / inst.hostname
        udir.mkdir(parents=True, exist_ok=True)

        (udir / "launch_account.json").write_text(json.dumps({
            "user_id": request.user.id,
            "login": account.account_number,
            "password": pw,
            "server": server_name,
        }), encoding="utf-8")
        os.chmod(udir / "launch_account.json", 0o600)

        (udir / "launch_request.json").write_text(json.dumps({
            "ts": timezone.now().isoformat(),
            "user_id": request.user.id,
        }), encoding="utf-8")
        os.chmod(udir / "launch_request.json", 0o600)

        return Response({"status": "QUEUED"})
