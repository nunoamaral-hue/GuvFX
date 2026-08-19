from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")


class Command(BaseCommand):
    help = "Register, inspect, or remove the dedicated customer Telegram webhook without printing secrets."

    def add_arguments(self, parser):
        actions = parser.add_mutually_exclusive_group(required=True)
        actions.add_argument("--register", action="store_true")
        actions.add_argument("--unregister", action="store_true")
        actions.add_argument("--status", action="store_true")

    def _request(self, method: str, payload: dict | None = None) -> dict:
        token = str(getattr(settings, "CUSTOMER_TELEGRAM_BOT_TOKEN", "") or "")
        if not token:
            raise CommandError("dedicated customer Telegram bot token is missing")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=json.dumps(payload or {}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads((response.read() or b"{}").decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, UnicodeError):
            raise CommandError("Telegram webhook request failed") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise CommandError("Telegram rejected the webhook request")
        return result

    def handle(self, *args, **options):
        if options["register"]:
            webhook_url = str(getattr(settings, "CUSTOMER_TELEGRAM_WEBHOOK_URL", "") or "")
            parsed = urlparse(webhook_url)
            secret = str(getattr(settings, "CUSTOMER_TELEGRAM_WEBHOOK_SECRET", "") or "")
            if parsed.scheme != "https" or not parsed.netloc:
                raise CommandError("dedicated customer Telegram webhook URL must be HTTPS")
            if not _SECRET_RE.fullmatch(secret):
                raise CommandError("dedicated customer Telegram webhook secret is invalid")
            self._request("setWebhook", {
                "url": webhook_url,
                "secret_token": secret,
                "allowed_updates": ["message"],
                "drop_pending_updates": True,
            })
            self.stdout.write("customer Telegram webhook registered")
            return

        if options["unregister"]:
            self._request("deleteWebhook", {"drop_pending_updates": True})
            self.stdout.write("customer Telegram webhook removed")
            return

        result = self._request("getWebhookInfo").get("result")
        result = result if isinstance(result, dict) else {}
        expected = str(getattr(settings, "CUSTOMER_TELEGRAM_WEBHOOK_URL", "") or "")
        safe = {
            "configured": bool(result.get("url")),
            "url_matches_expected": bool(expected and result.get("url") == expected),
            "pending_update_count": int(result.get("pending_update_count") or 0),
            "last_error_date": result.get("last_error_date"),
        }
        self.stdout.write(json.dumps(safe, sort_keys=True))
