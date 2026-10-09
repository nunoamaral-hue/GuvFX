"""broker_intelligence read-only member API.

WP6: the member-facing Withdrawals metrics endpoint. Owner-scoped (the authenticated member's OWN, non-disconnected
accounts only — never staff cross-user aggregation, never another member's data) and DARK-gated
(``broker_withdrawal_ux_enabled`` — 404 while OFF so the member feature stays dark until the Sponsor arms it).
Observation-only: it reads the withdrawal projection; no mutation, no broker contact, no execution/credential/
financial authority.
"""
from __future__ import annotations

import os
import secrets

from django.core import signing
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .flags import broker_mailbox_connect_enabled, broker_withdrawal_ux_enabled
from .metrics import withdrawal_metrics

_STATE_SALT = "broker_intelligence.mailbox_connect.state"
_STATE_MAX_AGE = 600   # seconds the signed CSRF state is valid for the connect->callback round trip


def _conf(name: str) -> str:
    from django.conf import settings as _s
    v = getattr(_s, name, None)
    if v is None:
        v = os.getenv(name, "")
    return str(v or "")


def _oauth_client():
    """(client_id, client_secret, redirect_uri) from the ingestion secret store (settings-then-env) — NEVER the
    DB/repo. Empty when unprovisioned (the endpoints then fail with a clear 'not configured')."""
    return _conf("GMAIL_OAUTH_CLIENT_ID"), _conf("GMAIL_OAUTH_CLIENT_SECRET"), _conf("GMAIL_OAUTH_REDIRECT_URI")


class WithdrawalMetricsView(APIView):
    """GET /api/broker-intelligence/withdrawals/metrics/ — the member's own withdrawal statistics."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not broker_withdrawal_ux_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from trading.models import TradingAccount
        # ALL of the member's OWN accounts — INCLUDING disconnected/tombstoned ones. A withdrawal is a durable
        # historical fact; a withdrawal a member made on an account they later disconnected is still THEIR history
        # and must not silently vanish from their totals (truthfulness/completeness). Unlike the live-portfolio
        # dashboard (which excludes disconnected accounts because their LIVE balance is gone), this is a historical
        # record. Still strictly owner-scoped (user=request.user) with no staff bypass — never another member's data.
        accounts = TradingAccount.objects.filter(user=request.user)
        return Response(withdrawal_metrics(accounts))


class MailboxConnectView(APIView):
    """GET /api/broker-intelligence/mailboxes/connect/ — the Google consent URL for the authenticated member. The
    CSRF ``state`` is a signed, time-limited token binding the flow to THIS user, verified in the callback."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not broker_mailbox_connect_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        client_id, _secret, redirect_uri = _oauth_client()
        if not client_id or not redirect_uri:
            return Response({"ok": False, "error": "oauth_not_configured",
                             "message": "Mailbox connection is not configured yet."},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        from . import gmail_oauth
        state = signing.dumps({"uid": request.user.id, "n": secrets.token_urlsafe(8)}, salt=_STATE_SALT)
        url = gmail_oauth.build_authorization_url(client_id=client_id, redirect_uri=redirect_uri, state=state)
        return Response({"authorization_url": url})


class MailboxCallbackView(APIView):
    """GET /api/broker-intelligence/mailboxes/callback/?code=&state= — complete the OAuth connection. Verifies the
    CSRF state (signature + age + issued to THIS user), exchanges the code read-only, stores the token ENCRYPTED
    (never in the DB), and records/updates the member's ConnectedMailbox."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not broker_mailbox_connect_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        if not code or not state:
            return Response({"ok": False, "error": "missing_code_or_state"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            payload = signing.loads(state, salt=_STATE_SALT, max_age=_STATE_MAX_AGE)
        except signing.BadSignature:
            return Response({"ok": False, "error": "invalid_state"}, status=status.HTTP_400_BAD_REQUEST)
        if payload.get("uid") != request.user.id:   # CSRF: state must have been issued to THIS user
            return Response({"ok": False, "error": "state_user_mismatch"}, status=status.HTTP_400_BAD_REQUEST)

        client_id, client_secret, redirect_uri = _oauth_client()
        if not client_id or not client_secret or not redirect_uri:
            return Response({"ok": False, "error": "oauth_not_configured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        from . import gmail_oauth
        from .gmail_source import fetch_profile
        from .credential_store import store_token
        from .models import ConnectedMailbox
        try:
            token = gmail_oauth.exchange_code(code=code, client_id=client_id, client_secret=client_secret,
                                              redirect_uri=redirect_uri)
            profile = fetch_profile(token["access_token"])
        except Exception:   # noqa: BLE001 — never leak the code/token; a failure is a generic connect error
            return Response({"ok": False, "error": "oauth_exchange_failed"}, status=status.HTTP_502_BAD_GATEWAY)
        from .resolver import canonical_address
        email = str(profile.get("emailAddress", "") or "")
        # Dedup anchor: the gmail-CANONICAL form (gmail.com/googlemail.com + dots/+tag unified), so the SAME Google
        # mailbox cannot be connected twice under equivalent spellings (R2 / §5). The reported primary_email keeps the
        # address Google returned. Non-Google addresses are unchanged by canonicalisation.
        mailbox_id = canonical_address(email) if email else ("gmail:" + secrets.token_hex(8))
        # Mailbox uniqueness is (provider, provider_mailbox_id) with NO user column (so the same underlying mailbox
        # can never be double-connected / hijacked across members). If this mailbox already belongs to ANOTHER
        # member, fail with a clean 409 BEFORE storing a credential — otherwise the INSERT would raise an unhandled
        # IntegrityError (500) AND leave an orphaned encrypted token file. A re-connect by the SAME owner updates
        # their own row (below) and is fine.
        clash = (ConnectedMailbox.objects
                 .filter(provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id=mailbox_id)
                 .exclude(user=request.user).first())
        if clash is not None:
            return Response({"ok": False, "error": "mailbox_already_connected"}, status=status.HTTP_409_CONFLICT)
        credential_ref = store_token(token)   # encrypted; the DB stores only the ref
        common = dict(primary_email=email, credential_ref=credential_ref, scopes=token.get("scope", ""),
                      status=ConnectedMailbox.Status.CONNECTED)
        mb, _created = ConnectedMailbox.objects.update_or_create(
            user=request.user, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id=mailbox_id,
            # cursor_state is anchored to the current historyId ONLY on initial create. On a RE-connect an in-progress
            # cursor MUST be preserved: overwriting it to 'now' would permanently skip every message added since the
            # last sync that the worker has not yet consumed (a lost-mail path). (Django 5.0+ create_defaults.)
            create_defaults={**common, "cursor_state": str(profile.get("historyId", "") or "")},
            defaults=common)
        return Response({"ok": True, "status": "connected", "mailbox_id": mb.id, "email": email})


class MailboxRevokeView(APIView):
    """POST /api/broker-intelligence/mailboxes/<id>/revoke/ — the member revokes a connected mailbox: destroy the
    stored credential and mark it revoked, independently of any other mailbox. Owner-scoped."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, mailbox_id):
        if not broker_mailbox_connect_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from .credential_store import delete_token
        from .models import ConnectedMailbox
        mb = ConnectedMailbox.objects.filter(id=mailbox_id, user=request.user).first()   # owner-scoped
        if mb is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if mb.credential_ref:
            delete_token(mb.credential_ref)
        mb.credential_ref = ""
        mb.status = ConnectedMailbox.Status.REVOKED
        mb.save(update_fields=["credential_ref", "status", "updated_at"])
        return Response({"ok": True, "status": "revoked", "mailbox_id": mb.id})
