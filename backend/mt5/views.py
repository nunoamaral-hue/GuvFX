import json
from pathlib import Path
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from .models import Mt5Credential
from .crypto import encrypt_password

HANDOFF = Path("/app/.guvfx_handoff")

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
        udir = HANDOFF / "free" / str(request.user.id)
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
        # Consume validate_result.json if present (EPHEMERAL)
        udir = HANDOFF / "free" / str(request.user.id)
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
