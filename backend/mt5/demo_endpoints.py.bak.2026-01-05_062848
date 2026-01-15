import secrets
import string
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Mt5Credential, Mt5Instance
from .services.windows_agent import provision_windows_user
from .services.guac_db import ensure_rdp_connection
from .services.guac_api import launch_url, guac_token


def _rand_password(length=20):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _win_username_for_user(user_id) -> str:
    short = str(user_id).replace("-", "")[:10]
    return f"guvfx_u_{short}"


def _get_or_create_windows_instance():
    inst, _ = Mt5Instance.objects.get_or_create(
        hostname="WIN-FH-01",
        defaults={"platform": "WINDOWS", "rdp_host": "10.50.0.2"},
    )
    if inst.platform != "WINDOWS":
        inst.platform = "WINDOWS"
        inst.rdp_host = "10.50.0.2"
        inst.save(update_fields=["platform", "rdp_host", "updated_at"])
    return inst


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def account_add_verify(request):
    login = str(request.data.get("login", "")).strip()
    server = str(request.data.get("server", "")).strip()
    password = str(request.data.get("password", "")).strip()

    if not login or not server or not password:
        return Response({"ok": False, "error": "login, server, password required"}, status=400)

    cred, _ = Mt5Credential.objects.get_or_create(
        user=request.user,
        defaults={"login": login, "server": server, "password_enc": password},  # demo only
    )
    cred.login = login
    cred.server = server
    cred.password_enc = password  # demo only
    cred.last_status = "PENDING"
    cred.last_error = ""
    cred.last_verified_at = timezone.now()
    cred.save(update_fields=["login","server","password_enc","last_status","last_error","last_verified_at","updated_at"])

    inst = _get_or_create_windows_instance()
    win_user = _win_username_for_user(request.user.id)
    win_pass = _rand_password()

    try:
        provision_windows_user(win_user, win_pass)

        if not inst.guac_connection_id or inst.windows_username != win_user:
            conn_name = f"U:{request.user.id} - WIN-FH-01"
            cid = ensure_rdp_connection(conn_name, win_user, win_pass)

            inst.is_leased = True
            inst.leased_to = request.user
            inst.lease_expires_at = None
            inst.windows_username = win_user
            inst.windows_password_enc = win_pass  # demo only
            inst.guac_connection_id = cid
            inst.save(update_fields=[
                "is_leased","leased_to","lease_expires_at",
                "windows_username","windows_password_enc","guac_connection_id","updated_at"
            ])

        cred.last_status = "SUCCESS"
        cred.last_error = ""
        cred.last_verified_at = timezone.now()
        cred.save(update_fields=["last_status","last_error","last_verified_at","updated_at"])

        return Response({"ok": True, "status": "SUCCESS", "message": "Verified (demo)", "active": True})

    except Exception as e:
        cred.last_status = "FAILED"
        cred.last_error = str(e)[:1000]
        cred.last_verified_at = timezone.now()
        cred.save(update_fields=["last_status","last_error","last_verified_at","updated_at"])
        return Response({"ok": False, "status": "FAILED", "message": cred.last_error}, status=400)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def launch(request):
    inst = _get_or_create_windows_instance()
    if not inst.guac_connection_id or inst.leased_to_id != request.user.id:
        return Response({"ok": False, "error": "No active Windows session assigned"}, status=400)

    t = guac_token()
    url = launch_url(t, inst.guac_connection_id)
    return Response({"ok": True, "launch_url": url, "expires_in_seconds": 900})
